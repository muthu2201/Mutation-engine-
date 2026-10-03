package main

import (
	"os"
	"strconv"
)

// settings is the runtime configuration, read from environment variables set by the launcher.
type settings struct {
	DSN       string
	PGOptions string
	PoolMin   int32
	PoolMax   int32
	ExecMode  string
}

func envString(name, def string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return def
}

func envInt32(name string, def int32) int32 {
	if v := os.Getenv(name); v != "" {
		if n, err := strconv.ParseInt(v, 10, 32); err == nil {
			return int32(n)
		}
	}
	return def
}

func loadSettings() settings {
	return settings{
		DSN:       envString("SHOP_DSN", "dbname=shop user=shop"),
		PGOptions: os.Getenv("SHOP_PG_OPTIONS"),
		PoolMin:   envInt32("SHOP_POOL_MIN", 2),
		PoolMax:   envInt32("SHOP_POOL_MAX", 10),
		ExecMode:  envString("SHOP_EXEC_MODE", "cache_statement"),
	}
}
