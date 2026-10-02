"""Shared fixtures. Integration tests that need the real stack (Postgres, the sandbox helper,
the Go load generator) are marked ``integration`` and skipped unless COLLOID_INTEGRATION=1,
so the fast unit/property suite runs anywhere."""

import os
from pathlib import Path

import pytest

INTEGRATION = os.environ.get("COLLOID_INTEGRATION") == "1"
requires_integration = pytest.mark.skipif(not INTEGRATION, reason="set COLLOID_INTEGRATION=1 to run (needs Postgres/sandbox/loadgen as root)")


@pytest.fixture(scope="session")
def target_root() -> Path:
    return Path(__file__).resolve().parents[1] / "targets" / "stackzero"
