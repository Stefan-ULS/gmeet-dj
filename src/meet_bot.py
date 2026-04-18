"""Selenium-driven Google Meet joiner.

Google does not expose an API for joining Meet as a bot, so we drive a real
Chrome instance. A persistent user-data-dir lets the bot stay signed in
across runs (sign in manually once; subsequent launches skip the login).

Chrome is launched with the VB-Cable OUTPUT wired to the page's microphone
via preferences, so whatever we stream to CABLE Input is transmitted as
the bot's voice.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


# Prepended to every message the bot sends; invisible in chat but lets the
# listener recognize and skip its own replies without a race-prone text buffer.
BOT_MARKER = "\u200b"


class MeetBot:
    def __init__(
        self,
        meet_url: str,
        display_name: str,
        user_data_dir: str,
        headless: bool = False,
    ):
        self.meet_url = meet_url
        self.display_name = display_name
        self.user_data_dir = str(Path(user_data_dir).resolve())
        self.headless = headless
        self.driver: Optional[webdriver.Chrome] = None

    def launch(self) -> None:
        # A stale profile from a previous crashed run is the #1 cause of
        # "DevToolsActivePort file doesn't exist". Clear Chrome's per-instance
        # lock files so a fresh launch isn't blocked.
        self._clear_profile_locks()

        opts = Options()
        opts.add_argument(f"--user-data-dir={self.user_data_dir}")
        opts.add_experimental_option(
            "prefs",
            {
                "profile.default_content_setting_values.media_stream_mic": 1,
                "profile.default_content_setting_values.media_stream_camera": 1,
                "profile.default_content_setting_values.notifications": 2,
            },
        )
        opts.add_argument("--use-fake-ui-for-media-stream")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-extensions")
        opts.add_argument("--start-maximized")
        if self.headless:
            opts.add_argument("--headless=new")

        service = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=service, options=opts)

    def _clear_profile_locks(self) -> None:
        """Remove Chrome's singleton lock files left over by a prior crash."""
        p = Path(self.user_data_dir)
        if not p.exists():
            return
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
            for f in p.rglob(name):
                try:
                    f.unlink()
                except Exception:
                    pass

    def join(self, timeout: int = 60) -> None:
        assert self.driver is not None, "call launch() first"
        d = self.driver
        d.get(self.meet_url)
        wait = WebDriverWait(d, timeout)

        # If a name prompt appears (unauthenticated flow), fill it.
        try:
            name_input = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[aria-label='Your name']"))
            )
            name_input.clear()
            name_input.send_keys(self.display_name)
        except TimeoutException:
            pass  # signed-in flow doesn't show this

        # Mute camera only (Ctrl+E). Mic state is handled after join via
        # ensure_unmuted(), which inspects the button rather than toggling.
        self._press_shortcut(Keys.CONTROL, "e")

        # Click "Join now" / "Ask to join" — text varies by locale and meeting type.
        join_texts = ["Join now", "Ask to join", "Join", "Ask"]
        clicked = False
        deadline = time.time() + timeout
        while time.time() < deadline and not clicked:
            for txt in join_texts:
                try:
                    btn = d.find_element(
                        By.XPATH,
                        f"//button[.//span[contains(text(), '{txt}')] or contains(., '{txt}')]",
                    )
                    btn.click()
                    clicked = True
                    break
                except NoSuchElementException:
                    continue
            time.sleep(1)
        if not clicked:
            raise RuntimeError("Could not locate a Join/Ask-to-join button")

        # Wait until we are in the call (mic toolbar visible).
        wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "[aria-label*='microphone' i], [data-is-muted]")
            )
        )
        print("[meet] joined")

    def ensure_unmuted(self, retries: int = 3) -> None:
        """Inspect the mic button state and unmute only if currently muted.

        Blind Ctrl+D toggling lands on the wrong end-state about half the
        time depending on whether Meet's pre-join screen auto-granted mic.
        """
        for _ in range(retries):
            muted = self._is_mic_muted()
            if muted is False:
                return
            self._press_shortcut(Keys.CONTROL, "d")
            time.sleep(0.4)
        print("[meet] WARN: could not confirm unmuted state")

    # Kept for backward compat with main.py — maps to the safer version.
    def unmute(self) -> None:
        self.ensure_unmuted()

    def _is_mic_muted(self) -> Optional[bool]:
        """Returns True/False if determinable, None otherwise."""
        assert self.driver is not None
        try:
            btn = self.driver.find_element(
                By.CSS_SELECTOR,
                "[data-is-muted][aria-label*='microphone' i], "
                "button[aria-label*='microphone' i], "
                "div[role='button'][aria-label*='microphone' i]",
            )
        except NoSuchElementException:
            return None
        attr = btn.get_attribute("data-is-muted")
        if attr == "true":
            return True
        if attr == "false":
            return False
        label = (btn.get_attribute("aria-label") or "").lower()
        if "turn on" in label or "unmute" in label:
            return True
        if "turn off" in label or "mute" in label:
            return False
        return None

    def send_chat(self, text: str) -> None:
        """Post a message to the Meet chat. Assumes chat panel is already open.

        Modern Meet uses a contenteditable div, not a textarea. Selenium's
        native send_keys is flaky on these — we set the text via JS and fire
        an Enter keypress through the active element.
        """
        assert self.driver is not None
        d = self.driver
        text = BOT_MARKER + text
        selectors = [
            "textarea[aria-label*='message' i]",
            "textarea[aria-label*='send' i]",
            "div[contenteditable='true'][aria-label*='message' i]",
            "div[contenteditable='true'][aria-label*='send' i]",
            "div[role='textbox']",
        ]
        area = None
        for sel in selectors:
            try:
                area = d.find_element(By.CSS_SELECTOR, sel)
                break
            except NoSuchElementException:
                continue
        if area is None:
            print("[meet] send_chat: no chat input found")
            return
        try:
            d.execute_script("arguments[0].scrollIntoView({block:'center'});", area)
            d.execute_script("arguments[0].focus();", area)
            tag = (area.tag_name or "").lower()
            if tag in ("textarea", "input"):
                # Fire a proper input event so Meet's React state updates.
                d.execute_script(
                    "const el = arguments[0], t = arguments[1];"
                    "const setter = Object.getOwnPropertyDescriptor("
                    "  el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype,"
                    "  'value').set;"
                    "setter.call(el, t);"
                    "el.dispatchEvent(new Event('input', {bubbles: true}));",
                    area,
                    text,
                )
            else:
                # contenteditable div: set textContent, dispatch input event.
                d.execute_script(
                    "const el = arguments[0], t = arguments[1];"
                    "el.textContent = t;"
                    "el.dispatchEvent(new InputEvent('input', {bubbles: true, data: t}));",
                    area,
                    text,
                )
            # Submit with Enter.
            area.send_keys(Keys.ENTER)
        except Exception as e:
            print(f"[meet] send_chat failed: {e}")

    def quit(self) -> None:
        if self.driver:
            try:
                self.driver.quit()
            finally:
                self.driver = None

    # ---------- helpers ----------

    def _press_shortcut(self, modifier: str, key: str) -> None:
        assert self.driver is not None
        body = self.driver.find_element(By.TAG_NAME, "body")
        body.send_keys(modifier + key)
