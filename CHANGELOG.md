# Changelog

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
