#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Video Tool
----------
Flet-based desktop app for video download (yt-dlp) and conversion (FFmpeg).

It manages its own copies of yt-dlp, ffmpeg/ffprobe and an optional
JavaScript runtime (Deno), so none of them need to be installed by hand.
yt-dlp binaries are verified against the release's signed SHA2-256SUMS,
and all downloads are restricted to HTTPS.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from collections import deque
from pathlib import Path


# ============================== dependency bootstrap ==============================
#
# IMPORTANT: flet and flet-desktop must be installed as a _pair_.
# They pin each other to an exact version (flet-desktop==0.80.5 requires
# flet==0.80.5). We therefore only install when the package is *not*
# installed at all - an existing, working installation is never
# overwritten. This keeps both packages version-synchronized.

FLET_MIN = "0.28.0"        # informational only - the API has been stable since 0.28


def _install_deps() -> None:
    # Perf: a frozen build (PyInstaller etc.) ships its own dependencies -
    # skip the check entirely.
    if getattr(sys, "frozen", False):
        return
    # Perf: importlib.util.find_spec is *significantly* faster than
    # importlib.metadata.version() - the latter scans every dist-info
    # directory under site-packages on Windows (~50-500 ms), find_spec
    # only does a single loader lookup (~1 ms).
    import importlib.util

    install = [
        pkg for pkg in ("flet", "requests")
        if importlib.util.find_spec(pkg) is None
    ]

    if install:
        print(f"Installing: {', '.join(install)}")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", *install, "-q"]
        )


_install_deps()

import flet as ft  # noqa: E402

# Perf: `requests` is imported lazily inside the few functions that need it
# (binary installer, update checks). A module-level import cost 0.2-0.4 s on
# every startup even though a normal session never touches the network.


# ============================== constants =========================================

VERSION = "0.0.2"
APP_NAME = "Video Tool"

# GitHub repo ("owner/name") used by the built-in self-update check
# (startup dialog / Info tab). The check reads VERSION straight from
# video_tool.py on GITHUB_BRANCH, so no GitHub release is required -
# pushing a commit with a higher VERSION is enough.
GITHUB_REPO = "SystemFunction/video-tool"
GITHUB_BRANCH = "main"
UPDATE_RAW_URL = (
    f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/video_tool.py"
)
UPDATE_PAGE_URL = f"https://github.com/{GITHUB_REPO}"
APP_DIR = Path.home() / ".video_tool_v3"
MAX_LOG = 1200
REFRESH_MIN_INTERVAL = 0.08  # 80ms minimum between UI refreshes
DOWNLOAD_CHUNK = 1 << 17     # 128 KiB streaming chunks for binary installer
PROC_WAIT_TIMEOUT = 15       # seconds to wait on .wait() after stop

# subprocess flags (hide console windows on Windows)
_POPEN_KWARGS: dict = {}
if platform.system() == "Windows":
    _POPEN_KWARGS["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

# ---- Theme palette -------------------------------------------------------------
ACCENT_PRIMARY = ft.Colors.CYAN_400
ACCENT_SECONDARY = ft.Colors.INDIGO_400
SURFACE_BG = ft.Colors.with_opacity(0.04, ft.Colors.WHITE)
SURFACE_PANEL = ft.Colors.with_opacity(0.07, ft.Colors.BLACK)
BORDER_FAINT = ft.Colors.with_opacity(0.15, ft.Colors.WHITE)
BORDER_SOFT = ft.Colors.with_opacity(0.20, ft.Colors.WHITE)

# log colors
_COL_DEFAULT = ft.Colors.GREY_300
_COL_ERROR = ft.Colors.RED_300
_COL_SUCCESS = ft.Colors.GREEN_300
_COL_PROGRESS = ft.Colors.CYAN_200
_COL_HEADER = ft.Colors.INDIGO_200

CODEC_OPTIONS: dict[str, list[tuple[str, str]]] = {
    "standard": [
        ("h264", "H.264 (compatible)"),
        ("h265", "H.265 / HEVC"),
        ("av1", "AV1 (modern, small)"),
        ("vp9", "VP9 (Web)"),
        ("copy", "Copy stream"),
    ],
    "editing": [
        ("h264_allintra", "H.264 All-Intra"),
        ("h264_handbrake", "H.264 Editing"),
        ("vegas_fix", "Vegas Sync Fix"),
        ("prores422", "ProRes 422"),
        ("prores422hq", "ProRes 422 HQ"),
        ("dnxhr_hq", "DNxHR HQ"),
    ],
    "delivery": [
        ("youtube", "YouTube Export (H.264)"),
        ("youtube_av1", "YouTube Export (AV1)"),
        ("social", "Instagram / TikTok"),
        ("copy", "Copy stream"),
    ],
}


# ============================== helper functions ==================================

def parse_hms_to_seconds(value: str) -> float | None:
    if not value:
        return None
    value = value.strip().replace(",", ".")
    m = re.match(r"^(\d+):(\d+):(\d+(?:\.\d+)?)$", value)
    if not m:
        return None
    h, mn, s = m.groups()
    try:
        return int(h) * 3600 + int(mn) * 60 + float(s)
    except ValueError:
        return None


def format_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    total = int(seconds)
    mm, ss = divmod(total, 60)
    hh, mm = divmod(mm, 60)
    return f"{hh:02d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"


def parse_speed_value(speed_text: str) -> float | None:
    if not speed_text:
        return None
    cleaned = speed_text.lower().replace("x", "").strip()
    try:
        val = float(cleaned)
        return val if val > 0 else None
    except ValueError:
        return None


def format_elapsed(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "0:00:00.00"
    total = int(seconds)
    frac = int((seconds - total) * 100)
    hh, rem = divmod(total, 3600)
    mm, ss = divmod(rem, 60)
    return f"{hh}:{mm:02d}:{ss:02d}.{frac:02d}"


def open_folder(path: str) -> None:
    """Opens the folder in the native file manager."""
    if not path:
        return
    p = Path(path)
    if not p.exists():
        return
    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.Popen(["open", str(p)], **_POPEN_KWARGS)
        else:
            subprocess.Popen(["xdg-open", str(p)], **_POPEN_KWARGS)
    except Exception:
        pass


# Hosts that block yt-dlp's default TLS fingerprint - impersonation is
# auto-enabled for these when curl_cffi support is available.
_IMPERSONATE_AUTO_HOSTS = (
    "instagram.com", "tiktok.com", "twitter.com",
    "//x.com", "www.x.com", "facebook.com", "fb.watch",
)

# precomputed keyword tuples - no new tuple per log line
_KW_ERROR = ("error", "failed", "traceback", "warning", "warn")
_KW_SUCCESS = ("success", "completed", "complete")
_KW_PROGRESS = ("download:", "%|", "frame=", "fps=", "speed=", "bitrate=")


def log_color(line: str) -> str:
    if line.startswith(("===", "---")):
        return _COL_HEADER
    lo = line.lower()
    for kw in _KW_ERROR:
        if kw in lo:
            return _COL_ERROR
    for kw in _KW_SUCCESS:
        if kw in lo:
            return _COL_SUCCESS
    for kw in _KW_PROGRESS:
        if kw in lo:
            return _COL_PROGRESS
    return _COL_DEFAULT


def _safe_terminate(proc: subprocess.Popen | None) -> None:
    """Terminates a process safely with a kill fallback.

    Note: we don't close the streams explicitly, so that a concurrently
    running iteration over proc.stdout ends cleanly via EOF instead of
    a read-on-closed-fd error.
    """
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
    except Exception:
        pass


def _detect_hw_encoder() -> str:
    """Detects the best available hardware acceleration via ffmpeg -encoders.
    Returns "nvidia" | "amd" | "intel" | "cpu"."""
    try:
        r = subprocess.run(
            [shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace", **_POPEN_KWARGS,
        )
        if r.returncode != 0:
            return "cpu"
        out = r.stdout.lower()
        if "h264_nvenc" in out:
            return "nvidia"
        if "h264_qsv" in out:
            return "intel"
        if "h264_amf" in out:
            return "amd"
    except Exception:
        pass
    return "cpu"


def _supports_impersonation(ytdlp_path: str) -> bool:
    """Checks whether the installed yt-dlp version supports --impersonate."""
    try:
        r = subprocess.run(
            [ytdlp_path, "--list-impersonate-targets"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace", **_POPEN_KWARGS,
        )
        return r.returncode == 0
    except Exception:
        return False


def _parse_version(text: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums) if nums else (0,)


def check_tool_update(timeout: int = 10) -> tuple[bool, str, str]:
    """Checks GitHub for a newer version of this tool itself.

    Fetches video_tool.py from GITHUB_BRANCH and compares its VERSION
    constant against the running one - this works without GitHub
    releases. Returns (has_update, latest_version, source_text) so the
    already-downloaded file can be installed directly;
    (False, "", "") on any failure or if GITHUB_REPO isn't configured.
    """
    if not GITHUB_REPO or GITHUB_REPO.startswith("yourusername"):
        return False, "", ""
    import requests
    try:
        r = requests.get(UPDATE_RAW_URL, timeout=timeout)
        if r.status_code != 200:
            return False, "", ""
        source = r.text
        m = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', source, re.M)
        if not m:
            return False, "", ""
        latest = m.group(1).strip()
        has_update = _parse_version(latest) > _parse_version(VERSION)
        return has_update, latest, source
    except Exception:
        return False, "", ""


def _detect_js_runtime() -> str:
    """Looks for a JavaScript runtime in PATH (for yt-dlp's n-challenge
    and the bgutil PO token provider). Returns "deno" | "node" | ""."""
    for rt in ("deno", "node"):
        if shutil.which(rt):
            return rt
    return ""


# ============================== config manager ====================================

class ConfigManager:
    """Stores and loads simple settings as JSON."""

    def __init__(self, app_dir: Path):
        app_dir.mkdir(parents=True, exist_ok=True)
        self._file = app_dir / "config.json"
        self._data: dict = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            if self._file.exists():
                loaded = json.loads(self._file.read_text(encoding="utf-8"))
                # Guard against a corrupt/hand-edited config that parses to a
                # non-object (list, number, null) - downstream code assumes a dict.
                self._data = loaded if isinstance(loaded, dict) else {}
        except Exception:
            self._data = {}

    def save(self) -> None:
        try:
            tmp = self._file.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self._file)  # atomic write
        except Exception:
            pass

    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        with self._lock:
            self._data[key] = value
        self.save()


# ============================== binary manager ====================================

class BinaryManager:
    def __init__(self):
        self.system = platform.system()
        self.machine = platform.machine().lower()
        self.is_windows = self.system == "Windows"
        self.app_dir = APP_DIR
        self.bin_dir = self.app_dir / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        # Directory for yt-dlp plugins (e.g. the bgutil PO token provider).
        self.plugins_dir = self.app_dir / "plugins"
        self.plugins_dir.mkdir(parents=True, exist_ok=True)

        _exe = ".exe" if self.is_windows else ""
        self.ytdlp_local = self.bin_dir / f"yt-dlp{_exe}"
        self.ffmpeg_local = self.bin_dir / f"ffmpeg{_exe}"
        self.ffprobe_local = self.bin_dir / f"ffprobe{_exe}"
        self.deno_local = self.bin_dir / f"deno{_exe}"

        # Path resolution cache - hot path during command builds and
        # progress refreshes; .exists()/shutil.which() are syscalls.
        # Invalidated after install / update.
        self._path_cache: dict[str, str] = {}

    # -- path resolution --

    def _resolve(self, name: str, local: Path) -> str:
        cached = self._path_cache.get(name)
        if cached is not None:
            return cached
        path = str(local) if local.exists() else (shutil.which(name) or str(local))
        self._path_cache[name] = path
        return path

    def invalidate_cache(self) -> None:
        self._path_cache.clear()

    def get_ytdlp_path(self) -> str:
        return self._resolve("yt-dlp", self.ytdlp_local)

    def get_ffmpeg_path(self) -> str:
        return self._resolve("ffmpeg", self.ffmpeg_local)

    def get_ffprobe_path(self) -> str:
        return self._resolve("ffprobe", self.ffprobe_local)

    def get_deno_path(self) -> str:
        return self._resolve("deno", self.deno_local)

    def get_js_runtime(self) -> tuple[str, str]:
        """Looks for a JS runtime for yt-dlp's n-challenge. Prefers the
        tool's own Deno (bin_dir), then Deno/Node from PATH.
        Returns (name, version_string) - ("", "") if none found."""
        candidates: list[tuple[str, str]] = []
        if self.deno_local.exists():
            candidates.append(("deno", str(self.deno_local)))
        for rt in ("deno", "node"):
            exe = shutil.which(rt)
            if exe:
                candidates.append((rt, exe))
        for name, exe in candidates:
            r = self._run([exe, "--version"])
            if r and r.returncode == 0:
                ver = (r.stdout.splitlines()[0].strip()
                       if r.stdout else name)
                return name, ver
        return "", ""

    # -- checks --

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=10, encoding="utf-8", errors="replace",
                **_POPEN_KWARGS,
            )
        except Exception:
            return None

    def check_ytdlp(self, retries: int = 1, retry_delay: float = 0.4) -> tuple[bool, str]:
        # retries > 0 covers the narrow window right after a Windows file
        # replace, where AV / SmartScreen may briefly lock the new .exe.
        for attempt in range(retries + 1):
            r = self._run([self.get_ytdlp_path(), "--version"])
            if r and r.returncode == 0:
                return True, r.stdout.strip() or "OK"
            if attempt < retries:
                time.sleep(retry_delay)
        return False, "Not found"

    def check_ffmpeg(self) -> tuple[bool, str]:
        r = self._run([self.get_ffmpeg_path(), "-version"])
        if r and r.returncode == 0:
            m = re.search(r"ffmpeg version\s+(\S+)", r.stdout)
            return True, m.group(1) if m else "OK"
        return False, "Not found"

    # -- install --

    def _download_to_file(
        self, url: str, target: Path, timeout: int = 60,
        on_progress=None,
    ) -> None:
        """Streams a download into a file. Writes atomically (tmp -> rename).

        Security: only https:// URLs are accepted. Several callers feed in
        URLs that originate from a remote JSON API (e.g. ffbinaries.com), so
        we refuse anything that isn't TLS-protected before touching the wire.
        """
        if not str(url).lower().startswith("https://"):
            raise ValueError(f"Refusing non-HTTPS download URL: {url!r}")
        import requests
        tmp = target.with_suffix(target.suffix + ".part")
        success = False
        try:
            with requests.get(url, stream=True, timeout=timeout) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length", "0") or "0")
                written = 0
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK):
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)
                            if on_progress:
                                try:
                                    on_progress(written, total)
                                except Exception:
                                    pass
            tmp.replace(target)
            success = True
        finally:
            if not success and tmp.exists():
                try:
                    tmp.unlink()
                except Exception:
                    pass
        if not self.is_windows:
            try:
                os.chmod(target, 0o755)
            except Exception:
                pass

    @staticmethod
    def _sha256_file(path: Path) -> str:
        """Streams a file through SHA-256 (constant memory)."""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(DOWNLOAD_CHUNK), b""):
                h.update(chunk)
        return h.hexdigest().lower()

    def _verify_ytdlp_checksum(self, target: Path, asset_name: str) -> None:
        """Verifies a freshly downloaded yt-dlp binary against the official
        SHA2-256SUMS list published alongside every yt-dlp release.

        Fail-closed on mismatch (the file is deleted and an error raised).
        If the checksum list itself cannot be fetched or does not contain an
        entry for this asset, we proceed - the binary was already fetched
        over TLS from GitHub, so this is best-effort integrity hardening on
        top of, not a replacement for, transport security.
        """
        sums_url = (
            "https://github.com/yt-dlp/yt-dlp/releases/latest/download/"
            "SHA2-256SUMS"
        )
        import requests
        try:
            resp = requests.get(sums_url, timeout=30)
            resp.raise_for_status()
            sums_text = resp.text
        except Exception:
            return  # checksum list unreachable -> skip (best-effort)

        expected = None
        for line in sums_text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == asset_name:
                expected = parts[0].strip().lower()
                break
        if not expected:
            return  # no entry for this asset -> nothing to compare against

        actual = self._sha256_file(target)
        if actual != expected:
            try:
                target.unlink(missing_ok=True)
            except Exception:
                pass
            raise RuntimeError(
                "yt-dlp checksum mismatch - download discarded "
                f"(expected {expected[:12]}..., got {actual[:12]}...)."
            )

    def _ytdlp_url(self) -> str:
        """Picks the matching yt-dlp binary for the current platform."""
        if self.system == "Windows":
            return "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
        if self.system == "Darwin":
            return "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos"
        # Linux: try to detect ARM64
        if "aarch64" in self.machine or "arm64" in self.machine:
            return "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux_aarch64"
        if self.machine.startswith("armv7") or "armhf" in self.machine:
            return "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux_armv7l"
        return "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"

    def _ffmpeg_platform_key(self) -> str:
        """Mapping for ffbinaries.com - including ARM where possible."""
        if self.system == "Windows":
            return "windows-64"
        if self.system == "Darwin":
            # ffbinaries currently only offers osx-64
            return "osx-64"
        if "aarch64" in self.machine or "arm64" in self.machine:
            return "linux-arm64"
        if self.machine.startswith("armv7"):
            return "linux-armhf"
        return "linux-64"

    def install_ytdlp(self, on_progress=None) -> None:
        url = self._ytdlp_url()
        self._download_to_file(url, self.ytdlp_local, timeout=90, on_progress=on_progress)
        # Integrity check against the release's signed SHA2-256SUMS list.
        asset_name = url.rsplit("/", 1)[-1]
        self._verify_ytdlp_checksum(self.ytdlp_local, asset_name)
        # Path cache may have pointed at PATH yt-dlp before; force re-resolve.
        self.invalidate_cache()

    def install_ffmpeg(self, on_progress=None) -> None:
        # Invalidate eagerly - even if we error halfway through, the partial
        # state should re-resolve cleanly on the next path lookup.
        self.invalidate_cache()
        import requests
        platform_key = self._ffmpeg_platform_key()
        data = requests.get("https://ffbinaries.com/api/v1/version/latest", timeout=30).json()
        binaries = data.get("bin", {}).get(platform_key, {})
        if not binaries:
            raise RuntimeError(f"No ffmpeg binaries available for {platform_key}.")

        for name, target in (("ffmpeg", self.ffmpeg_local), ("ffprobe", self.ffprobe_local)):
            url = binaries.get(name)
            if not url:
                continue
            zip_path = self.bin_dir / f"{name}.zip"
            try:
                self._download_to_file(
                    url, zip_path, timeout=180,
                    on_progress=(lambda w, t, _n=name: on_progress(_n, w, t)) if on_progress else None,
                )
                with zipfile.ZipFile(zip_path, "r") as zf:
                    found = False
                    for member in zf.namelist():
                        lo = member.lower().rstrip("/")
                        # Exact filename match (name.exe or name)
                        base = lo.rsplit("/", 1)[-1]
                        if base in (name, f"{name}.exe") and not member.endswith("/"):
                            with zf.open(member) as src, open(target, "wb") as dst:
                                shutil.copyfileobj(src, dst, length=DOWNLOAD_CHUNK)
                            found = True
                            break
                    if not found:
                        raise RuntimeError(f"{name} not found in the ZIP")
            finally:
                zip_path.unlink(missing_ok=True)
            if not self.is_windows:
                try:
                    os.chmod(target, 0o755)
                except Exception:
                    pass

    def _deno_url(self) -> str:
        """Portable Deno binary for the current platform (GitHub releases)."""
        base = "https://github.com/denoland/deno/releases/latest/download/"
        if self.system == "Windows":
            return base + "deno-x86_64-pc-windows-msvc.zip"
        if self.system == "Darwin":
            if "arm64" in self.machine or "aarch64" in self.machine:
                return base + "deno-aarch64-apple-darwin.zip"
            return base + "deno-x86_64-apple-darwin.zip"
        if "aarch64" in self.machine or "arm64" in self.machine:
            return base + "deno-aarch64-unknown-linux-gnu.zip"
        return base + "deno-x86_64-unknown-linux-gnu.zip"

    def check_deno(self) -> tuple[bool, str]:
        name, ver = self.get_js_runtime()
        if not name:
            return False, "Not found"
        return True, ver if (ver and name in ver) else f"{name} {ver}"

    def update_channel(self, channel: str) -> str:
        """Self-updates the local yt-dlp binary to the given release channel
        ("stable" | "nightly" | "master") using yt-dlp's own --update-to
        mechanism. This works against the standalone binary we manage here
        (it's the official frozen build with an embedded self-updater) and
        is the documented way to pick up fixes that have landed on
        Nightly/Master but not yet in a Stable release - see e.g.
        https://github.com/yt-dlp/yt-dlp/issues/17074.
        Returns combined stdout/stderr from the update command.
        """
        self.invalidate_cache()
        path = self.get_ytdlp_path()
        try:
            r = subprocess.run(
                [path, "--update-to", channel],
                capture_output=True, text=True, timeout=120,
                encoding="utf-8", errors="replace", **_POPEN_KWARGS,
            )
        finally:
            self.invalidate_cache()
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        if r.returncode != 0:
            raise RuntimeError(out or f"yt-dlp --update-to {channel} failed (code {r.returncode})")
        return out

    def install_deno(self, on_progress=None) -> None:
        """Downloads a portable Deno binary and extracts it to bin_dir."""
        self.invalidate_cache()
        url = self._deno_url()
        zip_path = self.bin_dir / "deno.zip"
        target = self.deno_local
        try:
            self._download_to_file(
                url, zip_path, timeout=180,
                on_progress=(lambda w, t: on_progress(w, t)) if on_progress else None,
            )
            with zipfile.ZipFile(zip_path, "r") as zf:
                found = False
                for member in zf.namelist():
                    base = member.lower().rstrip("/").rsplit("/", 1)[-1]
                    if base in ("deno", "deno.exe") and not member.endswith("/"):
                        with zf.open(member) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst, length=DOWNLOAD_CHUNK)
                        found = True
                        break
                if not found:
                    raise RuntimeError("deno not found in the ZIP")
        finally:
            zip_path.unlink(missing_ok=True)
        if not self.is_windows:
            try:
                os.chmod(target, 0o755)
            except Exception:
                pass
        self.invalidate_cache()


# ============================== main app ==========================================

class VideoToolApp:

    def __init__(self, page: ft.Page):
        self.page = page
        self.binaries = BinaryManager()
        self.config = ConfigManager(self.binaries.app_dir)

        # process handles
        self.download_process: subprocess.Popen | None = None
        self.convert_process: subprocess.Popen | None = None

        # log buffers
        self.download_log_lines: deque[str] = deque(maxlen=MAX_LOG)
        self.convert_log_lines: deque[str] = deque(maxlen=MAX_LOG)

        # synchronization
        self.download_lock = threading.Lock()
        self.convert_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._dl_running = False
        self._cv_running = False
        self._dl_cancelled = False
        self._cv_cancelled = False
        self._refresh_queued = False
        self._refresh_dirty = False
        self._last_refresh = 0.0
        self._dl_scroll_pend = False
        self._cv_scroll_pend = False

        # overlay tracking (avoid SnackBar leak)
        self._active_snackbars: list[ft.SnackBar] = []
        # currently open dialog (for clean close via page.close)
        self._open_dialog: ft.AlertDialog | None = None

        # binary status - placeholders, real values come from the
        # background probe (see _initial_binary_probe). The four
        # subprocess calls (yt-dlp --version, ffmpeg -version,
        # ffmpeg -encoders, yt-dlp --list-impersonate-targets) used to
        # block the window for ~2-6 s because yt-dlp is a PyInstaller
        # bundle with a slow cold start - and they also used to run
        # twice (once here, once in refresh_binary_status). Now the
        # window appears immediately, the chips/footer fill in after
        # the first frame.
        self.ytdlp_ok = False
        self.ytdlp_version = "Checking ..."
        self.ffmpeg_ok = False
        self.ffmpeg_version = "Checking ..."
        self.hw_backend = "cpu"
        self.impersonate_ok = False
        self.js_runtime = ""

        # self-update state (tool version, not yt-dlp/FFmpeg)
        self.tool_update_available = False
        self.tool_update_version = ""
        self.tool_update_source = ""  # downloaded video_tool.py, ready to install

        self._init_page()
        self._build_ui()
        self._refresh(immediate=True)

        # Prefill the URL field (clipboard URL > last used URL) right
        # after the first frame.
        try:
            self.page.run_task(self._prefill_url_async)
        except Exception:
            pass

        # Start the background probe as soon as the UI is visible.
        # page.run_thread runs on the same worker pool as the install
        # workers, so refresh_binary_status is thread-safe enough
        # (it only does property sets and ctrl.update()).
        try:
            self.page.run_thread(self._initial_binary_probe)
        except Exception:
            # Fallback for very old Flet versions without run_thread.
            threading.Thread(
                target=self._initial_binary_probe, daemon=True,
            ).start()

        # Self-update check (tool version, not yt-dlp/FFmpeg) - also
        # deferred to a background thread so it never blocks startup.
        try:
            self.page.run_thread(self._check_tool_update_worker, False)
        except Exception:
            threading.Thread(
                target=self._check_tool_update_worker, args=(False,), daemon=True,
            ).start()

    def _initial_binary_probe(self) -> None:
        """Detects installed binaries in the background.

        Runs *after* the first frame, so startup isn't blocked by
        yt-dlp's slow cold start (PyInstaller bundle).
        """
        try:
            self.refresh_binary_status(update_page=True)
        except Exception:
            pass

    # ========================= refresh engine =====================================

    async def _refresh_task(self) -> None:
        try:
            # Real throttling: if the last refresh was less than
            # REFRESH_MIN_INTERVAL ago, wait out the remainder. We used
            # to just 'await asyncio.sleep(0)' - that was effectively a
            # tight loop with many consecutive _refresh() calls.
            elapsed = time.monotonic() - self._last_refresh
            if elapsed < REFRESH_MIN_INTERVAL:
                await asyncio.sleep(REFRESH_MIN_INTERVAL - elapsed)
            else:
                await asyncio.sleep(0)

            if self._dl_scroll_pend and hasattr(self, "download_log"):
                try:
                    await self.download_log.scroll_to(offset=-1, duration=0)
                except Exception:
                    pass
                self._dl_scroll_pend = False
            if self._cv_scroll_pend and hasattr(self, "convert_log"):
                try:
                    await self.convert_log.scroll_to(offset=-1, duration=0)
                except Exception:
                    pass
                self._cv_scroll_pend = False
            try:
                self.page.update()
            except Exception:
                pass
            self._last_refresh = time.monotonic()
        finally:
            rerun = False
            with self._refresh_lock:
                if self._refresh_dirty:
                    self._refresh_dirty = False
                    rerun = True
                else:
                    self._refresh_queued = False
            if rerun:
                try:
                    self.page.run_task(self._refresh_task)
                except Exception:
                    with self._refresh_lock:
                        self._refresh_queued = False

    def _refresh(self, immediate: bool = False) -> None:
        if immediate:
            try:
                self.page.update()
                self._last_refresh = time.monotonic()
            except Exception:
                pass
            return

        with self._refresh_lock:
            if self._refresh_queued:
                self._refresh_dirty = True
                return
            self._refresh_queued = True
            self._refresh_dirty = False
        try:
            self.page.run_task(self._refresh_task)
        except Exception:
            with self._refresh_lock:
                self._refresh_queued = False
            try:
                self.page.update()
            except Exception:
                pass

    # ========================= page init ==========================================

    def _init_page(self) -> None:
        self.page.title = f"{APP_NAME} v{VERSION}"
        self.page.theme_mode = ft.ThemeMode.DARK
        self.page.padding = 0
        self.page.window.width = 1220
        self.page.window.height = 840
        self.page.window.min_width = 960
        self.page.window.min_height = 700
        self.page.theme = ft.Theme(color_scheme_seed=ACCENT_PRIMARY)
        self.page.window.on_event = self._on_window_event

    def _on_window_event(self, e) -> None:
        if e.data == "close":
            self._cleanup_processes()

    def _cleanup_processes(self) -> None:
        with self.download_lock:
            dl_proc = self.download_process
            self.download_process = None
            self._dl_cancelled = True
        with self.convert_lock:
            cv_proc = self.convert_process
            self.convert_process = None
            self._cv_cancelled = True
        _safe_terminate(dl_proc)
        _safe_terminate(cv_proc)

    # ========================= toast ==============================================

    def _toast(self, message: str, error: bool = False) -> None:
        self.page.run_task(self._toast_async, message, error)

    async def _toast_async(self, message: str, error: bool) -> None:
        bar = ft.SnackBar(
            content=ft.Text(message, color=ft.Colors.WHITE),
            bgcolor=ft.Colors.RED_800 if error else ft.Colors.GREEN_800,
            open=True,
            duration=2800,
        )
        self.page.overlay.append(bar)
        self._active_snackbars.append(bar)
        try:
            self.page.update()
        except Exception:
            pass
        await asyncio.sleep(3.2)
        try:
            if bar in self.page.overlay:
                self.page.overlay.remove(bar)
            if bar in self._active_snackbars:
                self._active_snackbars.remove(bar)
            self.page.update()
        except Exception:
            pass

    # ========================= dialogs ============================================

    def _close_dialog(self, e=None) -> None:
        dlg = getattr(self, "_open_dialog", None)
        # Prefers page.close (Flet 0.28+), falls back for older releases.
        for closer in (
            (lambda: self.page.close(dlg)) if dlg is not None else None,
            lambda: self.page.pop_dialog(),
            lambda: self.page.close_dialog(),
        ):
            if closer is None:
                continue
            try:
                closer()
                break
            except Exception:
                continue
        self._open_dialog = None
        self._refresh(immediate=True)

    # ========================= log helpers ========================================

    def _append_log(self, target: str, message: str) -> None:
        with self._log_lock:
            if target == "download":
                lines = self.download_log_lines
                control = self.download_log
                self._dl_scroll_pend = True
            else:
                lines = self.convert_log_lines
                control = self.convert_log
                self._cv_scroll_pend = True

            for line in message.replace("\r", "").split("\n"):
                lines.append(line)
                control.controls.append(ft.Text(
                    line, size=11, font_family="Consolas",
                    color=log_color(line), no_wrap=False,
                ))

            excess = len(control.controls) - MAX_LOG
            if excess > 0:
                del control.controls[:excess]

        self._refresh()

    def _clear_log(self, target: str) -> None:
        with self._log_lock:
            if target == "download":
                self.download_log_lines.clear()
                self.download_log.controls.clear()
            else:
                self.convert_log_lines.clear()
                self.convert_log.controls.clear()
        self._refresh(immediate=True)

    # ========================= busy state =========================================

    def _set_download_busy(self, busy: bool) -> None:
        self.download_start_btn.disabled = busy
        self.download_stop_btn.disabled = not busy
        self.download_progress.visible = busy
        if not busy:
            self.download_progress.value = 0

    def _set_convert_busy(self, busy: bool) -> None:
        self.convert_start_btn.disabled = busy
        self.convert_stop_btn.disabled = not busy
        self.convert_progress.visible = busy
        if not busy:
            self.convert_progress.value = 0

    # ========================= UI build ===========================================

    def _build_ui(self) -> None:
        self.output_picker = ft.FilePicker()
        self.input_picker = ft.FilePicker()
        self.cookies_picker = ft.FilePicker()
        self.clipboard = ft.Clipboard()
        self.page.services.extend([
            self.output_picker, self.input_picker,
            self.cookies_picker, self.clipboard,
        ])

        self.download_tab = self._build_download_tab()
        self.convert_tab = self._build_convert_tab()
        self.setup_tab = self._build_setup_tab()
        self.info_tab = self._build_info_tab()

        self.tab_views = [self.download_tab, self.convert_tab, self.setup_tab, self.info_tab]
        for i, v in enumerate(self.tab_views):
            v.visible = i == 0

        # ---- Modern NavigationRail with active indicator ----
        nav_items = [
            (ft.Icons.DOWNLOAD, "Download"),
            (ft.Icons.MOVIE_EDIT, "Convert"),
            (ft.Icons.BUILD, "Setup"),
            (ft.Icons.INFO, "Info"),
        ]
        self.nav_buttons: list[ft.Container] = []
        for idx, (icon, label) in enumerate(nav_items):
            self.nav_buttons.append(self._make_nav_button(idx, icon, label))

        nav_rail = ft.Container(
            width=178,
            bgcolor=ft.Colors.with_opacity(0.55, ft.Colors.BLACK),
            padding=ft.Padding(left=10, right=10, top=18, bottom=18),
            content=ft.Column(
                controls=self.nav_buttons, spacing=6,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
        )

        # ---- Status chips in header ----
        # We keep the Text instance and only update .value / .color,
        # so Flet reliably picks up the diff. Previously chip_*.content
        # was replaced wholesale - in some Flet versions that didn't
        # trigger a re-render and the user kept seeing the old version
        # after a yt-dlp update.
        self.chip_ytdlp_text = ft.Text(
            "yt-dlp: ...", size=11, color=ft.Colors.WHITE,
        )
        self.chip_ffmpeg_text = ft.Text(
            "FFmpeg: ...", size=11, color=ft.Colors.WHITE,
        )
        self.chip_ytdlp = ft.Container(
            border_radius=14, padding=ft.Padding(left=10, right=10, top=6, bottom=6),
            content=self.chip_ytdlp_text,
        )
        self.chip_ffmpeg = ft.Container(
            border_radius=14, padding=ft.Padding(left=10, right=10, top=6, bottom=6),
            content=self.chip_ffmpeg_text,
        )

        # ---- Self-update chip (hidden until a newer release is found) ----
        self.update_chip_text = ft.Text(
            "Update available", size=11, color=ft.Colors.WHITE, weight=ft.FontWeight.W_600,
        )
        self.update_chip = ft.Container(
            visible=self.tool_update_available,
            border_radius=14, padding=ft.Padding(left=10, right=10, top=6, bottom=6),
            bgcolor=ft.Colors.AMBER_700,
            tooltip="Click to install the update",
            on_click=self._show_update_dialog,
            content=ft.Row(spacing=4, controls=[
                ft.Icon(ft.Icons.SYSTEM_UPDATE, size=14, color=ft.Colors.WHITE),
                self.update_chip_text,
            ]),
        )

        # ---- Gradient header ----
        header = ft.Container(
            height=58,
            padding=ft.Padding(left=22, right=22, top=0, bottom=0),
            gradient=ft.LinearGradient(
                begin=ft.Alignment(-1, 0), end=ft.Alignment(1, 0),
                colors=[
                    ft.Colors.with_opacity(0.55, ft.Colors.INDIGO_900),
                    ft.Colors.with_opacity(0.45, ft.Colors.CYAN_900),
                ],
            ),
            content=ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Row(spacing=12, controls=[
                        ft.Icon(ft.Icons.MOVIE_FILTER, color=ACCENT_PRIMARY, size=26),
                        ft.Text(APP_NAME, size=19, weight=ft.FontWeight.BOLD),
                        ft.Container(
                            bgcolor=ft.Colors.with_opacity(0.30, ACCENT_PRIMARY),
                            border_radius=8,
                            padding=ft.Padding(left=8, right=8, top=3, bottom=3),
                            content=ft.Text(f"v{VERSION}", size=11, color=ft.Colors.CYAN_50),
                        ),
                    ]),
                    ft.Row(spacing=10, controls=[
                        self.chip_ytdlp,
                        self.chip_ffmpeg,
                        self.update_chip,
                    ]),
                ],
            ),
        )

        # ---- Status footer ----
        hw_label = {"nvidia": "NVIDIA NVENC", "amd": "AMD AMF", "intel": "Intel QSV", "cpu": "CPU"}
        impersonate_txt = "on" if self.impersonate_ok else "off"
        # Keep references - updated by the background probe (see
        # refresh_binary_status) once HW detection and the impersonate
        # check are done.
        self.footer_hw_text = ft.Text(
            f"HW: {hw_label.get(self.hw_backend, 'CPU')}",
            size=11, color=ft.Colors.GREY_400,
        )
        self.footer_impersonate_text = ft.Text(
            f"Impersonate: {impersonate_txt}",
            size=11, color=ft.Colors.GREY_400,
        )
        self.footer_js_text = ft.Text(
            "JS: -",
            size=11, color=ft.Colors.GREY_400,
        )
        self.footer = ft.Container(
            height=28,
            padding=ft.Padding(left=18, right=18, top=0, bottom=0),
            bgcolor=ft.Colors.with_opacity(0.35, ft.Colors.BLACK),
            content=ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Row(spacing=14, controls=[
                        self.footer_hw_text,
                        self.footer_impersonate_text,
                        self.footer_js_text,
                        ft.Text(
                            f"Platform: {platform.system()} {platform.machine()}",
                            size=11, color=ft.Colors.GREY_400,
                        ),
                    ]),
                    ft.Text(
                        f"{APP_NAME} v{VERSION}", size=11, color=ft.Colors.GREY_500,
                    ),
                ],
            ),
        )

        content = ft.Container(
            content=ft.Stack(expand=True, controls=self.tab_views),
            expand=True,
            bgcolor=SURFACE_BG,
        )

        self.page.add(ft.Column(
            expand=True, spacing=0,
            controls=[
                header,
                ft.Row(expand=True, spacing=0, controls=[nav_rail, content]),
                self.footer,
            ],
        ))

    def _make_nav_button(self, index: int, icon, label: str) -> ft.Container:
        active = index == 0
        bar = ft.Container(
            width=4, height=22, border_radius=2,
            bgcolor=ACCENT_PRIMARY if active else ft.Colors.TRANSPARENT,
        )
        row = ft.Row(
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                bar,
                ft.Icon(icon, size=18,
                        color=ACCENT_PRIMARY if active else ft.Colors.GREY_400),
                ft.Text(label, size=13,
                        color=ft.Colors.WHITE if active else ft.Colors.GREY_400,
                        weight=ft.FontWeight.W_500),
            ],
        )
        cont = ft.Container(
            padding=ft.Padding(left=4, right=10, top=10, bottom=10),
            border_radius=10,
            bgcolor=ft.Colors.with_opacity(0.10, ACCENT_PRIMARY) if active else ft.Colors.TRANSPARENT,
            on_click=lambda e, i=index: self.change_tab(i),
            content=row,
        )
        cont.data = {"bar": bar, "icon": row.controls[1], "text": row.controls[2]}
        return cont

    # ========================= Download Tab =======================================

    def _build_download_tab(self):
        saved_folder = self.config.get("last_output_folder", str(Path.home() / "Downloads"))

        self.download_url = ft.TextField(
            label="Video URL",
            hint_text="YouTube, TikTok, X/Twitter, Instagram ...",
            prefix_icon=ft.Icons.LINK,
            border_radius=10, expand=True,
            on_submit=self.start_download,
        )
        paste_btn = ft.IconButton(
            icon=ft.Icons.CONTENT_PASTE,
            tooltip="Paste URL from clipboard",
            on_click=self._paste_url,
        )

        self.download_output = ft.TextField(
            label="Save Location", value=saved_folder,
            prefix_icon=ft.Icons.FOLDER,
            read_only=True, expand=True, border_radius=10,
        )
        folder_btn = ft.IconButton(
            icon=ft.Icons.FOLDER_OPEN,
            tooltip="Choose folder",
            on_click=self.pick_output_folder,
        )
        self.open_folder_btn = ft.IconButton(
            icon=ft.Icons.OPEN_IN_NEW,
            tooltip="Open folder",
            on_click=lambda e: open_folder(self.download_output.value or ""),
        )

        self.download_quality = ft.Dropdown(
            label="Quality",
            value=self.config.get("last_quality", "best"),
            width=270, border_radius=10,
            options=[
                ft.dropdown.Option("best", "Best Quality (H.264)"),
                ft.dropdown.Option("best_av1", "Best Quality (AV1 preferred)"),
                ft.dropdown.Option("2160", "4K (2160p)"),
                ft.dropdown.Option("1440", "1440p"),
                ft.dropdown.Option("1080", "1080p"),
                ft.dropdown.Option("720", "720p"),
                ft.dropdown.Option("480", "480p"),
                ft.dropdown.Option("audio_wav", "Audio Only (WAV - Vegas/NLE)"),
                ft.dropdown.Option("audio", "Audio Only (MP3 320k)"),
                ft.dropdown.Option("audio_opus", "Audio Only (Opus, small)"),
            ],
            on_select=lambda e: self.config.set("last_quality", e.control.value),
        )

        saved_cookies = self.config.get("last_cookies", "none")
        self.download_cookies = ft.Dropdown(
            label="Cookies (403 + Age-Restricted)",
            value=saved_cookies, width=260, border_radius=10,
            tooltip=(
                "For 403 errors and age-restricted (18+) videos.\n"
                "Windows note: Chrome/Edge/Brave can NOT be read directly "
                "because of App-Bound Encryption - use the 'Cookies file "
                "(cookies.txt)' option there instead. Firefox works directly."
            ),
            options=[
                ft.dropdown.Option("none", "None (default)"),
                ft.dropdown.Option("firefox", "Firefox (recommended on Windows)"),
                ft.dropdown.Option("cookiefile", "Cookies File (cookies.txt)"),
                ft.dropdown.Option("chrome", "Chrome (often blocked/Windows)"),
                ft.dropdown.Option("edge", "Edge (often blocked/Windows)"),
                ft.dropdown.Option("brave", "Brave (often blocked/Windows)"),
                ft.dropdown.Option("safari", "Safari (macOS)"),
            ],
            on_select=self._on_cookies_change,
        )
        self._cookies_browser = (
            None if saved_cookies in ("none", "cookiefile") else saved_cookies
        )

        # cookies.txt file path (only visible when "cookiefile" is selected).
        # Browser-independent approach - needed for Chromium browsers on
        # Windows (App-Bound Encryption) and the most robust for 18+ videos.
        self.cookies_file = ft.TextField(
            label="cookies.txt Path",
            value=self.config.get("cookies_file", ""),
            read_only=True, expand=True, border_radius=10,
            prefix_icon=ft.Icons.COOKIE,
            hint_text="Export cookies.txt from your logged-in browser",
            on_change=lambda e: self.config.set("cookies_file", e.control.value or ""),
        )
        cookies_file_btn = ft.IconButton(
            icon=ft.Icons.UPLOAD_FILE,
            tooltip="Choose cookies.txt",
            on_click=self.pick_cookies_file,
        )
        self.cookies_file_row = ft.Row(
            visible=(saved_cookies == "cookiefile"),
            controls=[self.cookies_file, cookies_file_btn],
        )

        # ---- new: impersonate / sponsorblock / embed toggles ----
        self.toggle_impersonate = ft.Switch(
            label="Impersonate (Anti-Bot)",
            value=bool(self.config.get("impersonate", self.impersonate_ok)),
            tooltip="Disguises yt-dlp as a real browser - helps with 403/anti-bot. Requires curl_cffi.",
            on_change=lambda e: self.config.set("impersonate", e.control.value),
            disabled=not self.impersonate_ok,
        )
        self.toggle_sponsorblock = ft.Switch(
            label="SponsorBlock (Remove Sponsors)",
            value=bool(self.config.get("sponsorblock", False)),
            tooltip="Automatically removes sponsor segments (YouTube only).",
            on_change=lambda e: self.config.set("sponsorblock", e.control.value),
        )
        self.toggle_embed = ft.Switch(
            label="Embed Thumbnail/Metadata/Chapters",
            value=bool(self.config.get("embed", True)),
            tooltip="Embed thumbnail, metadata, and chapters directly into the file.",
            on_change=lambda e: self.config.set("embed", e.control.value),
        )
        self.toggle_subs = ft.Switch(
            label="Download Subtitles",
            value=bool(self.config.get("subs", False)),
            tooltip="Download subtitles (auto-generated or manual) and embed them.",
            on_change=lambda e: self.config.set("subs", e.control.value),
        )
        self.subs_lang = ft.TextField(
            label="Languages (comma-separated)",
            value=self.config.get("subs_lang", "en,de"),
            width=200, border_radius=10,
            hint_text="e.g. en,de,fr",
            on_change=lambda e: self.config.set("subs_lang", e.control.value),
        )
        self.toggle_potoken = ft.Switch(
            label="PO Token / mweb (for 18+ Videos)",
            value=bool(self.config.get("potoken", False)),
            tooltip=("Uses the mweb client + a PO token provider plugin (bgutil) "
                     "for age-restricted videos. Requires the plugin to be "
                     "installed and the provider to be reachable."),
            on_change=lambda e: self.config.set("potoken", e.control.value),
        )
        self.potoken_url = ft.TextField(
            label="PO Token Provider URL (optional)",
            value=self.config.get("potoken_url", ""),
            width=320, border_radius=10,
            hint_text="empty = http://127.0.0.1:4416",
            on_change=lambda e: self.config.set("potoken_url", e.control.value),
        )

        self.download_progress = ft.ProgressBar(
            value=0, visible=False, color=ACCENT_PRIMARY, height=6,
        )
        self.download_status = ft.Text("Ready", size=12, color=ft.Colors.CYAN_100)

        self.download_log = ft.ListView(
            expand=False, spacing=1,
            auto_scroll=True, scroll=ft.ScrollMode.AUTO,
        )
        self.download_log_lines.clear()
        for line in [f"  {APP_NAME} v{VERSION}", "  " + "-" * 36, "", "  Ready for downloads ..."]:
            self.download_log_lines.append(line)
            self.download_log.controls.append(
                ft.Text(line, size=11, font_family="Consolas", color=_COL_DEFAULT)
            )

        log_panel = ft.Container(
            content=self.download_log, height=210,
            border=ft.Border.all(1, BORDER_FAINT),
            border_radius=10, padding=10,
            bgcolor=ft.Colors.with_opacity(0.35, ft.Colors.BLACK),
        )

        self.download_start_btn = ft.FilledButton(
            "Start Download", icon=ft.Icons.DOWNLOAD, height=44,
            on_click=self.start_download,
        )
        self.download_stop_btn = ft.FilledButton(
            "Stop", icon=ft.Icons.STOP, height=44,
            on_click=self.stop_download, disabled=True,
            style=ft.ButtonStyle(bgcolor=ft.Colors.RED_700),
        )
        clear_log_btn = ft.IconButton(
            icon=ft.Icons.DELETE_SWEEP, icon_size=18,
            tooltip="Clear log",
            on_click=lambda e: self._clear_log("download"),
        )

        # ---- Advanced options: collapsed by default to keep the tab tidy ----
        self._adv_more_icon = ft.Icon(
            ft.Icons.EXPAND_MORE, size=18, color=ft.Colors.GREY_400,
        )
        self._adv_less_icon = ft.Icon(
            ft.Icons.EXPAND_LESS, size=18, color=ft.Colors.GREY_400, visible=False,
        )
        self.advanced_panel = ft.Container(
            visible=False,
            padding=ft.Padding(left=2, right=2, top=4, bottom=0),
            content=ft.Column(spacing=10, controls=[
                ft.Row(spacing=24, wrap=True, controls=[
                    self.toggle_impersonate,
                    self.toggle_sponsorblock,
                    self.toggle_embed,
                ]),
                ft.Row(
                    spacing=24, wrap=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[self.toggle_subs, self.subs_lang],
                ),
                ft.Row(
                    spacing=24, wrap=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[self.toggle_potoken, self.potoken_url],
                ),
            ]),
        )
        advanced_toggle = ft.Container(
            on_click=self._toggle_advanced,
            border_radius=8,
            padding=ft.Padding(left=4, right=8, top=6, bottom=6),
            content=ft.Row(spacing=6, controls=[
                ft.Icon(ft.Icons.TUNE, size=16, color=ft.Colors.GREY_400),
                ft.Text("Advanced options", size=12,
                        color=ft.Colors.GREY_300, weight=ft.FontWeight.W_500),
                self._adv_more_icon,
                self._adv_less_icon,
            ]),
        )

        source_card = self._section_card(
            ft.Icons.LINK, "Source & Quality",
            ft.Row(controls=[self.download_url, paste_btn]),
            ft.Row(spacing=12, wrap=True,
                   controls=[self.download_quality, self.download_cookies]),
            self.cookies_file_row,
            advanced_toggle,
            self.advanced_panel,
        )
        dest_card = self._section_card(
            ft.Icons.FOLDER, "Save Location",
            ft.Row(controls=[self.download_output, folder_btn, self.open_folder_btn]),
        )
        action_row = ft.Row(
            spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                self.download_start_btn,
                self.download_stop_btn,
                ft.Container(width=8),
                ft.Column(spacing=5, expand=True, controls=[
                    self.download_status,
                    self.download_progress,
                ]),
            ],
        )
        log_card = self._section_card(
            ft.Icons.TERMINAL, "Live Log",
            log_panel,
            trailing=clear_log_btn,
        )

        return ft.Container(
            expand=True,
            padding=ft.Padding(left=28, right=28, top=20, bottom=20),
            content=ft.Column(expand=True, spacing=14, scroll=ft.ScrollMode.AUTO, controls=[
                ft.Column(spacing=2, controls=[
                    ft.Text("Download", size=22, weight=ft.FontWeight.BOLD),
                    ft.Text(
                        "Videos & audio from YouTube, Instagram, TikTok, X and 1700+ sites",
                        color=ft.Colors.GREY_500, size=12,
                    ),
                ]),
                source_card,
                dest_card,
                action_row,
                log_card,
            ]),
        )

    def _section_card(self, icon, title: str, *controls, trailing=None) -> ft.Container:
        """Uniform section container - keeps the tabs visually consistent."""
        header = ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Row(spacing=8, controls=[
                    ft.Icon(icon, size=16, color=ACCENT_PRIMARY),
                    ft.Text(title, size=13, weight=ft.FontWeight.W_600,
                            color=ft.Colors.GREY_200),
                ]),
            ],
        )
        if trailing is not None:
            header.controls.append(trailing)
        return ft.Container(
            bgcolor=SURFACE_PANEL,
            border=ft.Border.all(1, BORDER_FAINT),
            border_radius=14,
            padding=ft.Padding(left=16, right=16, top=14, bottom=14),
            content=ft.Column(spacing=12, controls=[header, *controls]),
        )

    def _toggle_advanced(self, e=None) -> None:
        expanded = not self.advanced_panel.visible
        self.advanced_panel.visible = expanded
        self._adv_more_icon.visible = not expanded
        self._adv_less_icon.visible = expanded
        self._refresh(immediate=True)

    # ========================= Convert Tab ========================================

    def _build_convert_tab(self):
        self.convert_input = ft.TextField(
            label="Input File", prefix_icon=ft.Icons.VIDEO_FILE,
            read_only=True, expand=True, border_radius=10,
        )
        self.convert_output = ft.TextField(
            label="Output File", prefix_icon=ft.Icons.SAVE,
            expand=True, border_radius=10,
        )
        self.codec_category = ft.Dropdown(
            label="Category", value="editing", expand=True, border_radius=10,
            options=[
                ft.dropdown.Option("standard", "Standard"),
                ft.dropdown.Option("editing", "Editing"),
                ft.dropdown.Option("delivery", "Delivery"),
            ],
            on_select=self.on_category_change,
        )
        self.codec_select = ft.Dropdown(
            label="Codec", value=CODEC_OPTIONS["editing"][0][0],
            expand=True, border_radius=10,
            options=[ft.dropdown.Option(k, l) for k, l in CODEC_OPTIONS["editing"]],
            on_select=self.on_codec_change,
        )
        self.hw_select = ft.Dropdown(
            label="Hardware", value="auto", expand=True, border_radius=10,
            options=[
                ft.dropdown.Option("auto", "Auto"),
                ft.dropdown.Option("nvidia", "NVIDIA NVENC"),
                ft.dropdown.Option("amd", "AMD AMF"),
                ft.dropdown.Option("intel", "Intel QSV"),
                ft.dropdown.Option("cpu", "CPU"),
            ],
        )
        self.bitrate_mode = ft.Dropdown(
            label="Bitrate Mode", value="crf", expand=True, border_radius=10,
            options=[
                ft.dropdown.Option("crf", "CRF / CQ"),
                ft.dropdown.Option("custom", "Custom Bitrate"),
            ],
            on_select=self.on_bitrate_mode_change,
        )
        self.hw_hint = ft.Text("", size=11, color=ft.Colors.ORANGE_300)

        self.crf_slider = ft.Slider(
            min=15, max=30, value=20, divisions=15, label="{value}",
            on_change=self.on_crf_change,
        )
        self.crf_label = ft.Text("CRF: 20", size=12)

        self.bitrate_slider = ft.Slider(
            min=2, max=200, value=20, divisions=198, label="{value} Mbps",
            on_change=self.on_custom_bitrate_change,
        )
        self.bitrate_label = ft.Text("Video: 20 Mbps", size=12)

        self.crf_container = ft.Column(
            controls=[ft.Text("Quality", size=12), self.crf_slider, self.crf_label],
            spacing=4,
        )
        self.custom_bitrate_container = ft.Column(
            visible=False, spacing=4,
            controls=[
                ft.Text("Custom Bitrate", size=12),
                self.bitrate_slider,
                self.bitrate_label,
                ft.Row(spacing=6, controls=[
                    ft.TextButton("8M", on_click=lambda e: self.set_custom_bitrate(8)),
                    ft.TextButton("20M", on_click=lambda e: self.set_custom_bitrate(20)),
                    ft.TextButton("50M", on_click=lambda e: self.set_custom_bitrate(50)),
                    ft.TextButton("100M", on_click=lambda e: self.set_custom_bitrate(100)),
                ]),
            ],
        )

        # Color metadata toggle (preserves BT.709/2020)
        self.toggle_preserve_color = ft.Switch(
            label="Preserve Color Metadata (BT.709 / BT.2020)",
            value=True,
            tooltip="Keeps color primaries/transfer/space from the source (important for HDR).",
        )

        self.convert_progress = ft.ProgressBar(
            value=0, visible=False, color=ACCENT_PRIMARY, height=6,
        )
        self.convert_status = ft.Text("Ready", size=12, color=ft.Colors.CYAN_100)

        self.convert_log = ft.ListView(
            expand=False, spacing=1,
            auto_scroll=True, scroll=ft.ScrollMode.AUTO,
        )
        self.convert_log_lines.clear()
        self.convert_log_lines.append("  Ready for conversion ...")
        self.convert_log.controls.append(
            ft.Text("  Ready for conversion ...", size=11, font_family="Consolas", color=_COL_DEFAULT)
        )

        log_panel = ft.Container(
            content=self.convert_log, height=240,
            border=ft.Border.all(1, BORDER_SOFT),
            border_radius=10, padding=8,
            bgcolor=SURFACE_PANEL,
        )

        self.convert_start_btn = ft.FilledButton(
            "Convert", icon=ft.Icons.PLAY_ARROW,
            on_click=self.start_conversion,
        )
        self.convert_stop_btn = ft.FilledButton(
            "Stop", icon=ft.Icons.STOP, disabled=True,
            on_click=self.stop_conversion,
            style=ft.ButtonStyle(bgcolor=ft.Colors.RED_700),
        )
        clear_log_btn = ft.TextButton(
            "Clear Log", icon=ft.Icons.DELETE_SWEEP,
            on_click=lambda e: self._clear_log("convert"),
            style=ft.ButtonStyle(color=ft.Colors.GREY_500),
        )

        return ft.Container(
            expand=True, padding=24,
            content=ft.Column(expand=True, spacing=12, scroll=ft.ScrollMode.AUTO, controls=[
                ft.Column(spacing=2, controls=[
                    ft.Text("Convert", size=22, weight=ft.FontWeight.BOLD),
                    ft.Text("FFmpeg conversion with live progress",
                            color=ft.Colors.GREY_500, size=12),
                ]),
                ft.Row(controls=[
                    self.convert_input,
                    ft.IconButton(icon=ft.Icons.FILE_OPEN, tooltip="Choose file", on_click=self.pick_input_file),
                ]),
                self.convert_output,
                ft.Card(content=ft.Container(padding=14, content=ft.Column(spacing=10, controls=[
                    ft.Row(controls=[self.codec_category, self.codec_select], spacing=10),
                    ft.Row(controls=[self.hw_select, self.bitrate_mode], spacing=10),
                    self.hw_hint,
                    self.crf_container,
                    self.custom_bitrate_container,
                    ft.Divider(height=1, color=BORDER_FAINT),
                    self.toggle_preserve_color,
                ]))),
                ft.Row(spacing=10, controls=[
                    self.convert_start_btn,
                    self.convert_stop_btn,
                    ft.Container(expand=True),
                    clear_log_btn,
                ]),
                self.convert_progress,
                self.convert_status,
                ft.Text("Live Log", weight=ft.FontWeight.BOLD, size=13, color=ft.Colors.GREY_300),
                log_panel,
            ]),
        )

    # ========================= Setup Tab ==========================================

    def _build_setup_tab(self):
        # Simple Rows instead of ListTile -> subtitle texts re-render more
        # reliably with in-place .value changes.
        self.setup_ytdlp_status = ft.Text("-", size=12, color=ft.Colors.GREY_300)
        self.setup_ffmpeg_status = ft.Text("-", size=12, color=ft.Colors.GREY_300)
        self.setup_deno_status = ft.Text("-", size=12, color=ft.Colors.GREY_300)
        self.setup_path_text = ft.Text(
            str(self.binaries.bin_dir), size=11, color=ft.Colors.GREY_400,
        )
        self.setup_log = ft.Text("", size=12)

        self.ytdlp_channel = ft.Dropdown(
            label="yt-dlp Channel",
            value=self.config.get("ytdlp_channel", "stable"),
            width=230, border_radius=10,
            tooltip=(
                "Site-specific bug fixes (e.g. the Instagram \"empty media "
                "response\" error, yt-dlp #17074) often land in Nightly/Master "
                "days or weeks before the next Stable release. If a download "
                "fails with a known extractor bug, switch the channel here "
                "and click 'Update yt-dlp'."
            ),
            options=[
                ft.dropdown.Option("stable", "Stable (recommended)"),
                ft.dropdown.Option("nightly", "Nightly (latest fixes)"),
                ft.dropdown.Option("master", "Master (bleeding edge)"),
            ],
            on_select=lambda e: self.config.set("ytdlp_channel", e.control.value),
        )

        def _bin_row(icon, label: str, status: ft.Text) -> ft.Row:
            return ft.Row(
                spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(icon, color=ACCENT_PRIMARY),
                    ft.Column(spacing=2, expand=True, controls=[
                        ft.Text(label, size=13, weight=ft.FontWeight.W_500),
                        status,
                    ]),
                ],
            )

        return ft.Container(
            expand=True, padding=24,
            content=ft.Column(spacing=14, controls=[
                ft.Column(spacing=2, controls=[
                    ft.Text("Setup", size=22, weight=ft.FontWeight.BOLD),
                    ft.Text("Manage yt-dlp and FFmpeg",
                            color=ft.Colors.GREY_500, size=12),
                ]),
                ft.Card(content=ft.Container(
                    padding=14,
                    content=ft.Column(spacing=12, controls=[
                        _bin_row(ft.Icons.DOWNLOAD, "yt-dlp", self.setup_ytdlp_status),
                        ft.Divider(height=1, color=BORDER_FAINT),
                        _bin_row(ft.Icons.MOVIE, "FFmpeg", self.setup_ffmpeg_status),
                        ft.Divider(height=1, color=BORDER_FAINT),
                        _bin_row(ft.Icons.CODE, "Deno (JS Runtime for YouTube)", self.setup_deno_status),
                        ft.Divider(height=1, color=BORDER_FAINT),
                        _bin_row(ft.Icons.FOLDER, "Local Bin Path", self.setup_path_text),
                    ]),
                )),
                ft.Row(spacing=10, controls=[
                    ft.FilledButton(
                        "Install Binaries", icon=ft.Icons.DOWNLOAD,
                        on_click=self.install_binaries,
                    ),
                    self.ytdlp_channel,
                    ft.FilledButton(
                        "Update yt-dlp", icon=ft.Icons.UPDATE,
                        on_click=self.update_ytdlp,
                    ),
                    ft.FilledButton(
                        "Install Deno", icon=ft.Icons.CODE,
                        on_click=self.install_deno,
                    ),
                ]),
                self.setup_log,
            ]),
        )

    # ========================= Info Tab ===========================================

    def _build_info_tab(self):
        features = [
            ("Download", "YouTube, TikTok, Instagram, X/Twitter and 1700+ more sites"),
            ("Anti-Bot", "Optional --impersonate (curl_cffi) against 403/Cloudflare"),
            ("Vegas Pro", "H.264/AAC preferred - directly compatible with Vegas Pro 23+"),
            ("Audio", "MP3 (CBR 320k), WAV/PCM or Opus"),
            ("Quality", "4K, 1440p, 1080p, 720p, 480p - with AV1 preference"),
            ("SponsorBlock", "Automatically remove or mark sponsors"),
            ("Convert", "H.264, H.265, AV1 (SVT-AV1), ProRes 422, DNxHR, Vegas Sync Fix"),
            ("Hardware", "NVIDIA NVENC (multipass+lookahead), AMD AMF, Intel QSV, Auto-Detect"),
            ("HDR/Color", "Color metadata is preserved during conversion"),
            ("Settings", "Last save location and options are remembered"),
        ]
        rows = []
        for title, desc in features:
            rows.append(ft.Row(spacing=10, controls=[
                ft.Icon(ft.Icons.CHECK_CIRCLE, color=ft.Colors.GREEN_400, size=16),
                ft.Column(spacing=1, controls=[
                    ft.Text(title, size=13, weight=ft.FontWeight.BOLD),
                    ft.Text(desc, size=12, color=ft.Colors.GREY_400),
                ]),
            ]))

        return ft.Container(
            expand=True, padding=24,
            content=ft.Column(
                horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=0,
                controls=[
                    ft.Container(expand=True),
                    ft.Icon(ft.Icons.MOVIE_FILTER, size=72, color=ACCENT_PRIMARY),
                    ft.Container(height=8),
                    ft.Text(APP_NAME, size=32, weight=ft.FontWeight.BOLD),
                    ft.Text(f"Version {VERSION}", color=ft.Colors.GREY_400, size=14),
                    ft.Container(height=20),
                    ft.Card(content=ft.Container(
                        padding=ft.Padding(left=24, right=24, top=16, bottom=16),
                        width=600,
                        content=ft.Column(controls=rows, spacing=14),
                    )),
                    ft.Container(height=16),
                    ft.Row(
                        alignment=ft.MainAxisAlignment.CENTER, spacing=10,
                        controls=[
                            ft.OutlinedButton(
                                "Check for Updates", icon=ft.Icons.SYSTEM_UPDATE,
                                on_click=self.check_for_tool_update,
                                style=ft.ButtonStyle(side=ft.BorderSide(1, ACCENT_PRIMARY)),
                            ),
                        ],
                    ),
                    ft.Container(expand=True),
                ],
            ),
        )

    # ========================= tab navigation =====================================

    def change_tab(self, index: int) -> None:
        for i, v in enumerate(self.tab_views):
            v.visible = i == index
        for i, btn in enumerate(self.nav_buttons):
            active = i == index
            btn.bgcolor = ft.Colors.with_opacity(0.10, ACCENT_PRIMARY) if active else ft.Colors.TRANSPARENT
            data = btn.data or {}
            data["bar"].bgcolor = ACCENT_PRIMARY if active else ft.Colors.TRANSPARENT
            data["icon"].color = ACCENT_PRIMARY if active else ft.Colors.GREY_400
            data["text"].color = ft.Colors.WHITE if active else ft.Colors.GREY_400
        self._refresh(immediate=True)

    # ========================= binary status ======================================

    def refresh_binary_status(self, update_page: bool = True, retries: int = 0) -> None:
        # retries > 0 is worth it right after a replace-install, when the
        # new file may still be briefly locked by AV/SmartScreen.
        self.ytdlp_ok, self.ytdlp_version = self.binaries.check_ytdlp(retries=retries)
        self.ffmpeg_ok, self.ffmpeg_version = self.binaries.check_ffmpeg()
        if self.ffmpeg_ok:
            self.hw_backend = _detect_hw_encoder()
        if self.ytdlp_ok:
            self.impersonate_ok = _supports_impersonation(self.binaries.get_ytdlp_path())

        yt_txt = self.ytdlp_version if self.ytdlp_ok else "Not installed"
        ff_txt = self.ffmpeg_version if self.ffmpeg_ok else "Not installed"

        # In-place property updates - no control replacements, so Flet
        # reliably picks up the diff and the GUI actually shows the new
        # version after a yt-dlp update.
        changed: list = []

        if self.setup_ytdlp_status.value != yt_txt:
            self.setup_ytdlp_status.value = yt_txt
            changed.append(self.setup_ytdlp_status)
        if self.setup_ffmpeg_status.value != ff_txt:
            self.setup_ffmpeg_status.value = ff_txt
            changed.append(self.setup_ffmpeg_status)

        yt_chip_txt = f"yt-dlp: {yt_txt[:16] if self.ytdlp_ok else 'x'}"
        if self.chip_ytdlp_text.value != yt_chip_txt:
            self.chip_ytdlp_text.value = yt_chip_txt
            changed.append(self.chip_ytdlp_text)
        yt_chip_bg = ft.Colors.GREEN_900 if self.ytdlp_ok else ft.Colors.RED_900
        if self.chip_ytdlp.bgcolor != yt_chip_bg:
            self.chip_ytdlp.bgcolor = yt_chip_bg
            changed.append(self.chip_ytdlp)

        ff_chip_txt = f"FFmpeg: {ff_txt[:16] if self.ffmpeg_ok else 'x'}"
        if self.chip_ffmpeg_text.value != ff_chip_txt:
            self.chip_ffmpeg_text.value = ff_chip_txt
            changed.append(self.chip_ffmpeg_text)
        ff_chip_bg = ft.Colors.GREEN_900 if self.ffmpeg_ok else ft.Colors.RED_900
        if self.chip_ffmpeg.bgcolor != ff_chip_bg:
            self.chip_ffmpeg.bgcolor = ff_chip_bg
            changed.append(self.chip_ffmpeg)

        if hasattr(self, "toggle_impersonate"):
            new_disabled = not self.impersonate_ok
            if self.toggle_impersonate.disabled != new_disabled:
                self.toggle_impersonate.disabled = new_disabled
                changed.append(self.toggle_impersonate)
            if not self.impersonate_ok and self.toggle_impersonate.value:
                self.toggle_impersonate.value = False
                if self.toggle_impersonate not in changed:
                    changed.append(self.toggle_impersonate)

        # Footer (HW backend + impersonate status) - no longer set at
        # build time since the deferred probe, but updated here instead.
        if hasattr(self, "footer_hw_text"):
            hw_label = {"nvidia": "NVIDIA NVENC", "amd": "AMD AMF",
                        "intel": "Intel QSV", "cpu": "CPU"}
            new_hw = f"HW: {hw_label.get(self.hw_backend, 'CPU')}"
            if self.footer_hw_text.value != new_hw:
                self.footer_hw_text.value = new_hw
                changed.append(self.footer_hw_text)
        if hasattr(self, "footer_impersonate_text"):
            new_imp = f"Impersonate: {'on' if self.impersonate_ok else 'off'}"
            if self.footer_impersonate_text.value != new_imp:
                self.footer_impersonate_text.value = new_imp
                changed.append(self.footer_impersonate_text)
        deno_ok, deno_txt = self.binaries.check_deno()
        self.js_runtime = deno_txt.split()[0] if deno_ok else ""
        deno_status_txt = deno_txt if deno_ok else "Not installed"
        if hasattr(self, "setup_deno_status") and self.setup_deno_status.value != deno_status_txt:
            self.setup_deno_status.value = deno_status_txt
            changed.append(self.setup_deno_status)
        if hasattr(self, "footer_js_text"):
            new_js = f"JS: {deno_txt if deno_ok else '-'}"
            if self.footer_js_text.value != new_js:
                self.footer_js_text.value = new_js
                changed.append(self.footer_js_text)

        if update_page:
            # Per-control updates force the diff through even in cases
            # where page.update() didn't pick up everything in nested
            # containers.
            for ctrl in changed:
                try:
                    ctrl.update()
                except Exception:
                    pass
            self._refresh(immediate=True)

    # ========================= file pickers =======================================

    async def pick_output_folder(self, e) -> None:
        path = await self.output_picker.get_directory_path(dialog_title="Choose save folder")
        if path:
            self.download_output.value = path
            self.config.set("last_output_folder", path)
            self._refresh(immediate=True)

    async def pick_input_file(self, e) -> None:
        files = await self.input_picker.pick_files(
            dialog_title="Choose video file",
            allow_multiple=False,
            allowed_extensions=["mp4", "mkv", "avi", "mov", "webm", "flv", "wmv", "m4v", "ts"],
        )
        if files and files[0].path:
            inp = Path(files[0].path)
            self.convert_input.value = str(inp)
            self.convert_output.value = str(inp.parent / f"{inp.stem}_converted.mp4")
            self._refresh(immediate=True)

    def _paste_url(self, e) -> None:
        self.page.run_task(self._paste_url_async)

    async def _read_clipboard(self) -> str:
        """Reads the clipboard - Clipboard.get() is async in newer Flet
        versions and sync in older ones; handle both."""
        try:
            res = self.clipboard.get()
            if inspect.isawaitable(res):
                res = await res
            return (res or "").strip()
        except Exception:
            return ""

    async def _paste_url_async(self) -> None:
        text = await self._read_clipboard()
        if text:
            self.download_url.value = text
            self._refresh(immediate=True)

    async def _prefill_url_async(self) -> None:
        """Prefills the URL field on startup: a URL from the clipboard wins,
        otherwise the last URL used falls back in."""
        if (self.download_url.value or "").strip():
            return
        text = await self._read_clipboard()
        if re.match(r"^https?://\S+$", text or ""):
            self.download_url.value = text
            self._refresh(immediate=True)
            return
        last = (self.config.get("last_url") or "").strip()
        if last:
            self.download_url.value = last
            self._refresh(immediate=True)

    # ========================= codec / category events ============================

    def on_category_change(self, e) -> None:
        cat = self.codec_category.value or "editing"
        opts = CODEC_OPTIONS.get(cat, CODEC_OPTIONS["editing"])
        self.codec_select.options = [ft.dropdown.Option(k, l) for k, l in opts]
        self.codec_select.value = opts[0][0]
        self.on_codec_change(None)
        self._refresh(immediate=True)

    def on_codec_change(self, e) -> None:
        key = self.codec_select.value or ""
        if any(x in key for x in ("prores", "dnxhr", "vegas_fix")):
            self.hw_hint.value = "Note: this codec runs most stably on CPU."
        elif key == "copy":
            self.hw_hint.value = "Note: stream copy - no re-encoding."
        elif "av1" in key:
            self.hw_hint.value = "Note: AV1 - very efficient, but slow on CPU."
        else:
            self.hw_hint.value = ""
        self._refresh(immediate=True)

    def on_bitrate_mode_change(self, e) -> None:
        custom = self.bitrate_mode.value == "custom"
        self.crf_container.visible = not custom
        self.custom_bitrate_container.visible = custom
        self._refresh(immediate=True)

    def on_crf_change(self, e) -> None:
        self.crf_label.value = f"CRF: {int(self.crf_slider.value or 20)}"
        self._refresh(immediate=True)

    def on_custom_bitrate_change(self, e) -> None:
        self.bitrate_label.value = f"Video: {int(self.bitrate_slider.value or 20)} Mbps"
        self._refresh(immediate=True)

    def set_custom_bitrate(self, value: int) -> None:
        self.bitrate_slider.value = value
        self.bitrate_label.value = f"Video: {value} Mbps"
        self._refresh(immediate=True)

    def _on_cookies_change(self, e) -> None:
        val = self.download_cookies.value or "none"
        # Browser cookies only for real browsers; for "none"/"cookiefile"
        # --cookies-from-browser is not set.
        self._cookies_browser = None if val in ("none", "cookiefile") else val
        self.config.set("last_cookies", val)
        # Only show the file field for cookies.txt.
        self.cookies_file_row.visible = (val == "cookiefile")
        self._refresh(immediate=True)

    async def pick_cookies_file(self, e) -> None:
        files = await self.cookies_picker.pick_files(
            dialog_title="Choose cookies.txt",
            allow_multiple=False,
            allowed_extensions=["txt"],
        )
        if files and files[0].path:
            self.cookies_file.value = files[0].path
            self.config.set("cookies_file", files[0].path)
            self._refresh(immediate=True)

    # ========================= download ===========================================

    def start_download(self, e) -> None:
        with self.download_lock:
            if self._dl_running:
                return
            self._dl_running = True
            self._dl_cancelled = False

        url = (self.download_url.value or "").strip()
        out_dir = (self.download_output.value or "").strip()
        quality = self.download_quality.value or "best"
        cookies = self._cookies_browser
        cookiefile = ""
        if (self.download_cookies.value or "none") == "cookiefile":
            cookiefile = (self.cookies_file.value or "").strip()
        opts = {
            "impersonate": bool(self.toggle_impersonate.value) and self.impersonate_ok,
            "impersonate_available": self.impersonate_ok,
            "sponsorblock": bool(self.toggle_sponsorblock.value),
            "embed": bool(self.toggle_embed.value),
            "subs": bool(self.toggle_subs.value),
            "subs_lang": (self.subs_lang.value or "en,de").strip() or "en,de",
            "cookiefile": cookiefile,
            "potoken": bool(self.toggle_potoken.value),
            "potoken_url": (self.potoken_url.value or "").strip(),
            "plugins_dir": str(self.binaries.plugins_dir),
        }

        if not url:
            with self.download_lock:
                self._dl_running = False
            self._toast("Please enter a URL first.", error=True)
            return
        # Very simple URL sanity check (no network request, just
        # rejects obvious garbage like paths or shell snippets)
        if not re.match(r"^https?://", url, re.IGNORECASE):
            with self.download_lock:
                self._dl_running = False
            self._toast("URL must start with http:// or https://.", error=True)
            return
        if not out_dir:
            with self.download_lock:
                self._dl_running = False
            self._toast("Please choose a save folder.", error=True)
            return
        if not self.ytdlp_ok:
            with self.download_lock:
                self._dl_running = False
            self._toast("yt-dlp not available - please run Setup.", error=True)
            return
        if (self.download_cookies.value or "none") == "cookiefile" and (
            not cookiefile or not Path(cookiefile).is_file()
        ):
            with self.download_lock:
                self._dl_running = False
            self._toast("Cookies file (cookies.txt) is missing or not found.", error=True)
            return

        try:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
        except Exception as ex:
            with self.download_lock:
                self._dl_running = False
            self._toast(f"Could not create folder: {ex}", error=True)
            return

        self.config.set("last_output_folder", out_dir)
        self.config.set("last_url", url)

        self.download_status.value = "Downloading ..."
        self.download_progress.value = 0
        self._clear_log("download")
        self._set_download_busy(True)
        self._append_log("download", f"=== Download started ===\n{url}\n")
        self._refresh(immediate=True)

        self.page.run_thread(self._run_download, url, out_dir, quality, cookies, opts)

    def stop_download(self, e) -> None:
        with self.download_lock:
            proc = self.download_process
            self.download_process = None
            self._dl_cancelled = True
        _safe_terminate(proc)
        self.download_status.value = "Cancelled"
        self._set_download_busy(False)
        self._refresh(immediate=True)

    def _subprocess_env(self) -> dict:
        """Environment for yt-dlp - puts the tool's own bin folder at the
        front of PATH, so a Deno installed there (JS runtime for the
        YouTube n-challenge) is found by the yt-dlp subprocess."""
        env = os.environ.copy()
        bin_dir = str(self.binaries.bin_dir)
        sep = os.pathsep
        cur = env.get("PATH", "")
        if bin_dir not in cur.split(sep):
            env["PATH"] = (bin_dir + sep + cur) if cur else bin_dir
        return env

    def _build_download_cmd(
        self, url: str, out_dir: str, quality: str,
        cookies: str | None, opts: dict,
    ) -> list[str]:
        cmd: list[str] = [self.binaries.get_ytdlp_path()]

        # NOTE: postprocessor args are scoped to the specific postprocessor
        # (ExtractAudio / Merger) instead of the global "ffmpeg:" prefix.
        # The global prefix applied the audio re-encode to EVERY ffmpeg
        # pass - merge, thumbnail embed, metadata embed - i.e. the audio
        # was re-encoded up to three times per download (generation loss
        # + noticeably slower).
        if quality == "audio_wav":
            cmd.extend([
                "-x", "--audio-format", "wav",
                "--postprocessor-args", "ExtractAudio:-ar 48000 -ac 2 -c:a pcm_s16le",
            ])
        elif quality == "audio":
            cmd.extend([
                "-x", "--audio-format", "mp3", "--audio-quality", "0",
                "--postprocessor-args", "ExtractAudio:-codec:a libmp3lame -b:a 320k -ar 44100 -ac 2",
            ])
        elif quality == "audio_opus":
            cmd.extend([
                "-x", "--audio-format", "opus", "--audio-quality", "0",
            ])
        else:
            # Format selection - with AV1 preference or classic H.264/AAC.
            # AAC transcode only in the merge step; the embed steps just
            # keep +faststart without touching the streams again.
            cmd.extend([
                "--merge-output-format", "mp4",
                "--postprocessor-args",
                "Merger:-c:a aac -b:a 192k -ar 48000 -ac 2 -movflags +faststart",
                "--postprocessor-args", "Metadata:-movflags +faststart",
                "--postprocessor-args", "EmbedThumbnail:-movflags +faststart",
            ])
            # NOTE on the fallback chains: the old chains jumped from
            # "avc1 video + m4a audio" straight to "any video + any audio"
            # and then to "b" (the single pre-merged file). On YouTube the
            # pre-merged fallback is the 360p format - and on sites whose
            # audio isn't m4a (Instagram HLS, etc.) the first alternative
            # never matched. Each chain now degrades gracefully:
            # h264+m4a -> h264+any audio -> any video+audio -> pre-merged.
            if quality == "best_av1":
                # AV1 if available, otherwise best available
                cmd.extend([
                    "-f", ("bv*[vcodec^=av01]+ba/"
                           "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/"
                           "bv*[vcodec^=avc1]+ba/"
                           "bv*+ba/b"),
                    "-S", "vcodec:av01,res,acodec:m4a",
                ])
            elif quality == "best":
                cmd.extend([
                    "-f", ("bv*[vcodec^=avc1]+ba[acodec^=mp4a]/"
                           "bv*[vcodec^=avc1]+ba/"
                           "bv*+ba/b"),
                    "-S", "vcodec:h264,res,acodec:m4a",
                ])
            else:
                # Fixed resolution: cap the height in the filter itself
                # (not only via -S sorting) so an oversized format is never
                # chosen, but keep uncapped alternatives as a last resort.
                cmd.extend([
                    "-f", (f"bv*[vcodec^=avc1][height<={quality}]+ba[acodec^=mp4a]/"
                           f"bv*[vcodec^=avc1][height<={quality}]+ba/"
                           f"bv*[height<={quality}]+ba/"
                           "bv*+ba/b"),
                    "-S", f"res:{quality},vcodec:h264,acodec:m4a",
                ])

        # Modern, robust flags
        base_args: list[str] = [
            "--newline",
            "--no-playlist",
            "--retries", "10",
            "--fragment-retries", "10",
            "--concurrent-fragments", "8",
            "--extractor-retries", "3",
            "--throttled-rate", "100K",
            "--sleep-requests", "1",
            "--no-mtime",
            "--progress-template",
            "download:%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
            "-o", str(Path(out_dir) / "%(title).200B.%(ext)s"),
        ]

        # YouTube player clients.
        #   - tv + web_safari are currently the reliable clients;
        #     android/ios now usually need PO tokens and otherwise fail.
        #   - Once cookies are present (browser OR cookies.txt), we add
        #     web_embedded + web_creator. These are exactly the clients
        #     YouTube requires for age-restricted (18+) videos -
        #     web_creator covers account age verification, web_embedded
        #     the embeddable cases.
        # IMPORTANT: web_safari MUST always be included - its HLS/m3u8
        # formats need neither a PO token nor the n-challenge and (unlike
        # the tv client) are NOT affected by the DRM experiment
        # (yt-dlp #12563/#14404). This always leaves a playable format
        # and avoids the hard "This video is DRM protected" failure.
        # tv is never used on its own anymore.
        has_cookies = bool(cookies) or bool(opts.get("cookiefile"))
        if opts.get("potoken"):
            # With a PO token provider: mweb/tv_simply deliver full formats.
            player_clients = "mweb,tv_simply,web_safari,web_embedded,web_creator"
        elif has_cookies:
            # With cookies, the tv DRM issue goes away - default/tv deliver the best quality.
            player_clients = "default,web_safari,web_creator,web_embedded"
        else:
            # Without cookies/token: web_safari (HLS, no token/n needed) first,
            # default as a supplement for higher resolutions via Deno.
            player_clients = "web_safari,default"
        base_args.extend([
            "--extractor-args", f"youtube:player_client={player_clients}",
        ])

        # yt-dlp plugin directory (for the bgutil PO token provider). The
        # plugin (bgutil-ytdlp-pot-provider.zip) is placed by the user into
        # the plugins folder; "default" keeps the normal plugin paths.
        plugins_dir = opts.get("plugins_dir")
        if plugins_dir:
            base_args.extend([
                "--plugin-dirs", "default",
                "--plugin-dirs", str(plugins_dir),
            ])
        # Pass a different provider URL/port to the bgutil HTTP plugin.
        potoken_url = (opts.get("potoken_url") or "").strip()
        if potoken_url:
            base_args.extend([
                "--extractor-args", f"youtubepot-bgutilhttp:base_url={potoken_url}",
            ])

        # Embed options (only useful for video or non-PCM audio)
        if opts.get("embed"):
            base_args.extend([
                "--embed-thumbnail",
                "--embed-metadata",
                "--embed-chapters",
            ])

        # Subtitles
        if opts.get("subs") and quality not in ("audio", "audio_wav", "audio_opus"):
            langs = opts.get("subs_lang", "en,de")
            base_args.extend([
                "--write-subs", "--write-auto-subs",
                "--sub-langs", langs,
                "--embed-subs",
                "--convert-subs", "srt",
            ])

        # SponsorBlock
        if opts.get("sponsorblock"):
            base_args.extend([
                "--sponsorblock-remove", "sponsor,selfpromo,interaction",
                "--sponsorblock-mark", "all",
            ])

        # Anti-bot impersonation. Instagram/TikTok/X block yt-dlp's plain
        # TLS fingerprint outright, so impersonation is force-enabled for
        # those hosts whenever curl_cffi is available - even if the toggle
        # is off. This is the single most common cause of failed Instagram
        # downloads.
        impersonate = bool(opts.get("impersonate"))
        if not impersonate and opts.get("impersonate_available"):
            lo_url = url.lower()
            if any(h in lo_url for h in _IMPERSONATE_AUTO_HOSTS):
                impersonate = True
        if impersonate:
            base_args.extend(["--impersonate", "chrome"])

        # Cookies: either straight from the browser (Firefox/Safari are
        # reliable; Chromium on Windows is often blocked by App-Bound
        # Encryption) or from an exported cookies.txt - the robust,
        # browser-independent path for age-restricted videos.
        cookiefile = opts.get("cookiefile")
        if cookiefile:
            base_args = ["--cookies", str(cookiefile)] + base_args
        elif cookies:
            base_args = ["--cookies-from-browser", cookies] + base_args

        cmd.extend(base_args)
        cmd.append(url)
        return cmd

    def _log_preflight_hints(
        self, url: str, quality: str, cookies: str | None, opts: dict,
    ) -> None:
        """Warns about known problem setups BEFORE the download starts,
        instead of leaving the user to decode a yt-dlp error afterwards."""
        lo = url.lower()
        has_cookies = bool(cookies) or bool(opts.get("cookiefile"))
        is_audio = quality in ("audio", "audio_wav", "audio_opus")
        if ("youtube.com" in lo or "youtu.be" in lo) and not is_audio:
            if not self.js_runtime:
                self._append_log(
                    "download",
                    "Warning: no JS runtime (Deno) found - YouTube often only "
                    "offers 360p without one. Setup tab -> 'Install Deno'.",
                )
            elif not has_cookies and not opts.get("potoken"):
                self._append_log(
                    "download",
                    "Note: without cookies or a PO token, YouTube may withhold "
                    "some HD formats. If the result is low-res, set cookies or "
                    "enable the PO token option.",
                )
        if "instagram.com" in lo:
            if not has_cookies:
                self._append_log(
                    "download",
                    "Note: Instagram often requires login cookies. If this "
                    "download fails, pick a cookies option (cookies.txt is the "
                    "most reliable).",
                )
            if not opts.get("impersonate_available"):
                self._append_log(
                    "download",
                    "Warning: browser impersonation is unavailable - Instagram "
                    "usually blocks downloads without it. Update yt-dlp in the "
                    "Setup tab (the official binary includes curl_cffi).",
                )

    def _run_download(
        self, url: str, out_dir: str, quality: str,
        cookies: str | None, opts: dict,
    ) -> None:
        try:
            cmd = self._build_download_cmd(url, out_dir, quality, cookies, opts)
            self._log_preflight_hints(url, quality, cookies, opts)

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                encoding="utf-8", errors="replace",
                env=self._subprocess_env(),
                **_POPEN_KWARGS,
            )
            with self.download_lock:
                self.download_process = proc

            last_progress_line = ""
            try:
                if proc.stdout is not None:
                    for raw in proc.stdout:
                        line = raw.strip()
                        if not line:
                            continue

                        # Progress lines are often identical at high frequency -> dedupe.
                        is_progress = line.startswith("download:")
                        if not (is_progress and line == last_progress_line):
                            self._append_log("download", line)
                        if is_progress:
                            last_progress_line = line

                        if is_progress:
                            parts = [p.strip() for p in line[len("download:"):].split("|")]
                            if len(parts) >= 3:
                                pct_txt, speed, eta = parts[0], parts[1], parts[2]
                                m = re.search(r"(\d+(?:\.\d+)?)", pct_txt)
                                if m:
                                    pct = float(m.group(1)) / 100.0
                                    self.download_progress.value = max(0.0, min(pct, 1.0))
                                    self.download_status.value = f"{m.group(1)}%  |  {speed}  |  ETA {eta}"
                                    self._refresh()
                        else:
                            m = re.search(r"(\d+(?:\.\d+)?)%", line)
                            if m:
                                pct = float(m.group(1)) / 100.0
                                self.download_progress.value = max(0.0, min(pct, 1.0))
                                self.download_status.value = f"Downloading ... {m.group(1)}%"
                                self._refresh()
            except (ValueError, OSError):
                # stdout was closed (e.g. Stop) - fall through cleanly to wait()
                pass

            try:
                code = proc.wait(timeout=PROC_WAIT_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = -1

            cancelled = self._dl_cancelled
            if cancelled:
                self.download_status.value = "Cancelled"
                self._append_log("download", "\n=== Download cancelled ===")
            elif code == 0:
                self.download_progress.value = 1.0
                self.download_status.value = "Download completed"
                self._append_log("download", "\n=== Download successful ===")
                self._toast("Download successful")
            else:
                self.download_status.value = "Download failed"
                self._append_log("download", f"\n=== Download failed (code {code}) ===")
                log_text_lower = "\n".join(self.download_log_lines).lower()
                if "empty media response" in log_text_lower:
                    self._append_log(
                        "download",
                        "Tip: This looks like the known Instagram \"empty media "
                        "response\" bug (yt-dlp #17074) - the fix has landed in "
                        "yt-dlp's Nightly/Master channel but not yet in Stable. "
                        "Setup tab -> set Channel to 'Nightly' or 'Master' -> "
                        "'Update yt-dlp'.",
                    )
                elif ("requested format is not available" in log_text_lower
                      or "some formats may be missing" in log_text_lower
                      or "missing a url" in log_text_lower
                      or "please sign in" in log_text_lower
                      or "login required" in log_text_lower):
                    self._append_log(
                        "download",
                        "Tip: The site withheld formats or wants a login. "
                        "Try: (1) set cookies (cookies.txt is most reliable), "
                        "(2) install Deno in the Setup tab, (3) for YouTube "
                        "18+/HD issues enable the PO token option, (4) update "
                        "yt-dlp (Nightly channel often has the fix).",
                    )
                elif not self.js_runtime:
                    self._append_log(
                        "download",
                        "Tip: No JS runtime detected. Setup tab -> "
                        "'Install Deno' (needed for the YouTube n-challenge). "
                        "For 18+/stubborn videos, also set a cookies.txt.",
                    )
                self._toast("Download failed", error=True)

        except Exception as ex:
            self.download_status.value = f"Error: {ex}"
            self._append_log("download", f"Error: {ex}")
            self._toast(f"Download error: {ex}", error=True)
        finally:
            with self.download_lock:
                self.download_process = None
                self._dl_running = False
            self._set_download_busy(False)
            self._refresh(immediate=True)

    # ========================= conversion =========================================

    def _probe_duration(self, file_path: str) -> float | None:
        cmd = [
            self.binaries.get_ffprobe_path(),
            "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            file_path,
        ]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=30, encoding="utf-8", errors="replace",
                **_POPEN_KWARGS,
            )
            if r.returncode == 0 and r.stdout.strip():
                return float(r.stdout.strip())
        except Exception:
            pass
        return None

    def _probe_pix_fmt(self, file_path: str) -> str | None:
        """Reads the source pix_fmt for HDR-aware encoding."""
        cmd = [
            self.binaries.get_ffprobe_path(),
            "-v", "quiet", "-select_streams", "v:0",
            "-show_entries", "stream=pix_fmt",
            "-of", "default=noprint_wrappers=1:nokey=1",
            file_path,
        ]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=15, encoding="utf-8", errors="replace",
                **_POPEN_KWARGS,
            )
            if r.returncode == 0:
                return r.stdout.strip() or None
        except Exception:
            pass
        return None

    def _probe_color_meta(self, file_path: str) -> dict[str, str]:
        """Reads color primaries / transfer / space for color preservation."""
        cmd = [
            self.binaries.get_ffprobe_path(),
            "-v", "quiet", "-select_streams", "v:0",
            "-show_entries", "stream=color_primaries,color_trc,color_space,color_range",
            "-of", "default=noprint_wrappers=1",
            file_path,
        ]
        meta: dict[str, str] = {}
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=15, encoding="utf-8", errors="replace",
                **_POPEN_KWARGS,
            )
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        v = v.strip()
                        if v and v.lower() not in ("unknown", "n/a", "und", "reserved"):
                            meta[k.strip()] = v
        except Exception:
            pass
        return meta

    def _resolve_hw(self, hw: str) -> str:
        """Detects the best hardware acceleration when set to 'auto'."""
        if hw != "auto":
            return hw
        return _detect_hw_encoder()

    def _build_convert_args(self, source_pix_fmt: str | None = None) -> dict:
        codec_key = self.codec_select.value or "h264"
        hw_setting = self.hw_select.value or "auto"
        hw = self._resolve_hw(hw_setting)
        crf = int(self.crf_slider.value or 20)
        use_custom = self.bitrate_mode.value == "custom"
        custom_br = int(self.bitrate_slider.value or 20)

        # Detect HDR / 10-bit source
        is_10bit_source = bool(source_pix_fmt and ("10" in source_pix_fmt or "p010" in source_pix_fmt))

        video_codec = "libx264"
        audio_codec = "aac"
        audio_bitrate: str | None = "192k"
        preset = "medium"
        extra: list[str] = []

        match codec_key:
            case "copy":
                video_codec = audio_codec = "copy"
            case "vp9":
                video_codec = "libvpx-vp9"
                extra = ["-crf", "30", "-b:v", "0", "-row-mt", "1"]
            case "av1":
                # Hardware AV1 if available
                if hw == "nvidia":
                    video_codec = "av1_nvenc"
                    preset = "p4"
                    extra = [
                        "-rc", "vbr", "-cq", str(crf + 3), "-b:v", "0",
                        "-pix_fmt", "p010le" if is_10bit_source else "yuv420p",
                    ]
                elif hw == "intel":
                    video_codec = "av1_qsv"
                    preset = "medium"
                    extra = ["-global_quality", str(crf), "-pix_fmt", "nv12"]
                else:
                    # SVT-AV1 is significantly faster than libaom
                    video_codec = "libsvtav1"
                    preset = None  # SVT-AV1 nutzt -preset 0..13 separat
                    extra = [
                        "-preset", "6",
                        "-crf", str(crf),
                        "-pix_fmt", "yuv420p10le" if is_10bit_source else "yuv420p",
                    ]
            case "h264_allintra":
                video_codec = "libx264"
                extra = ["-g", "1", "-bf", "0", "-crf", str(crf), "-profile:v", "high", "-pix_fmt", "yuv420p"]
            case "h264_handbrake":
                video_codec = "libx264"
                extra = ["-crf", str(crf), "-profile:v", "high", "-pix_fmt", "yuv420p"]
            case "vegas_fix":
                video_codec = "libx264"
                preset = "fast"
                extra = ["-fps_mode", "cfr", "-r", "30", "-crf", "16", "-g", "1", "-bf", "0", "-pix_fmt", "yuv420p"]
                audio_codec = "pcm_s16le"
                audio_bitrate = None
            case "prores422":
                video_codec = "prores_ks"
                extra = ["-profile:v", "2", "-pix_fmt", "yuv422p10le"]
                audio_codec = "pcm_s16le"
                audio_bitrate = None
            case "prores422hq":
                video_codec = "prores_ks"
                extra = ["-profile:v", "3", "-pix_fmt", "yuv422p10le"]
                audio_codec = "pcm_s16le"
                audio_bitrate = None
            case "dnxhr_hq":
                video_codec = "dnxhd"
                extra = ["-profile:v", "dnxhr_hq", "-pix_fmt", "yuv422p"]
                audio_codec = "pcm_s16le"
                audio_bitrate = None
            case "youtube":
                video_codec = "libx264"
                preset = "slow"
                pix = "yuv420p10le" if is_10bit_source else "yuv420p"
                extra = ["-crf", "18", "-profile:v", "high10" if is_10bit_source else "high", "-pix_fmt", pix]
                audio_bitrate = "320k"
            case "youtube_av1":
                # YouTube recommends AV1 with a higher bitrate for uploads
                if hw == "nvidia":
                    video_codec = "av1_nvenc"
                    preset = "p5"
                    extra = ["-rc", "vbr", "-cq", "20", "-b:v", "0", "-pix_fmt", "yuv420p"]
                else:
                    video_codec = "libsvtav1"
                    preset = None
                    extra = ["-preset", "5", "-crf", "30", "-pix_fmt", "yuv420p"]
                audio_bitrate = "320k"
            case "social":
                video_codec = "libx264"
                preset = "medium"
                extra = ["-crf", "20", "-profile:v", "main", "-pix_fmt", "yuv420p"]
            case _:
                is_h265 = codec_key == "h265"
                if hw == "nvidia":
                    video_codec = "hevc_nvenc" if is_h265 else "h264_nvenc"
                    preset = "p4"
                    pix = "p010le" if (is_h265 and is_10bit_source) else ("p010le" if is_h265 else "yuv420p")
                    extra = [
                        "-rc", "vbr", "-cq", str(crf + 3), "-b:v", "0",
                        "-multipass", "qres",
                        "-rc-lookahead", "32",
                        "-spatial-aq", "1", "-temporal-aq", "1",
                        "-bf", "3", "-refs", "3",
                        "-profile:v", "main" if is_h265 else "high",
                        *(["-tag:v", "hvc1", "-pix_fmt", pix] if is_h265 else ["-pix_fmt", pix]),
                    ]
                elif hw == "amd":
                    video_codec = "hevc_amf" if is_h265 else "h264_amf"
                    preset = "balanced"
                    extra = [
                        "-qp_i", str(crf), "-qp_p", str(crf), "-qp_b", str(crf),
                        "-pix_fmt", "yuv420p",
                    ]
                elif hw == "intel":
                    video_codec = "hevc_qsv" if is_h265 else "h264_qsv"
                    preset = "medium"
                    extra = ["-global_quality", str(crf), "-pix_fmt", "nv12"]
                else:
                    video_codec = "libx265" if is_h265 else "libx264"
                    pix = "yuv420p10le" if (is_h265 and is_10bit_source) else "yuv420p"
                    extra = ["-crf", str(crf), "-pix_fmt", pix]
                    if is_h265:
                        extra.extend(["-tag:v", "hvc1", "-x265-params", "log-level=error"])

        # custom bitrate override
        if use_custom and video_codec not in ("copy", "prores_ks", "dnxhd"):
            remove = {"-crf", "-cq", "-global_quality", "-qp_i", "-qp_p", "-qp_b"}
            filtered: list[str] = []
            skip = False
            for arg in extra:
                if skip:
                    skip = False
                    continue
                if arg in remove:
                    skip = True
                    continue
                filtered.append(arg)
            extra = filtered + [
                "-b:v", f"{custom_br}M",
                "-maxrate", f"{int(custom_br * 1.5)}M",
                "-bufsize", f"{int(custom_br * 2)}M",
            ]

        return {
            "codec_key": codec_key,
            "video_codec": video_codec,
            "audio_codec": audio_codec,
            "audio_bitrate": audio_bitrate,
            "preset": preset,
            "extra": extra,
            "use_custom": use_custom,
            "custom_br": custom_br,
            "crf": crf,
            "hw_resolved": hw,
            "hw_requested": hw_setting,
        }

    def start_conversion(self, e) -> None:
        with self.convert_lock:
            if self._cv_running:
                return
            self._cv_running = True
            self._cv_cancelled = False

        inp = (self.convert_input.value or "").strip()
        out = (self.convert_output.value or "").strip()
        if not inp or not out:
            with self.convert_lock:
                self._cv_running = False
            self._toast("Please choose input and output files.", error=True)
            return
        if not Path(inp).exists():
            with self.convert_lock:
                self._cv_running = False
            self._toast("Input file does not exist.", error=True)
            return
        # Prevent the source file from being overwritten
        try:
            if Path(inp).resolve() == Path(out).resolve():
                with self.convert_lock:
                    self._cv_running = False
                self._toast("Input and output must not be identical.", error=True)
                return
        except Exception:
            pass
        if not self.ffmpeg_ok:
            with self.convert_lock:
                self._cv_running = False
            self._toast("FFmpeg not available - please run Setup.", error=True)
            return

        try:
            Path(out).parent.mkdir(parents=True, exist_ok=True)
        except Exception as ex:
            with self.convert_lock:
                self._cv_running = False
            self._toast(f"Could not create target folder: {ex}", error=True)
            return

        self.convert_progress.value = 0
        self.convert_status.value = "Converting ..."
        self._clear_log("convert")
        self._set_convert_busy(True)
        self._append_log("convert", f"=== Conversion started ===\n{Path(inp).name}\n")
        self._refresh(immediate=True)

        self.page.run_thread(self._run_conversion, inp, out)

    def stop_conversion(self, e) -> None:
        with self.convert_lock:
            proc = self.convert_process
            self.convert_process = None
            self._cv_cancelled = True
        _safe_terminate(proc)
        self.convert_status.value = "Cancelled"
        self._set_convert_busy(False)
        self._refresh(immediate=True)

    def _build_ffmpeg_cmd(
        self, ffmpeg: str, input_file: str, output_file: str,
        profile: dict, color_meta: dict[str, str],
    ) -> list[str]:
        cmd: list[str] = [
            ffmpeg, "-hide_banner", "-y",
            "-progress", "pipe:1",
            "-stats_period", "0.5",
            "-nostats",
            "-fflags", "+genpts",
            "-i", input_file,
            # Map all streams (video, audio, subs)
            "-map", "0:v:0",
            "-map", "0:a?",
            "-map_metadata", "0",
            "-map_chapters", "0",
        ]

        if profile["video_codec"] != "copy" and "-pix_fmt" not in profile["extra"]:
            cmd.extend(["-pix_fmt", "yuv420p"])

        cmd.extend(["-c:v", profile["video_codec"]])

        if profile["video_codec"] not in ("copy", "prores_ks", "dnxhd") and profile["preset"]:
            cmd.extend(["-preset", profile["preset"]])

        cmd.extend(profile["extra"])

        # Color metadata preservation (important for HDR!)
        if (profile["video_codec"] != "copy" and color_meta and
                hasattr(self, "toggle_preserve_color") and self.toggle_preserve_color.value):
            for k, ffmpeg_flag in (
                ("color_primaries", "-color_primaries"),
                ("color_trc", "-color_trc"),
                ("color_space", "-colorspace"),
                ("color_range", "-color_range"),
            ):
                v = color_meta.get(k)
                if v:
                    cmd.extend([ffmpeg_flag, v])

        # CPU thread usage
        if profile["video_codec"] in ("libx264", "libx265", "libvpx-vp9", "libsvtav1"):
            cmd.extend(["-threads", "0"])

        # audio
        if profile["video_codec"] == "copy":
            cmd.extend(["-c:a", "copy"])
        else:
            cmd.extend(["-c:a", profile["audio_codec"]])
            if profile["audio_bitrate"]:
                cmd.extend(["-b:a", profile["audio_bitrate"]])
            _pcm = ("copy", "pcm_s16le", "pcm_s32le", "pcm_f32le", "pcm_s24le")
            if profile["audio_codec"] not in _pcm:
                cmd.extend(["-ar", "48000", "-ac", "2"])

        # Fast web streaming for MP4 outputs
        out_lower = output_file.lower()
        if out_lower.endswith((".mp4", ".m4v", ".mov")) and profile["video_codec"] != "copy":
            cmd.extend(["-movflags", "+faststart"])

        cmd.append(output_file)
        return cmd

    def _run_conversion(self, input_file: str, output_file: str) -> None:
        try:
            ffmpeg = self.binaries.get_ffmpeg_path()
            source_pix = self._probe_pix_fmt(input_file)
            color_meta = self._probe_color_meta(input_file) if (
                hasattr(self, "toggle_preserve_color") and self.toggle_preserve_color.value
            ) else {}
            profile = self._build_convert_args(source_pix_fmt=source_pix)
            duration = self._probe_duration(input_file)

            cmd = self._build_ffmpeg_cmd(ffmpeg, input_file, output_file, profile, color_meta)

            self._append_log("convert", f"Codec:  {profile['codec_key']} -> {profile['video_codec']}")
            if profile["hw_requested"] == "auto":
                self._append_log("convert", f"HW:     auto -> {profile['hw_resolved']}")
            mode = f"Custom {profile['custom_br']}M" if profile["use_custom"] else f"CRF {profile['crf']}"
            self._append_log("convert", f"Mode:   {mode}")
            if source_pix:
                self._append_log("convert", f"Source: pix_fmt={source_pix}")
            if color_meta:
                meta_str = ", ".join(f"{k}={v}" for k, v in color_meta.items())
                self._append_log("convert", f"Color:  {meta_str}")
            if duration:
                self._append_log("convert", f"Duration: {duration:.1f}s")
            self._append_log("convert", "-" * 44)

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                encoding="utf-8", errors="replace",
                **_POPEN_KWARGS,
            )
            with self.convert_lock:
                self.convert_process = proc

            progress_data: dict[str, str] = {}
            started_at = time.monotonic()
            last_line = ""

            try:
                if proc.stdout is not None:
                    for raw in proc.stdout:
                        line = raw.strip()
                        if not line:
                            continue

                        if "=" in line:
                            key, val = line.split("=", 1)
                            key = key.strip()
                            val = val.strip()
                            progress_data[key] = val

                            if key == "progress":
                                cur_sec = parse_hms_to_seconds(progress_data.get("out_time", ""))
                                if cur_sec is None:
                                    raw_us = progress_data.get("out_time_us") or progress_data.get("out_time_ms")
                                    if raw_us and raw_us.lstrip("-").isdigit():
                                        n = int(raw_us)
                                        cur_sec = n / 1_000_000 if n > 10_000_000 else n / 1000

                                bitrate = progress_data.get("bitrate", "N/A")
                                speed = progress_data.get("speed", "N/A")
                                fps = progress_data.get("fps", "N/A")
                                frame = progress_data.get("frame", "?")
                                q_val = progress_data.get("stream_0_0_q", "?")
                                tot_size = progress_data.get("total_size", "0")
                                out_time = progress_data.get("out_time", "00:00:00.00")

                                try:
                                    size_text = f"{int(int(tot_size) / 1024)} KiB"
                                except (TypeError, ValueError):
                                    size_text = "0 KiB"

                                elapsed = format_elapsed(time.monotonic() - started_at)
                                compact = (
                                    f"frame={str(frame).rjust(5)} fps={fps} q={q_val} "
                                    f"size={size_text.rjust(9)} time={out_time} "
                                    f"bitrate={bitrate} speed={speed} elapsed={elapsed}"
                                )
                                if compact != last_line:
                                    self._append_log("convert", compact)
                                    last_line = compact

                                if cur_sec is not None and duration and duration > 0:
                                    pct = max(0.0, min(cur_sec / duration, 1.0))
                                    speed_val = parse_speed_value(speed)
                                    eta = format_eta((duration - cur_sec) / speed_val) if speed_val else "--:--"
                                    self.convert_progress.value = pct
                                    self.convert_status.value = (
                                        f"Converting ... {pct * 100:.1f}%  |  {bitrate}  |  {speed}  |  {fps} fps  |  ETA {eta}"
                                    )
                                else:
                                    self.convert_status.value = f"Converting ...  |  {bitrate}  |  {speed}  |  {fps} fps"

                                self._refresh()
                                if val == "end":
                                    break

                        else:
                            m_time = re.search(r"time=(\d{2}:\d{2}:\d{2}\.\d+)", line)
                            m_speed = re.search(r"speed=\s*(\S+)", line)
                            m_br = re.search(r"bitrate=\s*(\S+)", line)
                            m_fps = re.search(r"fps=\s*(\S+)", line)
                            if m_time:
                                compact = " ".join(line.split())
                                if compact != last_line:
                                    self._append_log("convert", compact)
                                    last_line = compact
                                cur_sec = parse_hms_to_seconds(m_time.group(1))
                                speed_str = m_speed.group(1) if m_speed else "N/A"
                                br_str = m_br.group(1) if m_br else "N/A"
                                fps_str = m_fps.group(1) if m_fps else "N/A"
                                if cur_sec is not None and duration and duration > 0:
                                    pct = max(0.0, min(cur_sec / duration, 1.0))
                                    sv = parse_speed_value(speed_str)
                                    eta = format_eta((duration - cur_sec) / sv) if sv else "--:--"
                                    self.convert_progress.value = pct
                                    self.convert_status.value = (
                                        f"Converting ... {pct * 100:.1f}%  |  {br_str}  |  {speed_str}  |  {fps_str} fps  |  ETA {eta}"
                                    )
                                else:
                                    self.convert_status.value = f"Converting ...  |  {br_str}  |  {speed_str}  |  {fps_str} fps"
                                self._refresh()
            except (ValueError, OSError):
                # stdout was closed (e.g. Stop) - fall through cleanly to wait()
                pass

            try:
                code = proc.wait(timeout=PROC_WAIT_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = -1

            cancelled = self._cv_cancelled
            if cancelled:
                self.convert_status.value = "Cancelled"
                self._append_log("convert", "\n=== Conversion cancelled ===")
            elif code == 0:
                self.convert_progress.value = 1.0
                self.convert_status.value = "Conversion completed"
                self._append_log("convert", "\n=== Conversion successful ===")
                self._toast("Conversion successful")
            else:
                self.convert_status.value = "Conversion failed"
                self._append_log("convert", f"\n=== Conversion failed (code {code}) ===")
                self._toast("Conversion failed", error=True)

        except Exception as ex:
            self.convert_status.value = f"Error: {ex}"
            self._append_log("convert", f"Error: {ex}")
            self._toast(f"Conversion error: {ex}", error=True)
        finally:
            with self.convert_lock:
                self.convert_process = None
                self._cv_running = False
            self._set_convert_busy(False)
            self._refresh(immediate=True)

    # ========================= setup workers ======================================

    def install_binaries(self, e) -> None:
        self.setup_log.value = "Installing binaries ..."
        self._refresh(immediate=True)
        self.page.run_thread(self._install_binaries_worker)

    def _install_binaries_worker(self) -> None:
        def _yt_progress(written: int, total: int) -> None:
            mb = written / (1024 * 1024)
            if total:
                pct = written * 100 / total
                self.setup_log.value = f"Downloading yt-dlp ... {pct:.0f}% ({mb:.1f} MiB)"
            else:
                self.setup_log.value = f"Downloading yt-dlp ... {mb:.1f} MiB"
            self._refresh()

        def _ff_progress(name: str, written: int, total: int) -> None:
            mb = written / (1024 * 1024)
            if total:
                pct = written * 100 / total
                self.setup_log.value = f"Downloading {name} ... {pct:.0f}% ({mb:.1f} MiB)"
            else:
                self.setup_log.value = f"Downloading {name} ... {mb:.1f} MiB"
            self._refresh()

        try:
            self.setup_log.value = "Downloading yt-dlp ..."
            self._refresh()
            self.binaries.install_ytdlp(on_progress=_yt_progress)
            self.setup_log.value = "Downloading FFmpeg ..."
            self._refresh()
            self.binaries.install_ffmpeg(on_progress=_ff_progress)
            self.setup_log.value = "Installation completed"
            # update_page=True + retries=2 -> per-control update +
            # short retry in case Windows briefly locks the new file.
            self.refresh_binary_status(update_page=True, retries=2)
            self._toast("Binaries installed successfully")
        except Exception as ex:
            self.setup_log.value = f"Error: {ex}"
            self._refresh()
            self._toast(f"Installation error: {ex}", error=True)

    def update_ytdlp(self, e) -> None:
        channel = self.ytdlp_channel.value if hasattr(self, "ytdlp_channel") else "stable"
        self.setup_log.value = (
            "Updating yt-dlp ..." if channel == "stable"
            else f"Switching yt-dlp to '{channel}' channel ..."
        )
        self._refresh(immediate=True)
        self.page.run_thread(self._update_ytdlp_worker, channel)

    def _update_ytdlp_worker(self, channel: str = "stable") -> None:
        def _progress(written: int, total: int) -> None:
            mb = written / (1024 * 1024)
            if total:
                pct = written * 100 / total
                self.setup_log.value = f"Updating yt-dlp ... {pct:.0f}% ({mb:.1f} MiB)"
            else:
                self.setup_log.value = f"Updating yt-dlp ... {mb:.1f} MiB"
            self._refresh()

        try:
            if channel == "stable":
                self.binaries.install_ytdlp(on_progress=_progress)
            else:
                # Nightly/Master builds often carry site-specific fixes (e.g.
                # the Instagram "empty media response" bug, yt-dlp #17074)
                # days or weeks before they reach a Stable release. This uses
                # yt-dlp's own --update-to self-updater against the binary
                # we already have installed.
                self.setup_log.value = f"Switching yt-dlp to '{channel}' channel ..."
                self._refresh()
                out = self.binaries.update_channel(channel)
                if out:
                    self._append_log("download", out)
            # Important: set the status text first, THEN call
            # refresh_binary_status with update_page=True - that way both the
            # log text and the new yt-dlp version arrive in one consistent
            # page update.
            self.setup_log.value = "yt-dlp updated"
            self.refresh_binary_status(update_page=True, retries=2)
            self._toast("yt-dlp updated successfully")
        except Exception as ex:
            self.setup_log.value = f"Error: {ex}"
            self._refresh()
            self._toast(f"Update error: {ex}", error=True)

    def install_deno(self, e) -> None:
        self.setup_log.value = "Downloading Deno ..."
        self._refresh(immediate=True)
        self.page.run_thread(self._install_deno_worker)

    def _install_deno_worker(self) -> None:
        def _progress(written: int, total: int) -> None:
            mb = written / (1024 * 1024)
            if total:
                pct = written * 100 / total
                self.setup_log.value = f"Downloading Deno ... {pct:.0f}% ({mb:.1f} MiB)"
            else:
                self.setup_log.value = f"Downloading Deno ... {mb:.1f} MiB"
            self._refresh()

        try:
            self.binaries.install_deno(on_progress=_progress)
            self.setup_log.value = "Deno installed"
            self.refresh_binary_status(update_page=True, retries=1)
            self._toast("Deno (JS runtime) installed")
        except Exception as ex:
            self.setup_log.value = f"Error: {ex}"
            self._refresh()
            self._toast(f"Deno installation error: {ex}", error=True)

    # ========================= self-update check ===================================

    def check_for_tool_update(self, e=None) -> None:
        """Manual trigger (Info tab button) for the tool's own update check."""
        self.page.run_thread(self._check_tool_update_worker, True)

    def _check_tool_update_worker(self, manual: bool = False) -> None:
        has_update, latest, source = check_tool_update()
        self.tool_update_available = has_update
        self.tool_update_version = latest
        self.tool_update_source = source if has_update else ""
        if hasattr(self, "update_chip"):
            self.update_chip.visible = has_update
            if has_update and hasattr(self, "update_chip_text"):
                self.update_chip_text.value = f"Update available: v{latest}"
            self._refresh(immediate=True)
        if has_update:
            # Ask the user right away - one click installs the update.
            self._show_update_dialog()
        elif manual:
            if GITHUB_REPO.startswith("yourusername"):
                self._toast(
                    "Self-update check is not configured yet - set GITHUB_REPO "
                    "at the top of the script.", error=True,
                )
            else:
                self._toast("You're running the latest version")

    def _show_update_dialog(self, e=None) -> None:
        if not self.tool_update_available:
            return
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row(spacing=10, controls=[
                ft.Icon(ft.Icons.SYSTEM_UPDATE, color=ft.Colors.AMBER_400),
                ft.Text("Update available"),
            ]),
            content=ft.Text(
                f"Version {self.tool_update_version} is available on GitHub "
                f"(installed: {VERSION}).\n\n"
                "Install now? The tool updates itself and restarts."
            ),
            actions=[
                ft.TextButton("Later", on_click=self._close_dialog),
                ft.FilledButton(
                    "Install & restart", icon=ft.Icons.DOWNLOAD,
                    on_click=self._apply_tool_update,
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self._open_dialog = dlg
        # Prefers page.open (Flet 0.28+), falls back for older releases.
        for opener in (
            lambda: self.page.open(dlg),
            lambda: self.page.show_dialog(dlg),
        ):
            try:
                opener()
                break
            except Exception:
                continue
        self._refresh(immediate=True)

    def _apply_tool_update(self, e=None) -> None:
        self._close_dialog()
        if getattr(sys, "frozen", False):
            # A frozen build (PyInstaller etc.) can't replace itself -
            # send the user to the repo page instead.
            try:
                self.page.launch_url(UPDATE_PAGE_URL)
            except Exception:
                pass
            return
        self._toast(f"Installing update v{self.tool_update_version} ...")
        self.page.run_thread(self._apply_tool_update_worker)

    def _apply_tool_update_worker(self) -> None:
        try:
            source = self.tool_update_source
            if not source:
                _has, _latest, source = check_tool_update(timeout=30)
                if not source:
                    raise RuntimeError("Could not download the update")
            # Never overwrite the script with a file that doesn't even
            # parse - a broken update would brick the tool.
            compile(source, "video_tool.py", "exec")
            target = Path(__file__).resolve()
            tmp = target.with_suffix(".py.new")
            tmp.write_text(source, encoding="utf-8")
            os.replace(tmp, target)  # atomic swap
        except Exception as ex:
            self._toast(f"Update failed: {ex}", error=True)
            return
        self._toast("Update installed - restarting ...")
        time.sleep(1.5)
        self._restart_self(target)

    def _restart_self(self, script: Path) -> None:
        """Launches the (updated) script as a new process and closes
        this instance."""
        self._cleanup_processes()
        try:
            subprocess.Popen(
                [sys.executable, str(script)], cwd=str(script.parent),
            )
        except Exception as ex:
            self._toast(f"Restart failed - please restart manually: {ex}", error=True)
            return
        try:
            self.page.window.destroy()
        except Exception:
            os._exit(0)


# ============================== entry point =======================================

def main(page: ft.Page) -> None:
    VideoToolApp(page)


if __name__ == "__main__":
    ft.run(main)
