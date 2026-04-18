"""Polls the Meet chat panel for commands.

Meet's chat DOM structure is not a public API and changes periodically. We
open the chat panel, grab all message bubbles, and diff against what we've
already seen. Commands are single-line messages starting with a prefix
(default `!`), e.g.

    !play daft punk
    !skip
    !queue
    !vol 0.7
    !pause / !resume / !np

The listener runs in a background thread and calls a handler for each
new command it sees.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException
from selenium.webdriver.common.by import By


@dataclass
class ChatMessage:
    author: str
    text: str
    id: str = ""


CommandHandler = Callable[[ChatMessage, str, list[str]], Optional[str]]
# handler(msg, command, args) -> optional reply to post back to chat


class ChatListener:
    def __init__(
        self,
        driver,
        prefix: str,
        handler: CommandHandler,
        poll_interval: float = 1.5,
    ):
        self.driver = driver
        self.prefix = prefix
        self.handler = handler
        self.poll_interval = poll_interval
        self._seen: set[str] = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._opened = False
        self._logged_first = False
        self._warned_open = False
        self._dumped_dom = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="chat-listener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    # Meet chat DOM (as of the current build):
    #   - Each message: [data-message-id] with text in descendant [jsname="dTKtvb"]
    #   - Consecutive messages from the same author share an outer bubble
    #     [jsname="Ypafjf"] whose .poVWob child holds the author name.
    _SCRAPE_JS = r"""
    const out = [];
    const seen = new Set();
    const messages = document.querySelectorAll('[data-message-id]');
    for (const m of messages) {
      const textEl = m.querySelector('[jsname="dTKtvb"]');
      const text = ((textEl && textEl.innerText) || m.innerText || '').trim();
      if (!text) continue;
      let author = '';
      const bubble = m.closest('[jsname="Ypafjf"]');
      if (bubble) {
        const a = bubble.querySelector('.poVWob');
        if (a) author = (a.innerText || '').trim();
      }
      const id = m.getAttribute('data-message-id') || (author + '|' + text);
      if (seen.has(id)) continue;
      seen.add(id);
      out.push({author, text, id});
    }
    return out;
    """

    _DUMP_JS = r"""
    const info = {
      message_id_items: document.querySelectorAll('[data-message-id]').length,
      ypafjf_bubbles: document.querySelectorAll('[jsname="Ypafjf"]').length,
      dtktvb_text_nodes: document.querySelectorAll('[jsname="dTKtvb"]').length,
      legacy_attr_items: document.querySelectorAll('[data-message-text], [data-sender-name]').length,
      aria_chat_panels: document.querySelectorAll('[aria-label*="chat" i], [aria-label*="messages" i]').length,
    };
    return info;
    """

    # Inspect + click the chat toggle via JS. Meet's toolbar uses jsaction
    # bindings that bypass Selenium's synthetic click() half the time — a
    # direct DOM click reliably fires their handlers. aria-expanded lets us
    # avoid toggling it closed when it's already open.
    _OPEN_CHAT_JS = r"""
    const btn = document.querySelector(
      'button[aria-label*="chat" i], button[aria-label*="messages" i]'
    );
    if (!btn) return 'no-button';
    if (btn.getAttribute('aria-expanded') === 'true') return 'already-open';
    btn.click();
    return 'clicked';
    """

    def _ensure_chat_open(self) -> None:
        try:
            result = self.driver.execute_script(self._OPEN_CHAT_JS)
        except Exception as e:
            result = f"error: {e}"
        if result == "clicked":
            time.sleep(0.6)  # let the panel animate in
        elif result == "no-button" and not self._warned_open:
            print("[chat] note: chat button not found — assuming already open")
            self._warned_open = True

    def _scrape_messages(self) -> list[ChatMessage]:
        try:
            raw = self.driver.execute_script(self._SCRAPE_JS) or []
        except Exception as e:
            print(f"[chat] scrape error: {e}")
            return []
        msgs = [
            ChatMessage(author=r.get("author", ""), text=r.get("text", ""), id=r.get("id", ""))
            for r in raw
        ]
        if msgs and not self._logged_first:
            print(f"[chat] scraping {len(msgs)} messages. sample: {msgs[-1]}")
            self._logged_first = True
        elif not msgs:
            # Periodic diagnostic while empty. Throttle to once every ~10 polls.
            self._empty_polls = getattr(self, "_empty_polls", 0) + 1
            if self._empty_polls % 10 == 1:
                try:
                    info = self.driver.execute_script(self._DUMP_JS) or {}
                    print(f"[chat] DOM probe: {info}")
                except Exception as e:
                    print(f"[chat] DOM probe failed: {e}")
        return msgs

    def _run(self) -> None:
        primed = False
        while not self._stop.is_set():
            try:
                self._ensure_chat_open()
                messages = self._scrape_messages()
                if not primed:
                    for msg in messages:
                        self._seen.add(msg.id or f"{msg.author}|{msg.text}")
                    primed = True
                    continue
                for msg in messages:
                    key = msg.id or f"{msg.author}|{msg.text}"
                    if key in self._seen:
                        continue
                    self._seen.add(key)
                    # Skip bot's own replies (tagged with an invisible marker).
                    if msg.text.startswith("\u200b"):
                        continue
                    print(f"[chat] new message: {msg.author}: {msg.text!r}")
                    if not msg.text.startswith(self.prefix):
                        continue
                    body = msg.text[len(self.prefix) :].strip()
                    if not body:
                        continue
                    parts = body.split(maxsplit=1)
                    cmd = parts[0].lower()
                    args = parts[1].split() if len(parts) > 1 else []
                    try:
                        self.handler(msg, cmd, args)
                    except Exception as e:
                        print(f"[chat] handler error: {e}")
            except Exception as e:
                print(f"[chat] poll error: {e}")
            self._stop.wait(self.poll_interval)
