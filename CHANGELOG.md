# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- The manifest names the category `media_entertainment.music_radio_services`, so Home Assistant lists the integration next to the other music services.

## [0.1.0] - 2026-10-23

First public release.

### Added

- Config flow for a Navidrome server or any other server with the Subsonic or OpenSubsonic API, checked with `ping`. Authentication uses the Subsonic token, the password is never sent.
- Options flow: one playlist per Creative Tonie, the mode `append` or `replace`, and automatic sync every 15 minutes.
- Sync engine: uploads new songs one at a time, removes songs that left the playlist, orders the chapters like the playlist and writes the chapter list with one request. Chapters that did not come from the playlist stay in `append` mode and move with the chapter before them.
- Safety checks that change nothing: capacity of 90 minutes and 99 chapters (or the smaller values the cloud reports), unknown durations while the cloud transcodes, a check before large removals (`confirm_removals`), and IDs that can no longer be found.
- Chapters deleted in the Tonie app are not uploaded again unless the sync runs with `restore_deleted`.
- Recovery of an interrupted run: the mapping is stored as `pending` before the write and resolved on the next run, so no chapter ends up twice on the Tonie. Songs uploaded by a run that stopped before the write are uploaded again by the next run.
- Fresh uploads are found again after the cloud has transcoded them and changed their ID.
- Actions `tonie_sync.sync`, `tonie_sync.adopt` (creates a Navidrome playlist from the chapters already on a Tonie), `tonie_sync.search_songs` and `tonie_sync.playlist_edit`, all with responses. The last three are admin actions and take `config_entry_id` when several entries are set up.
- A device per Tonie with a sync button and an enum status sensor with capacity, progress and the last message as attributes.
- Diagnostics with the raw chapters, cloud capacity and the stored mapping. Password, server URL, username and household IDs are redacted, Tonie IDs replaced by placeholders.
- Error texts are stripped of request URLs and Subsonic auth parameters before they reach messages, logs, the store or diagnostics.
- English and German translations.
- HACS metadata, CI with tests, `hassfest` and the HACS check.

[Unreleased]: https://github.com/derblub/ha-tonie-sync/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/derblub/ha-tonie-sync/releases/tag/v0.1.0
