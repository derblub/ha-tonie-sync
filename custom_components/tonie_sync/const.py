"""Constants for tonie_sync."""
DOMAIN = "tonie_sync"
TONIEBOX_DOMAIN = "toniebox"

CONF_URL = "url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
DEFAULT_URL = "http://navidrome.local:4533"

OPT_TONIES = "tonies"  # {tonie_id: {name, household_id, playlist, playlist_name, mode}}
OPT_AUTO_SYNC = "auto_sync"
NO_PLAYLIST = "none"

AUTO_SYNC_MINUTES = 15
STORAGE_VERSION = 2

# States of the status sensor (translation keys)
STATE_PENDING = "pending"
STATE_CURRENT = "up_to_date"
STATE_RUNNING = "running"
STATE_TOO_LONG = "too_long"
STATE_ERROR = "error"
STATE_CONFIRM = "needs_confirmation"  # large removal, only with confirm_removals
STATES = [STATE_PENDING, STATE_CURRENT, STATE_RUNNING, STATE_TOO_LONG, STATE_CONFIRM, STATE_ERROR]

# Storage version 1 used German state keys
LEGACY_STATES = {
    "ausstehend": STATE_PENDING,
    "aktuell": STATE_CURRENT,
    "laeuft": STATE_RUNNING,
    "zu_lang": STATE_TOO_LONG,
    "bestaetigen": STATE_CONFIRM,
    "fehler": STATE_ERROR,
}

SIGNAL_UPDATE = f"{DOMAIN}_update_{{entry_id}}_{{tonie_id}}"
