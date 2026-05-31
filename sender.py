from __future__ import annotations

import logging
import re
import time

from selenium.common.exceptions import InvalidArgumentException, StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

LOGGER = logging.getLogger(__name__)


def send_message(driver, text: str) -> bool:
    # IMPORTANT: Instagram DMs treat Enter as "send". Multi-line content increases
    # the chance of partial sends or focus churn. Normalize to a single line.
    message = re.sub(r"\s+", " ", (text or "")).strip()
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

    wait = WebDriverWait(driver, 15)
    last_error: Exception | None = None

    # Close any open pickers/popovers that can steal focus.
    try:
        driver.switch_to.active_element.send_keys(Keys.ESCAPE)
    except Exception:  # noqa: BLE001
        pass

    def _read_composer_text(el) -> str:
        """Read composer text in a way that's resilient to driver JSON-escape bugs.

        Some Firefox/GeckoDriver combinations can throw `InvalidArgumentException`
        (e.g. "unexpected end of hex escape") when returning raw element text that
        contains certain backslash/escape-like sequences. We fall back to a
        hex-encoded UTF-8 read via JS to keep the returned JSON safe.
        """

        def _read_via_safe_hex_js() -> str:
            try:
                hex_str = driver.execute_script(
                    """
                    const el = arguments[0];
                    const text = (el && (el.textContent ?? '')) || '';
                    // Encode to UTF-8 bytes, then return a hex string (JSON-safe).
                    let bytes;
                    try {
                      bytes = new TextEncoder().encode(text);
                    } catch (e) {
                      // Older engines: encodeURIComponent returns UTF-8 percent-escapes.
                      const enc = encodeURIComponent(text);
                      const out = [];
                      for (let i = 0; i < enc.length; i++) {
                        const ch = enc[i];
                        if (ch === '%') {
                          out.push(parseInt(enc.slice(i + 1, i + 3), 16));
                          i += 2;
                        } else {
                          out.push(ch.charCodeAt(0));
                        }
                      }
                      bytes = out;
                    }
                    let hex = '';
                    for (let i = 0; i < bytes.length; i++) {
                      hex += bytes[i].toString(16).padStart(2, '0');
                    }
                    return hex;
                    """,
                    el,
                )
                if not isinstance(hex_str, str):
                    return ""
                hex_str = hex_str.strip()
                if not hex_str:
                    return ""
                if len(hex_str) % 2 != 0:
                    return ""
                return bytes.fromhex(hex_str).decode("utf-8", errors="replace").strip()
            except Exception:  # noqa: BLE001
                return ""

        try:
            # Fast path for typical drivers.
            return (el.get_attribute("textContent") or "").strip()
        except InvalidArgumentException:
            safe = _read_via_safe_hex_js()
            if safe:
                return safe
        except Exception:  # noqa: BLE001
            pass

        try:
            return (el.text or "").strip()
        except InvalidArgumentException:
            safe = _read_via_safe_hex_js()
            if safe:
                return safe
        except Exception:  # noqa: BLE001
            pass

        return ""

    def _normalize(s: str) -> str:
        return " ".join((s or "").strip().split()).lower()

    def _describe_el(el) -> dict:
        info: dict = {}
        try:
            info["tag"] = (el.tag_name or "").lower()
        except Exception:  # noqa: BLE001
            info["tag"] = ""
        for attr in ("role", "contenteditable", "aria-label"):
            try:
                info[attr] = (el.get_attribute(attr) or "")
            except Exception:  # noqa: BLE001
                info[attr] = ""
        try:
            r = el.rect
            info["rect"] = {"x": r.get("x"), "y": r.get("y"), "w": r.get("width"), "h": r.get("height")}
        except Exception:  # noqa: BLE001
            info["rect"] = {}
        try:
            info["text"] = _read_composer_text(el)[:40]
        except Exception:  # noqa: BLE001
            info["text"] = ""
        return info

    def _dump_candidates(prefix: str, elements: list) -> None:
        sample = elements[:3]
        details = []
        for el in sample:
            try:
                details.append(_describe_el(el))
            except Exception:  # noqa: BLE001
                details.append({"error": "describe_failed"})
        try:
            active = driver.switch_to.active_element
            active_info = _describe_el(active)
        except Exception:  # noqa: BLE001
            active_info = {"error": "active_failed"}
        LOGGER.debug("%s candidates=%s active=%s", prefix, details, active_info)

    def _focus(el) -> None:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'nearest'});", el)
        except Exception:  # noqa: BLE001
            pass
        ActionChains(driver).move_to_element(el).click(el).perform()
        time.sleep(0.08)

    def _resolve_edit_target(el):
        # Instagram sometimes nests the actual editable inside a role=textbox container.
        try:
            tag = (el.tag_name or "").lower()
        except Exception:  # noqa: BLE001
            tag = ""
        if tag == "textarea":
            return el
        try:
            ce = (el.get_attribute("contenteditable") or "").strip().lower()
            if ce in {"true", ""}:
                return el
        except Exception:  # noqa: BLE001
            pass
        try:
            nested = el.find_elements(By.XPATH, ".//*[@contenteditable='true' or @contenteditable='']")
            if nested:
                return nested[-1]
        except Exception:  # noqa: BLE001
            pass
        return el

    def _js_set_composer_text(el, message_text: str) -> str:
        # Fallback for cases where Instagram drops keystrokes (e.g., only "T").
        # Returns the text content after attempting to set it.
        try:
            result = driver.execute_script(
                """
                const el = arguments[0];
                const text = arguments[1];
                if (!el) return '';

                                try { el.focus(); } catch (e) {}
                                const target = (document.activeElement || el);

                                const tag = (target.tagName || '').toUpperCase();
                                const ce = (target.getAttribute('contenteditable') || '').toLowerCase();
                                const isCE = (ce === 'true' || ce === '');

                const dispatchInput = () => {
                  let evt;
                  try { evt = new InputEvent('input', { bubbles: true }); }
                  catch (e) { evt = new Event('input', { bubbles: true }); }
                                    try { target.dispatchEvent(evt); } catch (e) {}
                };

                if (tag === 'TEXTAREA' || (('value' in el) && !isCE)) {
                                    try { target.value = text; } catch (e) {}
                  dispatchInput();
                                    try { return String(target.value || ''); } catch (e) { return ''; }
                }

                // contenteditable path
                try {
                  const sel = window.getSelection && window.getSelection();
                  if (sel) {
                    const range = document.createRange();
                                        range.selectNodeContents(target);
                    sel.removeAllRanges();
                    sel.addRange(range);
                  }
                } catch (e) {}

                let ok = false;
                try {
                  document.execCommand('selectAll', false, null);
                  document.execCommand('delete', false, null);
                  ok = document.execCommand('insertText', false, text);
                } catch (e) {
                  ok = false;
                }

                if (!ok) {
                  // Final fallback: replace DOM contents with a text node.
                  try {
                                        while (target.firstChild) target.removeChild(target.firstChild);
                                        target.appendChild(document.createTextNode(text));
                  } catch (e) {
                                        try { target.textContent = text; } catch (e2) {}
                  }
                }

                dispatchInput();
                                try { return String(target.textContent || ''); } catch (e) { return ''; }
                """,
                el,
                message_text,
            )
            return str(result or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _js_clear_composer(el) -> None:
        _js_set_composer_text(el, "")

    def _find_ordered_candidates() -> list:
        gathered: list = []
        for xpath in candidates:
            try:
                gathered.extend(driver.find_elements(By.XPATH, xpath))
            except Exception:  # noqa: BLE001
                continue
        ordered_local = _pick_best_candidate(gathered)
        if ordered_local:
            return ordered_local
        # As a last resort, use the first clickable candidate from the original list.
        for xpath in candidates:
            try:
                return [wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))]
            except TimeoutException as exc:
                nonlocal_last_error[0] = exc
                continue
        return []

    def _send_enter_on_best_effort(target_el) -> None:
        try:
            target_el.send_keys(Keys.ENTER)
            return
        except StaleElementReferenceException:
            pass
        # Try active element as a fallback.
        driver.switch_to.active_element.send_keys(Keys.ENTER)

    def _set_text_js_and_send_once() -> bool:
        ordered_local = _find_ordered_candidates()
        if not ordered_local:
            return False
        for composer_el in ordered_local[:2]:
            try:
                composer_el = _resolve_edit_target(composer_el)
                _focus(composer_el)
                _js_clear_composer(composer_el)

                js_after = _js_set_composer_text(composer_el, message)
                composed = (js_after or "").strip() or _read_composer_text(composer_el)

                if _normalize(composed) != _normalize(message):
                    last_error_local = ValueError("composer_mismatch")
                    nonlocal_last_error[0] = last_error_local
                    continue

                _send_enter_on_best_effort(composer_el)

                # Best-effort success signal: composer should clear shortly after send.
                def _composer_cleared(_drv) -> bool:
                    try:
                        ordered2 = _find_ordered_candidates()
                        if not ordered2:
                            return True
                        el2 = _resolve_edit_target(ordered2[0])
                        return _normalize(_read_composer_text(el2)) == ""
                    except Exception:  # noqa: BLE001
                        return False

                try:
                    WebDriverWait(driver, 3).until(_composer_cleared)
                except Exception:  # noqa: BLE001
                    # Not a hard failure; sometimes IG delays clearing.
                    pass

                return True
            except StaleElementReferenceException as exc:
                nonlocal_last_error[0] = exc
                continue
            except Exception as exc:  # noqa: BLE001
                nonlocal_last_error[0] = exc
                continue
        return False

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

    # We'll retry a few times, always re-finding the composer to avoid stale references.
    nonlocal_last_error: list[Exception | None] = [None]

    initial_ordered = _find_ordered_candidates()
    if initial_ordered:
        _dump_candidates("Composer candidates (pre)", initial_ordered)

    for _ in range(3):
        if _set_text_js_and_send_once():
            return True
        time.sleep(0.35)

    ordered_post = _find_ordered_candidates()
    if ordered_post:
        _dump_candidates("Composer candidates (post-fail)", ordered_post)

    last_error = nonlocal_last_error[0]
    LOGGER.warning("Failed to compose/send message (last_error=%s)", type(last_error).__name__ if last_error else "None")
    return False
