"""Every entry point must import FIRST, in a fresh interpreter.

`src.scrapers.base_scraper` imports `src.api` (the SSRF guard), and `src.api`'s
package __init__ imports every router. So a router that imports a scraper module at
module level closes a loop that only bites a process whose FIRST import is
`src.scrapers`: the api and the worker happened to import in an order that survived,
while every ops script under scripts/ died at import with a circular ImportError
(#393's quote route imported the contact-lookup planner, which imports
pierce_atip_owner -> base_scraper).

An in-process `import` cannot catch this: by the time a test runs, conftest has
already imported everything, and a second import is a cache hit. Each case below
runs in its own subprocess with the test environment.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent

# The first module a real process imports. Scripts start from src.scrapers.* or
# src.db / src.workers; the services start from main / src.workers.
_ENTRY_POINTS = (
    "src.scrapers",
    "src.scrapers.base_scraper",
    "src.scrapers.enrichment.pierce_atip_owner",
    "src.scrapers.enrichment.king_county_assessor",
    "src.api",
    "src.api.contact_lookup_planner",
    "src.api.routes.jobs",
    "src.workers",
    "src.workers.skip_trace_claim",
    "main",
)


@pytest.mark.parametrize("module", _ENTRY_POINTS)
def test_the_module_imports_first_in_a_fresh_interpreter(module):
    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=_REPO, env=os.environ.copy(), capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
