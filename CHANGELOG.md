# Changelog

## v0.0.6 — 2026-07-18

### Added

- **Convert to MP3 or WAV.** The Convert tab has a new **"Audio (MP3 / WAV)"** category for extracting the audio track from a local video file — as **MP3 (320 kbps)** or **WAV (PCM 16-bit)**. When an audio target is selected, the video-only controls (hardware encoder, bitrate mode, quality slider) are hidden, and the output file extension follows the selection automatically: `.mp3`/`.wav` for audio, back to `.mp4` when you switch to a video codec again. A hand-typed wrong extension is corrected before the conversion starts, so the output container always matches its name.

## v0.0.5 — 2026-07-13

### Fixed

- **Downloads that silently did nothing when the file already existed.** If the target folder already contained a video with the same name, yt-dlp skipped the download — but reported success, so the log looked exactly like a completed download while no new file was written. The app now checks the target file name *before* the download starts and never claims success when nothing was downloaded.

### Added

- **"If file exists" — you decide what happens.** A new dropdown next to Quality and Cookies on the Download tab, remembered between sessions:
  - **Ask me** (default) — a dialog shows which file is in the way and lets you type a new name (pre-filled with a free one), overwrite the existing file, or skip.
  - **Auto-rename** — saves as `Title (1).mp4`, `Title (2).mp4`, … without asking.
  - **Overwrite** — replaces the existing file.
  - **Skip** — keeps the existing file and says so clearly instead of pretending to download.

  The file extension is always chosen by the download itself, so a renamed file can never end up with a wrong or misleading extension — including audio-only downloads, where the final `.mp3`/`.wav`/`.opus` file is correctly recognized as an existing file.

## v0.0.4 — 2026-07-12

### Fixed

- **Instagram "empty media response" failures.** The bug (yt-dlp #17074) is fixed in the yt-dlp Stable release 2026.07.04 — switching to the Nightly channel is no longer necessary. The app now knows the fixed version: if your yt-dlp is older, it warns you *before* an Instagram download starts and tells you exactly what to do (Setup tab → "Update yt-dlp", Stable channel). The failure tip after an "empty media response" error was updated accordingly and now shows your installed version when it is the culprit.

## v0.0.3 — 2026-07-12

### Added

- **Built-in one-click self-update.** On startup the app checks GitHub for a newer version. If one is found, a dialog asks whether you want it — one click on "Install & restart" downloads the update, installs it and restarts the app. The yellow "Update available" chip in the header and the "Check for Updates" button on the Info tab open the same dialog. The downloaded file is syntax-checked before it replaces anything, so a broken download can never brick the tool.

## v0.0.2 — 2026-07-10

### Fixed

- **YouTube videos no longer download in 360p when a higher quality is available.** The app used to give up too early when picking video and audio tracks and fell back to the lowest built-in quality. It now tries several sensible combinations in order and only uses 360p as a true last resort. Fixed resolutions (1080p, 720p, ...) are now also enforced properly.
- **Instagram downloads work much more reliably.** Instagram blocks download tools that don't look like a real browser. The app now automatically switches on its "look like a real browser" mode for Instagram, TikTok and X/Twitter — you don't have to remember the toggle anymore.
- **Better audio quality.** The audio track was quietly re-encoded up to three times during a single download (once while merging, again when embedding the thumbnail, and again for the metadata). It is now converted only once, so downloads are faster and sound slightly better.
- **The paste button next to the URL field works again** on newer versions of the UI framework.

### Improved

- **The app starts faster.** A network library that is only needed for installing/updating tools is no longer loaded on every start.
- **Helpful hints before and after a download.** If something about your setup is likely to cause a problem (no Deno installed, no cookies set for Instagram, ...), the log now tells you *before* the download starts — and if a download still fails, it suggests concrete next steps instead of just showing an error code.
- **Cleaner, tidier look.** The Download tab is now organized into clear sections (Source & Quality, Save Location, Live Log). All the advanced switches are tucked away in a collapsible "Advanced options" area so the main screen stays simple. Slimmer header, nicer log panel.

### Added

- **The URL field fills itself in.** If you have a link in your clipboard when the app starts, it's already in the URL field. If not, the last link you used is restored.
- **Press Enter in the URL field to start the download** — no need to reach for the button.
