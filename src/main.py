"""Entry point: wire DJ engine + Meet bot + chat listener."""

from __future__ import annotations

import re
import sys
import threading
import time

# Resolve ffmpeg before importing anything that touches pydub, so pydub and
# yt-dlp both see a working converter regardless of shell PATH state.
from src.ffmpeg_locator import ensure_ffmpeg

ensure_ffmpeg()

from src.audio_engine import DJEngine, Track
from src.chat_listener import ChatListener, ChatMessage
from src.config import Config
from src.meet_bot import MeetBot
from src.youtube_source import YouTubeSource


_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def build_command_handler(dj: DJEngine, bot: MeetBot, yt: YouTubeSource | None):
    def queue_from_youtube(query: str) -> None:
        """Run in a worker so chat polling keeps flowing while the download runs."""
        if yt is None:
            bot.send_chat("youtube disabled (install yt-dlp)")
            return
        bot.send_chat(f"fetching: {query} ...")
        path = yt.fetch(query)
        if path is None:
            bot.send_chat(f"yt: no result for '{query}'")
            return
        dj.enqueue_path(path)
        bot.send_chat(f"queued: {path.stem}")

    def handler(msg: ChatMessage, cmd: str, args: list[str]) -> None:
        reply: str | None = None

        if cmd in ("play", "queue", "add") and args:
            query = " ".join(args)
            # URL → always YouTube. Otherwise try local library first, then fall back to YT.
            if _URL_RE.match(query):
                threading.Thread(target=queue_from_youtube, args=(query,), daemon=True).start()
                return
            match = dj.enqueue(query)
            if match:
                reply = f"queued: {match.stem}"
            else:
                threading.Thread(target=queue_from_youtube, args=(query,), daemon=True).start()
                return
        elif cmd in ("yt", "ytplay", "ytm") and args:
            threading.Thread(
                target=queue_from_youtube, args=(" ".join(args),), daemon=True
            ).start()
            return
        elif cmd in ("skip", "next"):
            dj.skip()
            reply = "skipping"
        elif cmd == "pause":
            dj.pause()
            reply = "paused"
        elif cmd in ("resume", "unpause"):
            dj.resume()
            reply = "resumed"
        elif cmd == "vol" and args:
            try:
                dj.set_volume(float(args[0]))
                reply = f"volume {args[0]}"
            except ValueError:
                reply = "usage: !vol 0.8"
        elif cmd in ("np", "nowplaying"):
            np = dj.now_playing()
            reply = f"now playing: {np}" if np else "nothing"
        elif cmd in ("list", "q"):
            items = dj.list_queue(limit=5)
            reply = "up next: " + ", ".join(items) if items else "queue empty"
        elif cmd == "shuffle":
            dj.shuffle_queue()
            reply = "shuffled"
        elif cmd == "help":
            reply = (
                "!play <name|url> | !yt <query> | !skip | !pause | !resume "
                "| !vol N | !np | !list | !shuffle"
            )

        if reply:
            bot.send_chat(reply)

    return handler


def main() -> int:
    cfg = Config.load()

    bot = MeetBot(
        meet_url=cfg.meet_url,
        display_name=cfg.bot_display_name,
        user_data_dir=cfg.chrome_user_data_dir,
    )

    def announce(track: Track) -> None:
        try:
            bot.send_chat(f"now playing: {track.name}")
        except Exception:
            pass

    dj = DJEngine(
        music_dir=cfg.music_dir,
        output_device_substring=cfg.output_device_substring,
        sample_rate=cfg.sample_rate,
        crossfade_seconds=cfg.crossfade_seconds,
        default_volume=cfg.default_volume,
        shuffle=cfg.shuffle_on_start,
        on_track_change=announce,
        # Cached YT downloads count as library so previously-played tracks
        # come back around in the shuffle loop.
        extra_library_dirs=[cfg.youtube_cache_dir] if cfg.youtube_enabled else [],
    )

    yt = (
        YouTubeSource(
            cache_dir=cfg.youtube_cache_dir,
            cookies_from_browser=cfg.youtube_cookies_from_browser,
        )
        if cfg.youtube_enabled
        else None
    )

    bot.launch()
    bot.join()
    bot.unmute()
    dj.start()

    listener = None
    if cfg.allow_chat_control:
        listener = ChatListener(
            driver=bot.driver,
            prefix=cfg.chat_command_prefix,
            handler=build_command_handler(dj, bot, yt),
        )
        listener.start()

    print(
        "[main] running. Commands: yt <query> | play <name|url> | skip | "
        "pause | resume | vol N | np | list | shuffle | q"
    )
    # Reuse the same command handler used for Meet chat so terminal and
    # chat control stay in sync.
    class _FakeMsg:
        author = "console"
        text = ""

    console_handler = build_command_handler(dj, bot, yt)

    try:
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                time.sleep(0.5)
                continue
            if not line:
                continue
            if line.lower() in ("q", "quit", "exit", "stop"):
                break
            parts = line.split(maxsplit=1)
            cmd = parts[0].lower()
            args = parts[1].split() if len(parts) > 1 else []
            try:
                console_handler(_FakeMsg(), cmd, args)
            except Exception as e:
                print(f"[main] command error: {e}")
    except KeyboardInterrupt:
        print("\n[main] interrupt received, shutting down...")
    finally:
        try:
            if listener:
                listener.stop()
            dj.stop()
            bot.quit()
        except Exception as e:
            print(f"[main] shutdown error: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
