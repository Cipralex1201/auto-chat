from __future__ import annotations

import logging
import time

from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

LOGGER = logging.getLogger(__name__)


def send_message(driver, text: str) -> bool:
    message = (text or "").strip()
    if not message:
        LOGGER.warning("Refusing to send empty message")
        return False

    # Prefer the DM composer inside the main message area.
    # On Instagram DMs this is typically a contenteditable role=textbox.
    candidates = [
        "(//main | //*[@role='main'])[1]//div[@role='textbox' and (@contenteditable='true' or @contenteditable='')]",
        "(//main | //*[@role='main'])[1]//textarea",
        # Fallbacks (less preferred)
        "//div[@role='textbox' and (@contenteditable='true' or @contenteditable='')]",
        "//textarea",
    ]

    wait = WebDriverWait(driver, 10)
    last_error: Exception | None = None

    def _read_composer_text(el) -> str:
        try:
            # contenteditable composer
            return (el.get_attribute("textContent") or "").strip()
        except Exception:  # noqa: BLE001
            return (el.text or "").strip()

    def _normalize(s: str) -> str:
        return " ".join((s or "").strip().split()).lower()

    def _js_set_composer_text(el, message_text: str) -> None:
        # Fallback for cases where Instagram drops keystrokes (e.g., only "T").
        # Works for both contenteditable and textarea-like inputs.
        driver.execute_script(
            """
            const el = arguments[0];
            const text = arguments[1];
            if (!el) return;
            try { el.focus(); } catch (e) {}

            const tag = (el.tagName || "").toUpperCase();
                        const ce = (el.getAttribute('contenteditable') || '').toLowerCase();
                        const isCE = (ce === 'true' || ce === '');

                        if (tag === "TEXTAREA" || ("value" in el && !isCE)) {
              try { el.value = text; } catch (e) {}
            } else {
                            // Try to update contenteditable in a way React/IG accepts.
                            try {
                                // Select all current content
                                const sel = window.getSelection && window.getSelection();
                                if (sel && el) {
                                    const range = document.createRange();
                                    range.selectNodeContents(el);
                                    sel.removeAllRanges();
                                    sel.addRange(range);
                                }
                            } catch (e) {}

                            try {
                                // These often trigger the right internal handlers.
                                document.execCommand('selectAll', false, null);
                                document.execCommand('delete', false, null);
                                document.execCommand('insertText', false, text);
                            } catch (e) {
                                try { el.textContent = text; } catch (e2) {}
                            }
            }

            let evt;
            try {
              evt = new InputEvent('input', { bubbles: true });
            } catch (e) {
              evt = new Event('input', { bubbles: true });
            }
            try { el.dispatchEvent(evt); } catch (e) {}
            """,
            el,
            message_text,
        )

    def _js_clear_composer(el) -> None:
        _js_set_composer_text(el, "")

    def _type_message(el, message_text: str) -> None:
        # Focus + clear
        ActionChains(driver).move_to_element(el).click(el).perform()
        time.sleep(0.05)
        try:
            el.send_keys(Keys.CONTROL, "a")
            el.send_keys(Keys.BACKSPACE)
        except Exception:  # noqa: BLE001
            pass

        # Type in chunks; Instagram sometimes drops fast key bursts.
        chunk_size = 12
        for i in range(0, len(message_text), chunk_size):
            el.send_keys(message_text[i : i + chunk_size])
            time.sleep(0.02)

    def _is_editable(el) -> bool:
        try:
            tag = (el.tag_name or "").lower()
        except Exception:  # noqa: BLE001
            tag = ""
        if tag == "textarea":
            return True
        try:
            ce = (el.get_attribute("contenteditable") or "").strip().lower()
            if ce in {"true", ""}:
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _pick_best_candidate(elements: list) -> list:
        # Return elements sorted by visual Y (bottom-most first).
        scored: list[tuple[float, object]] = []
        for el in elements:
            try:
                if not el.is_displayed() or not el.is_enabled():
                    continue
            except Exception:  # noqa: BLE001
                continue
            if not _is_editable(el):
                continue
            try:
                y = float(el.rect.get("y", 0.0))
            except Exception:  # noqa: BLE001
                y = 0.0
            scored.append((y, el))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [el for _, el in scored]

    # Gather candidates (across xpaths) then try best (bottom-most) first.
    gathered: list = []
    for xpath in candidates:
        try:
            gathered.extend(driver.find_elements(By.XPATH, xpath))
        except Exception:  # noqa: BLE001
            continue

    ordered = _pick_best_candidate(gathered)
    if not ordered:
        # As a last resort, use the first clickable candidate from the original list.
        for xpath in candidates:
            try:
                ordered = [wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))]
                break
            except TimeoutException as exc:
                last_error = exc
                continue

    for composer in ordered[:3]:
        try:
            # Clear using JS first to avoid stuck leftovers.
            try:
                _js_clear_composer(composer)
            except Exception:  # noqa: BLE001
                pass

            _type_message(composer, message)
            composed = _read_composer_text(composer)

            # If we only got a prefix (common failure: just first character), retry once.
            if _normalize(composed) != _normalize(message):
                LOGGER.debug(
                    "Composer text mismatch; retrying via active element. expected_prefix=%s got=%s",
                    message[:20],
                    composed[:20],
                )
                active = driver.switch_to.active_element
                _type_message(active, message)
                composer = active
                composed = _read_composer_text(composer)

            # If we still didn't get the message into the composer, use JS fallback.
            if _normalize(composed) != _normalize(message):
                LOGGER.debug(
                    "Composer still mismatched after retry; using JS fallback. expected_prefix=%s got=%s",
                    message[:20],
                    composed[:20],
                )
                _js_set_composer_text(composer, message)
                composed = _read_composer_text(composer)

            LOGGER.debug("Composer contains (len=%d): %s", len(composed), composed[:80])
            if _normalize(composed) != _normalize(message):
                LOGGER.warning(
                    "Refusing to send because composer mismatch persists. expected=%s got=%s",
                    message[:40],
                    composed[:40],
                )
                continue

            composer.send_keys(Keys.ENTER)
            return True
        except StaleElementReferenceException as exc:
            last_error = exc
            continue

    LOGGER.warning("Could not find/click message composer textbox (last_error=%s)", type(last_error).__name__)
    return False
