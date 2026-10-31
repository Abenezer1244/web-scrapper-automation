"""scripts/data_quality_report.py must start when run as its docstring says.

`python scripts/data_quality_report.py` puts scripts/ on sys.path, not the repo root, so
`from src...` failed with ModuleNotFoundError before any query ran (found 2026-10-28,
same bug fixed in scripts/cv_qualification_report.py).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_the_documented_command_imports_the_app():
    root = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    done = subprocess.run([sys.executable, str(root / "scripts" / "data_quality_report.py"), "--help"],
                          cwd=root / "scripts", env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-400:]
    assert "--days" in done.stdout
