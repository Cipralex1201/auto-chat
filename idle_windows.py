from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable


_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


@dataclass(frozen=True)
class WindowSpec:
    """A daily time window in local time.

    `start_min` and `end_min` are minutes since midnight. If end_min <= start_min,
    the window is treated as crossing midnight into the next day.
    """

    start_min: int
    end_min: int


def parse_hhmm_to_minutes(value: str) -> int:
    m = _TIME_RE.match((value or "").strip())
    if not m:
        raise ValueError(f"Invalid time '{value}'. Expected HH:MM (24h).")
    hh = int(m.group(1))
    mm = int(m.group(2))
    if not (0 <= hh <= 23):
        raise ValueError(f"Invalid hour in time '{value}'.")
    if not (0 <= mm <= 59):
        raise ValueError(f"Invalid minute in time '{value}'.")
    return hh * 60 + mm


def parse_idle_windows(value: str | None) -> list[WindowSpec]:
    """Parse env-style value like '01:00-08:00, 13:00-14:00' into specs."""

    if not value:
        return []

    out: list[WindowSpec] = []
    parts = [p.strip() for p in value.split(",") if p.strip()]
    for part in parts:
        if "-" not in part:
            raise ValueError(
                f"Invalid idle window '{part}'. Expected 'HH:MM-HH:MM' (comma-separated for multiple windows)."
            )
        a, b = [x.strip() for x in part.split("-", 1)]
        start_min = parse_hhmm_to_minutes(a)
        end_min = parse_hhmm_to_minutes(b)
        out.append(WindowSpec(start_min=start_min, end_min=end_min))
    return out


def _seed_int(base_seed: int, day: date, window_index: int) -> int:
    payload = f"{base_seed}:{day.isoformat()}:{window_index}".encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


class DailyIdleWindows:
    """Evaluates whether 'now' is within a configured idle window.

    The start/end can be jittered by a configurable number of minutes.
    Jitter is stable per day within a run (but changes between runs).
    """

    def __init__(
        self,
        windows: Iterable[WindowSpec],
        *,
        start_jitter_min: int = 0,
        end_jitter_min: int = 0,
        base_seed: int | None = None,
        min_duration_min: int = 5,
    ) -> None:
        self._windows = list(windows)
        self._start_jitter_min = max(0, int(start_jitter_min))
        self._end_jitter_min = max(0, int(end_jitter_min))
        self._min_duration = timedelta(minutes=max(1, int(min_duration_min)))

        if base_seed is None:
            # Keep it process-local and unpredictable; we only need stability within a run.
            base_seed = int.from_bytes(hashlib.sha256(str(datetime.now().timestamp()).encode()).digest()[:8], "big")
        self._base_seed = int(base_seed)

    @property
    def enabled(self) -> bool:
        return bool(self._windows)

    def current_window_end(self, now: datetime) -> datetime | None:
        """Return the end datetime if `now` is inside any idle window, else None.

        `now` should be timezone-aware and in the local timezone.
        """

        if not self._windows:
            return None
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")

        today = now.date()
        tz = now.tzinfo
        candidates = []

        # Include windows starting today and yesterday to handle cross-midnight windows.
        for day in (today, today - timedelta(days=1)):
            candidates.extend(self._windows_for_day(day, tz))

        active_ends = [end for start, end in candidates if start <= now < end]
        if not active_ends:
            return None
        # If multiple overlap, stay idle until all are done.
        return max(active_ends)

    def _windows_for_day(self, day: date, tz) -> list[tuple[datetime, datetime]]:
        out: list[tuple[datetime, datetime]] = []

        for idx, spec in enumerate(self._windows):
            start_hh = spec.start_min // 60
            start_mm = spec.start_min % 60
            end_hh = spec.end_min // 60
            end_mm = spec.end_min % 60

            start_dt = datetime.combine(day, time(start_hh, start_mm), tzinfo=tz)
            end_day = day if spec.end_min > spec.start_min else (day + timedelta(days=1))
            end_dt = datetime.combine(end_day, time(end_hh, end_mm), tzinfo=tz)

            seed = _seed_int(self._base_seed, day, idx)

            # Deterministic jitter per (run, day, window).
            start_delta = 0
            if self._start_jitter_min:
                start_delta = (seed % (2 * self._start_jitter_min + 1)) - self._start_jitter_min
            end_delta = 0
            if self._end_jitter_min:
                # Mix seed a bit differently for end jitter.
                end_seed = ((seed >> 13) ^ (seed << 7)) & ((1 << 64) - 1)
                end_delta = (end_seed % (2 * self._end_jitter_min + 1)) - self._end_jitter_min

            start_dt = start_dt + timedelta(minutes=int(start_delta))
            end_dt = end_dt + timedelta(minutes=int(end_delta))
            if end_dt <= start_dt:
                end_dt = start_dt + self._min_duration

            out.append((start_dt, end_dt))

        return out
