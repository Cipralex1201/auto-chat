import unittest
from datetime import datetime, timedelta, timezone

from idle_windows import DailyIdleWindows, WindowSpec, parse_idle_windows


class TestIdleWindowsParsing(unittest.TestCase):
    def test_parse_single_window(self):
        specs = parse_idle_windows("01:00-08:00")
        self.assertEqual(specs, [WindowSpec(start_min=60, end_min=480)])

    def test_parse_multiple_windows(self):
        specs = parse_idle_windows("01:00-08:00, 13:00-14:00")
        self.assertEqual(specs, [WindowSpec(60, 480), WindowSpec(780, 840)])


class TestIdleWindowsEvaluation(unittest.TestCase):
    def test_inside_simple_window_returns_end(self):
        tz = timezone.utc
        win = DailyIdleWindows([WindowSpec(60, 480)], base_seed=1)
        now = datetime(2026, 5, 26, 2, 0, tzinfo=tz)
        end = win.current_window_end(now)
        self.assertEqual(end, datetime(2026, 5, 26, 8, 0, tzinfo=tz))

    def test_outside_simple_window_returns_none(self):
        tz = timezone.utc
        win = DailyIdleWindows([WindowSpec(60, 480)], base_seed=1)
        now = datetime(2026, 5, 26, 9, 0, tzinfo=tz)
        self.assertIsNone(win.current_window_end(now))

    def test_cross_midnight_window_detects_previous_day(self):
        tz = timezone.utc
        # 23:00 -> 02:00 crosses midnight
        win = DailyIdleWindows([WindowSpec(23 * 60, 2 * 60)], base_seed=1)
        now = datetime(2026, 5, 26, 1, 0, tzinfo=tz)
        end = win.current_window_end(now)
        self.assertEqual(end, datetime(2026, 5, 26, 2, 0, tzinfo=tz))

    def test_jitter_is_stable_within_instance_and_bounded(self):
        tz = timezone.utc
        base_end = datetime(2026, 5, 26, 8, 0, tzinfo=tz)
        win = DailyIdleWindows(
            [WindowSpec(60, 480)],
            start_jitter_min=20,
            end_jitter_min=20,
            base_seed=123,
        )
        now = datetime(2026, 5, 26, 2, 0, tzinfo=tz)
        end1 = win.current_window_end(now)
        end2 = win.current_window_end(now)
        self.assertEqual(end1, end2)
        self.assertIsNotNone(end1)
        self.assertGreaterEqual(end1, base_end - timedelta(minutes=20))
        self.assertLessEqual(end1, base_end + timedelta(minutes=20))


if __name__ == "__main__":
    unittest.main()
