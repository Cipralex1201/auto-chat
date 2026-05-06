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
    candidates = [
        "(//main | //*[@role='main'])[1]//div[@role='textbox' and (@contenteditable='true' or not(@contenteditable))]",
        "//div[@role='textbox' and (@contenteditable='true' or not(@contenteditable))]",
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
            if (tag === "TEXTAREA" || ("value" in el && el.getAttribute("contenteditable") !== "true")) {
              try { el.value = text; } catch (e) {}
            } else {
              try { el.textContent = text; } catch (e) {}
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

    for xpath in candidates:
        try:
            composer = wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
            try:
                _type_message(composer, message)
                composed = _read_composer_text(composer)

                # If we only got a prefix (common failure: just first character), retry once.
                if not composed or not message.lower().startswith(composed.lower()) or len(composed) < min(3, len(message)):
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
                if not composed or not message.lower().startswith(composed.lower()) or len(composed) < min(3, len(message)):
                    LOGGER.debug(
                        "Composer still mismatched after retry; using JS fallback. expected_prefix=%s got=%s",
                        message[:20],
                        composed[:20],
                    )
                    _js_set_composer_text(composer, message)
                    composed = _read_composer_text(composer)

                LOGGER.debug("Composer contains (len=%d): %s", len(composed), composed[:80])
                composer.send_keys(Keys.ENTER)
                return True
            except StaleElementReferenceException as exc:
                # Instagram can re-render the composer; retry once for this xpath.
                last_error = exc
                composer = wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
                _type_message(composer, message)
                composed = _read_composer_text(composer)
                if not composed or not message.lower().startswith(composed.lower()) or len(composed) < min(3, len(message)):
                    try:
                        _js_set_composer_text(composer, message)
                    except Exception:  # noqa: BLE001
                        pass
                composer.send_keys(Keys.ENTER)
                return True
        except TimeoutException as exc:
            last_error = exc
            continue

    LOGGER.warning("Could not find/click message composer textbox (last_error=%s)", type(last_error).__name__)
    return False
