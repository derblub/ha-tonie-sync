"""Load the modules without HA dependencies directly from the integration folder."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "tonie_sync"))
