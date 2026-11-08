# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [1.1.0] - 2026-11-08

### Added
- **Microphone mirroring**: capture any WASAPI input and play it on one or more
  outputs, running in parallel with audio mirroring. Mono sources are upmixed.
- **Windows installer** (Inno Setup): per-user install without admin rights,
  Start menu entry, uninstaller, optional desktop icon and "Start with Windows".
- **Start with Windows** toggle (Settings page and tray menu), stored in
  `HKCU\...\Run` and aware of the Windows *Startup apps* switch.
- **Settings page**: start with Windows, start minimized, version and a button
  to open the data/log folder.
- Single instance with activation: opening the app while it is in the tray
  shows its window; `--minimized` and `--quit` command-line options.
- `build.ps1` (tests → PyInstaller → installer), `version.py` as the single
  source of the version, unit tests (`tests/`), CI and release workflows.

### Fixed
- Device events were never received: pycaw changed its callback signatures and
  every state/default-device notification raised `TypeError`, so automatic
  rules did not fire when a device was connected.
- Heavy work (enumeration, `SetDefaultEndpoint`) ran inside the COM
  notification thread; it now runs on the backend thread, debounced.
- Mirroring could deadlock the app when stopping/restarting, and audio
  crackled because blocks of different sizes were dropped or zero-padded.
- A source could be mirrored to itself (infinite feedback).
- Targets with a different sample rate failed to open (WASAPI auto-convert).
- "Reload WASAPI devices" did not detect newly connected devices.
- Repeated clicks on *Start mirroring* could restart or duplicate the stream.
- Rules with role "communications" were ignored when a "console" rule had
  higher priority; rule edits did not trigger a re-evaluation.
- Typing a multi-digit priority was impossible (the table rebuilt on each key).
- "Remove selected" could delete the wrong rule.
- Quitting from the tray could crash the app (COM uninitialized on the wrong
  thread, shutdown called twice).
- A corrupt `config.json` prevented startup and was overwritten without backup.
- The log file grew without limit (now rotates at 1 MB).
- Selected list items were not highlighted after a refresh.

### Changed
- The executable is built in folder (onedir) mode: faster startup and fewer
  antivirus false positives. Installer size: ~44 MB (was a 259 MB onefile exe).

## [1.0.0] - 2026-09-27

### Added
- Initial version: device list and default device switching (console /
  communications), priority rules with negation syntax, WASAPI loopback
  mirroring, system tray menu.

[1.1.0]: https://github.com/RomanFama592/audio-manager-pro/releases/tag/v1.1.0
