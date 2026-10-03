"""One process owns an evaluation cluster: a second one is refused instead of stopping it."""

import os
import subprocess
import sys

import pytest

from colloid.adapters.target.stackzero.postgres import PostgresCluster, _take_ownership

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the evaluation cluster is Linux-only")


def test_a_second_process_cannot_take_the_cluster(tmp_path):
    _take_ownership(tmp_path)
    _take_ownership(tmp_path)  # re-entrant within the owning process
    code = ("import sys; from colloid.adapters.target.stackzero.postgres import _take_ownership, PostgresError\n"
            f"try:\n    _take_ownership(__import__('pathlib').Path({str(tmp_path)!r}))\nexcept PostgresError as e:\n    print(e); sys.exit(3)\n")
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert res.returncode == 3 and f"pid {os.getpid()}" in res.stdout


def test_targets_in_one_process_share_one_cluster_object(tmp_path):
    a = PostgresCluster.shared(object(), tmp_path / "pg")  # type: ignore[arg-type]
    b = PostgresCluster.shared(object(), tmp_path / "pg")  # type: ignore[arg-type]
    assert a is b and PostgresCluster.shared(object(), tmp_path / "other") is not a  # type: ignore[arg-type]
