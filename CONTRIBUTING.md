# Contributing

Thanks for taking the time. The most useful bug reports come with the diagnostics file of the integration: most surprises come from answers of the Tonie cloud or of a Subsonic server that nobody anticipated.

## Reporting a bug

Open an [issue](https://github.com/derblub/ha-tonie-sync/issues/new/choose) with:

- the versions of Home Assistant, Tonie Sync and the toniebox integration,
- the state and `last_message` of the status sensor,
- the diagnostics file (Settings → Devices & services → Tonie Sync → ⋮ → Download diagnostics). The password is redacted, song and chapter titles are not: remove what you would rather not share,
- your Subsonic server and its version, if it is not Navidrome.

## Development setup

You need Python 3.14, the version Home Assistant 2026.10 runs on.

```console
$ git clone https://github.com/derblub/ha-tonie-sync
$ cd ha-tonie-sync
$ python -m venv .venv
$ .venv/bin/pip install pytest aiohttp
$ .venv/bin/python -m pytest -q
```

The tests import the modules that do not depend on Home Assistant straight from `custom_components/tonie_sync` (see `tests/conftest.py`), so they need neither Home Assistant nor the network. Please keep it that way: fake the Subsonic server and the Tonie cloud in the test instead of calling them.

CI runs the tests, `python -m py_compile custom_components/tonie_sync/*.py`, `hassfest` and the HACS check.

## Where things live

| File | What it does |
| --- | --- |
| `custom_components/tonie_sync/planner.py` | Plans the new chapter list: mapping, foreign chapters, capacity. No I/O |
| `custom_components/tonie_sync/engine.py` | Runs a sync: safety checks, downloads, uploads, re-planning, the one write, `pending` |
| `custom_components/tonie_sync/matcher.py` | Matches chapter titles to Navidrome songs for `adopt` |
| `custom_components/tonie_sync/adopt.py` | The `adopt` action |
| `custom_components/tonie_sync/subsonic.py` | Subsonic API client with token authentication |
| `custom_components/tonie_sync/bridge.py` | Every access to the toniebox integration. Its internals are used nowhere else |
| `custom_components/tonie_sync/__init__.py` | Setup and the registration of the actions |
| `custom_components/tonie_sync/manager.py` | Per config entry: status, store, locks, automatic sync |
| `custom_components/tonie_sync/migrations.py` | Migrations of the stored data |
| `custom_components/tonie_sync/config_flow.py` | Config flow and options flow |

A change to the sync rules in `planner.py` or `engine.py` needs a test case and an update to "How a sync works" in the README. A change that touches the toniebox integration belongs in `bridge.py`.

## Pull requests

- One topic per pull request, with a test that fails without the change.
- Describe the user-visible change in `CHANGELOG.md` under "Unreleased".
- Strings shown in Home Assistant go into `strings.json` and both files in `translations/`.
- By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).

Everyone taking part is expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
