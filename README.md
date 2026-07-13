# Video Tool

A cross-platform desktop app for **downloading videos with [yt-dlp](https://github.com/yt-dlp/yt-dlp)** and **converting them with [FFmpeg](https://ffmpeg.org/)** — wrapped in a clean [Flet](https://flet.dev/) GUI. It manages its own copies of `yt-dlp`, `ffmpeg`/`ffprobe` and an optional JavaScript runtime, so you don't have to install or wire up any of them by hand.

> Current version: **0.0.5** — see the [changelog](CHANGELOG.md).

---

## Features

- **Download** any site supported by yt-dlp, with per-job quality presets (video resolutions, audio-only as M4A/WAV/Opus).
- **Convert** local media via FFmpeg with selectable codecs, bitrates and hardware-encoder backends.
- **Self-managing binaries** — the app downloads and updates `yt-dlp`, `ffmpeg`/`ffprobe` and (optionally) [Deno](https://deno.com/) into its own folder. yt-dlp downloads are **verified against the release's signed `SHA2-256SUMS`** before use.
- **Release-channel switch** — pick `Stable`, `Nightly` or `Master` for yt-dlp and update in place via yt-dlp's own `--update-to`, so you can pick up site-bug fixes that haven't reached a stable release yet.
- **PO token support** — works with the [bgutil PO token provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) plugin for content that requires a GVS PO token.
- **Cookies** from your browser or from an exported `cookies.txt` for age-restricted/private content.
- **Existing-file handling** — when a download would overwrite a file that is already there, choose per job whether to be asked for a new name, auto-rename (`Title (1).mp4`), overwrite, or skip. No more silent "skipped but reported as done" downloads.
- **Subtitles** (write + auto-subs, embed, convert to SRT), **SponsorBlock**, **metadata/thumbnail/chapter embedding**, and **request impersonation** to reduce bot blocks.
- **Built-in self-update check** for the app itself against GitHub Releases.
- Responsive UI: binary probing runs in the background, so the window appears instantly.

## Requirements

- **Python 3.10+**
- The app installs its Python dependencies (`flet`, `requests`) automatically on first launch. You can also install them manually:

  ```bash
  pip install -r requirements.txt
  ```

- `yt-dlp` and `ffmpeg` are fetched and managed by the app itself — nothing to install separately. (If a system-wide `yt-dlp`/`ffmpeg` is already on your `PATH`, it will be used.)

## Quick start

```bash
git clone https://github.com/<your-username>/video-tool.git
cd video-tool
pip install -r requirements.txt
python video_tool.py
```

On first run, open the **Setup** tab and let the app download `yt-dlp` and `ffmpeg`. Then paste a URL on the **Download** tab and go.

## Run in Docker (headless / Raspberry Pi)

The repo ships a `Dockerfile` and `compose.yaml` that run the same `main()` as a Flet **web app**, reachable in the browser at `http://<host>:8550/`.

```bash
docker compose up -d --build
# then open http://localhost:8550/
```

Downloads land in `./downloads`; config and a locally updated `yt-dlp` persist in the `video-tool-config` volume.

## Configuration

Settings are stored as JSON in `~/.video_tool_v3/config.json`. Managed binaries live in `~/.video_tool_v3/bin`, and yt-dlp plugins (e.g. the bgutil PO token provider) go in `~/.video_tool_v3/plugins`.

### Enabling the self-update check

The app can notify you when a newer release exists. Set the repo at the top of `video_tool.py`:

```python
GITHUB_REPO = "<your-username>/video-tool"
```

While this is left at the default placeholder, the self-update check stays disabled.

## Security & integrity

This tool downloads and runs third-party binaries, so a few safeguards are built in:

- **HTTPS-only downloads** — the internal downloader refuses any non-`https://` URL, including URLs returned by remote APIs (e.g. ffbinaries).
- **Checksum verification** — yt-dlp binaries are verified against the official, release-signed `SHA2-256SUMS` list; a mismatch discards the download and aborts.
- **No shell invocation** — all external commands (`yt-dlp`, `ffmpeg`) are executed as argument lists, never through a shell, so URLs and paths cannot be interpreted as shell commands.
- **Atomic writes** — downloads and config are written to a temp file and renamed into place, so an interrupted write never leaves a half-written binary or config behind.
- **Robust config parsing** — a corrupt or hand-edited `config.json` is ignored rather than crashing the app.

**Note:** Only download content you have the right to download, and respect the terms of service of the sites you use.

## Project layout

```
video_tool.py          # the application (single file)
requirements.txt       # Python dependencies
Dockerfile             # headless/web container image
compose.yaml           # Docker Compose service
entrypoint.py          # container entrypoint (Flet web view)
```

## Credits

Built on the excellent work of [yt-dlp](https://github.com/yt-dlp/yt-dlp), [FFmpeg](https://ffmpeg.org/), [Flet](https://flet.dev/) and the [bgutil PO token provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider). This project bundles/launches those tools but is not affiliated with them.

## License

Released under the MIT License — see [LICENSE](LICENSE).
