from __future__ import annotations

import json
import logging
import shutil
import subprocess
import zipfile
from pathlib import Path

import httpx

from pixiv_lab.config import PIXIV_API_BASE, REQUEST_TIMEOUT
from pixiv_lab.pixiv import pixiv_headers

log = logging.getLogger(__name__)

# A ugoira that plays in under this many seconds gets looped so it is watchable in
# the channel instead of a 0.8s blink. Telegram natively loops short videos too, but
# a self-looped mp4 is independent of client behaviour.
MIN_WATCHABLE_SECONDS = 3.0
LOOP_MULTIPLIER = 3  # repeat the animation so the video is long enough to appreciate


class UgoiraError(RuntimeError):
    """The ugoira could not be fetched / converted. The post is failed, not skipped."""


def fetch_ugoira_meta(client: httpx.Client, token: str, illust_id: int) -> dict:
    """The app-api metadata for one ugoira.

    Real response (app-api /v1/ugoira/metadata):
        {"ugoira_metadata": {
            "zip_urls": {"medium": "https://.../ugoira600x600.zip", "large": "..."},
            "frames": [{"file": "000000.jpg", "delay": 65}, ...]
        }}
    The web endpoint also exposes originalSrc + mime_type; we only need the zip + delays
    (the frames themselves encode the format, ffmpeg detects it).
    """
    url = f"{PIXIV_API_BASE}/ugoira/metadata"
    r = client.get(url, params={"illust_id": illust_id}, headers=pixiv_headers(token), timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    meta = (data or {}).get("ugoira_metadata") or {}
    if not isinstance(meta, dict):
        raise UgoiraError(f"ugoira metadata missing for {illust_id}")
    return meta


def _zip_url(meta: dict) -> str:
    zips = meta.get("zip_urls") or {}
    return zips.get("large") or zips.get("medium") or ""


def download_ugoira_zip(client: httpx.Client, token: str, illust_id: int, dest: Path) -> Path:
    """Download the frame zip (with its frames + animation.json inside) into dest."""
    meta = fetch_ugoira_meta(client, token, illust_id)
    url = _zip_url(meta)
    if not url:
        raise UgoiraError(f"ugoira {illust_id}: no zip_urls in metadata")
    r = client.get(url, headers=pixiv_headers(token), timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    out = dest / f"ugoira_{illust_id}.zip"
    out.write_bytes(r.content)
    log.info("Downloaded ugoira %s: %.1f KB", illust_id, len(r.content) / 1024)
    return out


def _extract_zip(zip_path: Path, frames_dir: Path, meta: dict | None) -> list[tuple[str, int]]:
    """Unzip frames; returns [(frame_filename, delay_ms)] with the animation delays."""
    frames: list[tuple[str, int]] = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(frames_dir)
            names = [n for n in zf.namelist() if _is_frame(n)]
    except (zipfile.BadZipFile, OSError) as exc:
        raise UgoiraError(f"ugoira zip unreadable: {exc}") from exc
    names.sort()

    # delays come from ugoira metadata (frame file -> delay); if meta lacks them,
    # fall back to animation.json inside the zip (gallery-dl convention).
    delays: dict[str, int] = {}
    if meta:
        for fr in meta.get("frames") or []:
            fname = str(fr.get("file") or "")
            delay = fr.get("delay")
            if fname and isinstance(delay, int):
                delays[fname] = delay
    if not delays:
        aj = frames_dir / "animation.json"
        if aj.exists():
            try:
                payload = json.loads(aj.read_text(encoding="utf-8"))
                for fr in payload.get("frames") or []:
                    fname = str(fr.get("file") or "")
                    delay = fr.get("delay")
                    if fname and isinstance(delay, int):
                        delays[fname] = delay
            except (ValueError, OSError):
                log.warning("ugoira animation.json unreadable; using equal-frame timing")

    for name in names:
        delays.setdefault(name, 100)
        frames.append((name, delays[name]))
    if not frames:
        raise UgoiraError("ugoira zip contained no image frames")
    return frames


def _is_frame(name: str) -> bool:
    low = name.lower()
    return any(low.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp"))


def _plan_timing(frames: list[tuple[str, int]]) -> tuple[list[str], float]:
    """Per-frame ffmpeg timing is unsupported in concat demuxer; we bake the average
    delay into a constant frame rate (the common ugoira build approach)."""
    avg_delay_ms = sum(d for _, d in frames) / max(1, len(frames))
    fps = round(1000.0 / max(1, avg_delay_ms), 3)
    return [name for name, _ in frames], fps


def convert_ugoira_to_video(
    client: httpx.Client,
    token: str,
    illust_id: int,
    workdir: Path,
    ffmpeg: str | None = None,
) -> tuple[str, bytes]:
    """Build a watchable mp4 from a ugoira. Returns (filename, bytes).

    workdir must already exist and be the caller's temp dir; the zip and frame
    extract live under it and are removed before returning.
    """
    ffmpeg_bin = ffmpeg or shutil.which("ffmpeg") or "ffmpeg"
    asset_dir = workdir / f"ugoira_{illust_id}"
    asset_dir.mkdir(parents=True, exist_ok=True)
    zip_path = None
    try:
        zip_path = download_ugoira_zip(client, token, illust_id, asset_dir)
        meta = fetch_ugoira_meta(client, token, illust_id)
        frames_dir = asset_dir / "frames"
        frames_dir.mkdir(exist_ok=True)
        ordered, fps = _plan_timing(_extract_zip(zip_path, frames_dir, meta))

        concat = asset_dir / "concat.txt"
        seg = 1.0 / max(fps, 1.0)
        with concat.open("w", encoding="utf-8") as fh:
            for name in ordered:
                fh.write(f"file 'frames/{name}'\nduration {seg:.4f}\n")
        # last line repeats so the tail frame does not get cut short
        with concat.open("a", encoding="utf-8") as fh:
            fh.write(f"file 'frames/{ordered[-1]}'\n")

        out_name = f"ugoira_{illust_id}.mp4"
        out_path = asset_dir / out_name
        cmd = [
            ffmpeg_bin, "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
            "-vf", "fps=30,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", str(out_path),
        ]
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if run.returncode != 0 or not out_path.exists():
            raise UgoiraError(f"ffmpeg failed: {run.stderr[-400:]}")

        duration = _probe_seconds(ffmpeg_bin, out_path)
        if duration < MIN_WATCHABLE_SECONDS:
            # self-loop the video LOOP_MULTIPLIER times so it is long enough to watch
            looped = asset_dir / f"looped_{out_name}"
            lcmd = [
                ffmpeg_bin, "-y", "-stream_loop", str(LOOP_MULTIPLIER), "-i", str(out_path),
                "-c", "copy", "-movflags", "+faststart", str(looped),
            ]
            rl = subprocess.run(lcmd, capture_output=True, text=True, timeout=120)
            if rl.returncode == 0 and looped.exists():
                out_path = looped

        data = out_path.read_bytes()
        log.info("Ugoira %s -> %s (%.1f KB)", illust_id, out_path.name, len(data) / 1024)
        return f"{illust_id}.mp4", data
    finally:
        if zip_path is not None:
            try:
                zip_path.unlink(missing_ok=True)
            except OSError:
                pass
        shutil.rmtree(asset_dir, ignore_errors=True)


def _probe_seconds(ffmpeg_bin: str, video: Path) -> float:
    run = subprocess.run(
        [ffmpeg_bin, "-i", str(video)], capture_output=True, text=True, timeout=30
    )
    import re

    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", run.stderr)
    if not m:
        return 0.0
    h, mm, ss = m.groups()
    return int(h) * 3600 + int(mm) * 60 + float(ss)
