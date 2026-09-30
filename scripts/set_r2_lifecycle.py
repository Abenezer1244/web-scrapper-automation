"""Set the R2 export-retention lifecycle rule (Privacy Policy §7). Runbook 5g step 6.

WHY THIS EXISTS, AND WHY IT IS NOT OPTIONAL.
The retention sweep deletes aged export objects from R2 itself
(scheduler_helpers/retention.py), and that is the suspenders. This is the belt,
and it is the only half that closes the EXPORT RACE: an export job can read a
lead's phone number, the purge can commit, and the job can then upload its file
to R2 — putting the PII back after the database is clean. Nothing in application
code can prevent that, because the read happened before the purge existed. Only a
storage-side maximum age reaps it.

It also covers every object the sweep cannot see: anything whose key was lost,
written by a path that never recorded it, or orphaned by a failed upload.

USES THE CLOUDFLARE NATIVE API, NOT S3/boto3. An earlier version of this script
used boto3 against R2_ENDPOINT_URL, and it could not authenticate in production at
all — head_bucket, list_objects and get_lifecycle every one returned 401. The
worker's S3-compatible credentials are not live; `upload_to_r2` has always used
the native API with R2_API_TOKEN, and so does this. Verified working against
production 2026-09-18.

IT MERGES, IT DOES NOT REPLACE. Writing a lifecycle configuration replaces the
whole thing, and this bucket ALREADY HAS A RULE — "Default Multipart Abort Rule",
found in production. A blind write would have silently deleted it. This reads the
current rules, substitutes only our own by id, keeps every other rule exactly as
returned, and prints before and after so the change is auditable from the log.

    railway run --service worker -- python scripts/set_r2_lifecycle.py
    railway run --service worker -- python scripts/set_r2_lifecycle.py \
        --apply --yes-bucket bridgeleads-exports

`--yes-bucket` must match the configured bucket exactly. It is not ceremony: this
writes a DELETION policy across a whole bucket, unattended, where there is no
prompt to catch a typo.

A success means the rule is STORED, not that anything is deleted. R2 applies
lifecycle asynchronously and existing objects can take over 24h.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.api  # noqa: E402, F401 — package init first; data_exporter is circular otherwise
import requests  # noqa: E402

from src.config import settings  # noqa: E402
from src.utils.data_exporter import _r2_api_base, _r2_headers  # noqa: E402

_RULE_ID = "bridgeleads-export-retention"
_DAY = 86_400


def _get_rules() -> list:
    r = requests.get(_r2_api_base() + "/lifecycle", headers=_r2_headers(), timeout=30)
    if r.status_code != 200:
        raise SystemExit(f"could not read lifecycle ({r.status_code}): {r.text[:200]}")
    return r.json().get("result", {}).get("rules", []) or []


def _our_rule(days: int) -> dict:
    # Shape mirrors what Cloudflare itself returns for the pre-existing rule:
    # empty `conditions` means every object. maxAge is in SECONDS, not days.
    return {
        "id": _RULE_ID,
        "enabled": True,
        "conditions": {},
        "deleteObjectsTransition": {
            "condition": {"type": "Age", "maxAge": days * _DAY}
        },
    }


def _show(rules: list, label: str) -> None:
    print(f"--- {label}: {len(rules)} rule(s) ---")
    if not rules:
        print("  (none — every export ever delivered is retained indefinitely)")
    for rule in rules:
        print(f"  {json.dumps(rule, sort_keys=True)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the rule (default: show only)")
    ap.add_argument("--yes-bucket", default="", help="must equal R2_BUCKET_NAME; required with --apply")
    ap.add_argument(
        "--days", type=int, default=None,
        help=f"max object age in days (default: EXPORT_RETENTION_DAYS = {settings.EXPORT_RETENTION_DAYS})",
    )
    args = ap.parse_args()

    # `args.days or DEFAULT` would be wrong: 0 is falsy, so `--days 0` would
    # silently become the default instead of being rejected. On a retention
    # control, quietly substituting a different number than the operator typed is
    # the last thing you want.
    days = settings.EXPORT_RETENTION_DAYS if args.days is None else args.days
    if days < 1:
        raise SystemExit("--days must be at least 1")

    bucket = settings.R2_BUCKET_NAME
    print(f"bucket: {bucket}")
    print(f"rule:   {_RULE_ID} — delete objects older than {days} days\n")

    before = _get_rules()
    _show(before, "before")

    if not args.apply:
        print(f"\nDRY RUN. Nothing written. Re-run with:\n  --apply --yes-bucket {bucket}")
        return

    if args.yes_bucket != bucket:
        raise SystemExit(
            f"\nrefusing to apply: --yes-bucket {args.yes_bucket!r} does not match the "
            f"configured bucket {bucket!r}. This writes a deletion policy across the "
            f"whole bucket; name it explicitly."
        )

    # MERGE. Keep every rule that is not ours, substitute ours by id.
    kept = [r for r in before if r.get("id") != _RULE_ID]
    if kept:
        print(f"\npreserving {len(kept)} pre-existing rule(s): "
              f"{', '.join(str(r.get('id')) for r in kept)}")

    resp = requests.put(
        _r2_api_base() + "/lifecycle",
        headers={**_r2_headers(), "Content-Type": "application/json"},
        json={"rules": kept + [_our_rule(days)]},
        timeout=30,
    )
    if resp.status_code != 200 or not resp.json().get("success", False):
        raise SystemExit(f"lifecycle write FAILED ({resp.status_code}): {resp.text[:300]}")

    print(f"\napplied '{_RULE_ID}' ({days}-day expiry) to {bucket}\n")
    _show(_get_rules(), "after")
    print(
        "\nNOTE: a successful write does NOT mean anything is deleted yet. R2 applies "
        "lifecycle asynchronously and existing objects can take over 24h. Re-check "
        "before reporting the retention promise as performed."
    )


if __name__ == "__main__":
    main()
