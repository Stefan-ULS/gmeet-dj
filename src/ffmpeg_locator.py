"""Locate ffmpeg/ffprobe and wire them into pydub + os.environ PATH.

Windows' per-app PATH is unreliable when venvs, VSCode terminals, and
winget installs interact. Rather than fight it, we search known
locations at import time and configure:

  - pydub.AudioSegment.{converter,ffmpeg,ffprobe} → absolute paths
  - prepend the bin dir to os.environ['PATH'] so yt-dlp subprocesses also see it

Call `ensure_ffmpeg()` once at startup before any pydub or yt-dlp use.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional


def _winget_packages_dir() -> Optional[Path]:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    p = Path(local) / "Microsoft" / "WinGet" / "Packages"
    return p if p.exists() else None


def _candidate_bins() -> list[Path]:
    """Plausible directories containing ffmpeg.exe, most-likely first."""
    bins: list[Path] = []

    # 1. Anything already on PATH.
    on_path = shutil.which("ffmpeg")
    if on_path:
        bins.append(Path(on_path).parent)

    # 2. Winget's Gyan.FFmpeg install location (version number varies).
    wg = _winget_packages_dir()
    if wg:
        for pkg in wg.glob("Gyan.FFmpeg_*"):
            for build in pkg.glob("ffmpeg-*-full_build"):
                b = build / "bin"
                if b.exists():
                    bins.append(b)
        # Also try Gyan.FFmpeg.Essentials and the newer layouts.
        for pkg in wg.glob("Gyan.FFmpeg.*"):
            for b in pkg.rglob("bin"):
                if (b / "ffmpeg.exe").exists():
                    bins.append(b)

    # 3. Chocolatey / manual common locations.
    for candidate in (
        Path("C:/ProgramData/chocolatey/bin"),
        Path("C:/ffmpeg/bin"),
        Path("C:/Program Files/ffmpeg/bin"),
    ):
        if (candidate / "ffmpeg.exe").exists():
            bins.append(candidate)

    # Dedup while preserving order.
    seen: set[Path] = set()
    out: list[Path] = []
    for b in bins:
        r = b.resolve()
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def ensure_ffmpeg() -> Path:
    """Find ffmpeg, configure pydub, patch PATH. Returns the bin directory.

    Raises RuntimeError with a clear hint if ffmpeg can't be located.
    """
    candidates = _candidate_bins()
    for bin_dir in candidates:
        ffmpeg = bin_dir / "ffmpeg.exe"
        ffprobe = bin_dir / "ffprobe.exe"
        if ffmpeg.exists():
            # Configure pydub so it stops warning and actually decodes.
            from pydub import AudioSegment

            AudioSegment.converter = str(ffmpeg)
            AudioSegment.ffmpeg = str(ffmpeg)
            if ffprobe.exists():
                AudioSegment.ffprobe = str(ffprobe)

            # Make sure subprocesses (yt-dlp) see ffmpeg too.
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            return bin_dir

    raise RuntimeError(
        "ffmpeg not found. Install via `winget install Gyan.FFmpeg` and try again. "
        "Searched: " + ", ".join(str(b) for b in candidates) if candidates else "Searched: <none>"
    )
