"""Resolve a query or URL into a cached audio file via yt-dlp.

Accepts:
- A full YouTube / YouTube Music URL (watch, music.youtube.com, youtu.be)
- A free-text query ("daft punk around the world") — resolved via ytsearch1

Downloads the best audio stream, remuxes to m4a/opus/webm (whatever yt-dlp
chose natively — no re-encode), and returns the file path. Files are
cached under `<cache_dir>/<video_id>.<ext>` so repeat requests are free.

ytmusicapi is used opportunistically for richer music-catalog search. If
it is not installed or fails, we fall back to yt-dlp's `ytsearch1:` which
searches plain YouTube.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yt_dlp

try:
    from ytmusicapi import YTMusic  # type: ignore

    _YTM = YTMusic()
except Exception:
    _YTM = None


_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _is_url(s: str) -> bool:
    return bool(_URL_RE.match(s.strip()))


def _search_video_id(query: str) -> Optional[str]:
    """Prefer YouTube Music search; fall back to None so yt-dlp's ytsearch runs."""
    if _YTM is None:
        return None
    try:
        results = _YTM.search(query, filter="songs", limit=1)
        if results:
            return results[0].get("videoId")
        results = _YTM.search(query, filter="videos", limit=1)
        if results:
            return results[0].get("videoId")
    except Exception:
        return None
    return None


class YouTubeSource:
    def __init__(self, cache_dir: str, cookies_from_browser: str = ""):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # e.g. "chrome", "edge", "firefox" — yt-dlp reads the logged-in session
        # from the browser's cookie store so Premium tier audio (256kbps AAC)
        # is served instead of free 128kbps.
        self.cookies_from_browser = cookies_from_browser.strip()

    def fetch(self, query_or_url: str) -> Optional[Path]:
        """Return a local audio file for the given input, downloading if needed."""
        target = query_or_url.strip()
        if not target:
            return None

        if not _is_url(target):
            vid = _search_video_id(target)
            target = (
                f"https://music.youtube.com/watch?v={vid}"
                if vid
                else f"ytsearch1:{target}"
            )

        # yt-dlp output template — keep the id so we can cache-hit on the same video.
        outtmpl = str(self.cache_dir / "%(id)s.%(ext)s")
        opts = {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "retries": 2,
            # Keep source codec — pydub + ffmpeg decode anything on playback.
            "postprocessors": [],
        }
        if self.cookies_from_browser:
            opts["cookiesfrombrowser"] = (self.cookies_from_browser,)
        def _run(dl_opts: dict) -> Optional[Path]:
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                info = ydl.extract_info(target, download=True)
                if "entries" in info and info["entries"]:
                    info = info["entries"][0]
                path = Path(ydl.prepare_filename(info))
                return path if path.exists() else None

        try:
            return _run(opts)
        except Exception as e:
            msg = str(e)
            # Chrome cookie DB is locked by the running browser. Retry without
            # cookies so the user still gets (free-tier) audio.
            if "cookie" in msg.lower() and "cookiesfrombrowser" in opts:
                print(f"[yt] cookie DB locked — retrying without Premium cookies")
                opts_no_cookies = {k: v for k, v in opts.items() if k != "cookiesfrombrowser"}
                try:
                    return _run(opts_no_cookies)
                except Exception as e2:
                    print(f"[yt] fallback also failed: {e2}")
                    return None
            print(f"[yt] fetch failed for '{query_or_url}': {e}")
            return None
