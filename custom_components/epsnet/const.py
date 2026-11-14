"""Constants for the EPSNET integration."""

DOMAIN = "epsnet"

CONF_HOST = "host"
CONF_LOCAL_PORT = "local_port"
CONF_PORT = "port"
CONF_MAP_PATH = "map_path"
CONF_SCAN_INTERVAL = "scan_interval"
# Fast lane: poll binary inputs (buttons, PIRs) this often, independently of the full cycle.
CONF_FAST_SCAN_INTERVAL = "fast_scan_interval"
CONF_NAME = "name"
# Replace the program's own words in variable names in the diagnostics download.
# On unless the user turned it off (Configure -> Download diagnostics).
CONF_ANONYMIZE_DIAGNOSTICS = "anonymize_diagnostics"

# Frozen published-variable map persisted in the config entry (see map_sources.py).
CONF_MAP = "map"
# FileSelector upload id used transiently by the config-flow upload step.
CONF_MAP_FILE = "map_file"

DEFAULT_PORT = 61682
DEFAULT_LOCAL_PORT = 61681
DEFAULT_SCAN_INTERVAL = 500
# Entries that never stored the setting (created before 0.6) keep the fast lane off.
DEFAULT_FAST_SCAN_INTERVAL = 0
# New entries start with it on: 100 ms catches short wall-button presses.
NEW_ENTRY_FAST_SCAN_INTERVAL = 100
LEGACY_DEFAULT_SCAN_INTERVAL_SECONDS = 5
DIAGNOSTIC_DIRECTORY = "epsnet"
MERGED_CATALOG_BASENAME = "merged_catalog"

SUPPORTED_PLATFORMS = ("binary_sensor", "light", "sensor", "switch")

ISSUE_TRACKER_URL = "https://github.com/ocervinka/ha-epsnet/issues"
# Set when the entry was created without a map: its entities come from the PLC
# program and keep their identity when a map is imported later.
CONF_PROGRAM_FIRST = "program_entities_first"
