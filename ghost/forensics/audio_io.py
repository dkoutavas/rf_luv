#!/usr/bin/env python3
"""
Audio I/O for the ghost forensics tools.

Inputs are public videos. yt-dlp fetches the original audio stream (opus/aac,
not re-encoded), and ffmpeg decodes any container to a plain float array for
analysis. Both are host binaries already installed; no new Python dependency.

No channel or person names are stored anywhere. Callers pass an opaque video_id.
"""

import os
import json
import shutil
import subprocess

import numpy as np


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def fetch_audio(url: str, out_dir: str, video_id: str = "clip") -> str:
    """Download the best original audio stream with yt-dlp. Returns the file path.

    Does not re-encode (the analysis wants the delivered codec's own roll-off).
    """
    if not have("yt-dlp"):
        raise RuntimeError("yt-dlp not installed")
    os.makedirs(out_dir, exist_ok=True)
    out_tmpl = os.path.join(out_dir, f"{video_id}.%(ext)s")
    subprocess.run(
        ["yt-dlp", "-f", "bestaudio", "--no-playlist", "-o", out_tmpl, url],
        check=True)
    for f in os.listdir(out_dir):
        if f.startswith(f"{video_id}."):
            return os.path.join(out_dir, f)
    raise RuntimeError("yt-dlp produced no output file")


def decode_audio(path: str, fs: int = 48000, start_s: float = None,
                 dur_s: float = None) -> np.ndarray:
    """Decode any audio/video file to mono float32 at fs via ffmpeg. Returns [-1,1]."""
    if not have("ffmpeg"):
        raise RuntimeError("ffmpeg not installed")
    cmd = ["ffmpeg", "-v", "error"]
    if start_s is not None:
        cmd += ["-ss", f"{start_s:.3f}"]
    cmd += ["-i", path]
    if dur_s is not None:
        cmd += ["-t", f"{dur_s:.3f}"]
    cmd += ["-f", "f32le", "-ac", "1", "-ar", str(fs), "-"]
    proc = subprocess.run(cmd, capture_output=True, check=True)
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def load(path_or_url: str, out_dir: str, fs: int = 48000, video_id: str = "clip",
         start_s: float = None, dur_s: float = None) -> np.ndarray:
    """Fetch (if a URL) then decode to a mono float array."""
    if path_or_url.startswith(("http://", "https://")):
        path_or_url = fetch_audio(path_or_url, out_dir, video_id)
    return decode_audio(path_or_url, fs=fs, start_s=start_s, dur_s=dur_s)
