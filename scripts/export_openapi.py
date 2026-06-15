"""Export the FastAPI OpenAPI schema to schema/openapi.json (the API contract).

This committed file is the SOURCE OF TRUTH for the API contract: the separate
frontend repo (bridgeleads-web) generates its TypeScript types from it via
`openapi-typescript`, and CI on both sides fails if either drifts.

Deterministic output (sorted keys + trailing newline) so re-running on unchanged
code produces a byte-identical file — that's what the CI staleness gate diffs.

  python scripts/export_openapi.py            # write schema/openapi.json
  python scripts/export_openapi.py --check     # exit 1 if the committed file is stale
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import app  # noqa: E402

_OUT = Path(__file__).resolve().parents[1] / "schema" / "openapi.json"


def _render() -> str:
    return json.dumps(app.openapi(), sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if schema/openapi.json differs from the current app schema")
    args = ap.parse_args()

    rendered = _render()
    if args.check:
        current = _OUT.read_text(encoding="utf-8") if _OUT.exists() else ""
        if current != rendered:
            print("STALE: schema/openapi.json is out of date. Run: python scripts/export_openapi.py")
            sys.exit(1)
        print("OK: schema/openapi.json is up to date.")
        return

    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {_OUT} ({len(rendered)} bytes, {len(app.openapi().get('paths', {}))} paths)")


if __name__ == "__main__":
    main()
