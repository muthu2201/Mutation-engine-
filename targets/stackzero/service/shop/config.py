"""Runtime configuration, read from environment variables set by the launcher."""

import os


def _int(name, default):
    value = os.environ.get(name)
    return int(value) if value not in (None, "") else default


def _bool(name, default):
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return value.lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self):
        self.dsn = os.environ.get("SHOP_DSN", "dbname=shop user=shop")
        self.pg_options = os.environ.get("SHOP_PG_OPTIONS", "")
        self.pool_min = _int("SHOP_POOL_MIN", 2)
        self.pool_max = _int("SHOP_POOL_MAX", 10)
        threshold = os.environ.get("SHOP_PREPARE_THRESHOLD", "5")
        self.prepare_threshold = None if threshold == "off" else int(threshold)
        self.gc_gen0 = _int("SHOP_GC_GEN0", 700)
        self.gc_freeze = _bool("SHOP_GC_FREEZE", False)
        self.json_compact = _bool("SHOP_JSON_COMPACT", False)
        self.native_lib = os.environ.get("SHOP_NATIVE_LIB", "")


settings = Settings()
