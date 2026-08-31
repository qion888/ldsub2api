"""Shared limits and defaults for the Sub2API integration."""

DEFAULT_URL = "http://127.0.0.1:8080"
DEFAULT_AUTOMATION = {
    "enabled": False,
    "interval_seconds": 300,
    "auto_import": False,
    "max_reclaim_attempts": 3,
    "proxy_id": None,
    "group_ids": [],
    "codex_fingerprint_mode": "off",
}
CODEX_FINGERPRINT_MODES = {"off", "device", "session", "full"}
MAX_ACCOUNTS = 5000
MAX_PROXIES = 5000
MAX_BUNDLES = 50
MAX_GROUPS = 100
MAX_RECLAIM_CODES = 100
