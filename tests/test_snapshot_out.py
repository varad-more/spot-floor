"""`--out` is deleted wholesale, so it must refuse to be the working directory."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SNAPSHOT = str(Path(__file__).resolve().parent.parent / "scripts" / "snapshot.py")


@pytest.mark.parametrize("target", [".", "..", "/"])
def test_snapshot_refuses_to_rmtree_the_working_directory(target, tmp_path) -> None:
    canary = tmp_path / "do-not-delete.txt"
    canary.write_text("still here", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, SNAPSHOT, "--out", target, "--skip-poll"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )

    assert result.returncode != 0
    assert "would delete the working directory" in result.stderr
    assert canary.read_text(encoding="utf-8") == "still here"
