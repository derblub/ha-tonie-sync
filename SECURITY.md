# Security policy

## Supported versions

Security fixes go into the latest release. Please upgrade before reporting.

## Reporting a vulnerability

Please do not open a public issue. Report it privately through [GitHub's vulnerability reporting](https://github.com/derblub/ha-tonie-sync/security/advisories/new) or by email to [daniel@pushingpixels.at](mailto:daniel@pushingpixels.at).

You will get an answer within five working days. Once a fix is released, the report is credited in the changelog unless you prefer otherwise.

## What the integration does with your data

- It stores the URL, username and password of your Subsonic server in the Home Assistant config entry, like other integrations store their credentials. Requests carry the Subsonic token `md5(password + salt)` with a new salt each time, never the password. Token and salt are part of the request URL: over plain `http://` they can be read on the network, reused and attacked offline to recover the password. Use `https://` for the server URL. Error messages, logs, the stored state and diagnostics never contain these URLs.
- It downloads songs from your Subsonic server and uploads them to the Tonie cloud through the toniebox integration. It holds one song in memory at a time and writes no audio to disk.
- It stores the mapping between songs and chapters in `.storage/tonie_sync.<entry_id>`.
- It contacts no other service. The Tonie cloud login belongs to the toniebox integration.
- Diagnostics redact the password, server URL, username and household IDs and replace Tonie IDs with placeholders. They do contain Tonie names, song and chapter titles.
- The actions `adopt`, `search_songs` and `playlist_edit` are limited to Home Assistant administrators.
