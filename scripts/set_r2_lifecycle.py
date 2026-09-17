"""Set the R2 export-retention lifecycle rule (Privacy Policy §7). Item 5c.

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

RUN IT AGAINST PRODUCTION, so the credentials stay in Railway and never land in a
local shell or an agent transcript:

    railway run --service worker python scripts/set_r2_lifecycle.py           # show
    railway run --service worker python scripts/set_r2_lifecycle.py --apply   # write

Idempotent: re-running with --apply re-asserts the same rule. Safe to run twice.

VERSIONING. If the bucket has object versioning enabled, deleting or expiring the
CURRENT object can leave prior versions readable. The rule below expires
noncurrent versions too, which is the part people forget.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

from src.config import settings  # noqa: E402

_RULE_ID = "bridgeleads-export-retention"


def _client():
    missing = [
        n for n in ("R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
        if not getattr(settings, n, "")
    ]
    if missing:
        raise SystemExit(
            f"missing {', '.join(missing)} - run this through `railway run` so the "
            "production R2 credentials are injected; do not paste them into a shell"
        )
    return boto3.client(
        "s3",
        endpoint_url=settings.R2_ENDPOINT_URL,
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def _desired(days: int) -> dict:
    return {
        "Rules": [
            {
                "ID": _RULE_ID,
                "Status": "Enabled",
                # Whole bucket. The exporter writes under exports/<user>/<job>/,
                # but a prefix filter would silently miss anything written
                # elsewhere — and "silently missed" is the failure mode this rule
                # exists to cover.
                "Filter": {"Prefix": ""},
                "Expiration": {"Days": days},
                # The half that is usually forgotten: with versioning on, expiring
                # the current object leaves the old version fetchable.
                "NoncurrentVersionExpiration": {"NoncurrentDays": days},
                # Reap failed multipart uploads too; their parts hold real bytes.
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
            }
        ]
    }


def _show(client, bucket: str) -> None:
    try:
        current = client.get_bucket_lifecycle_configuration(Bucket=bucket)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in (
            "NoSuchLifecycleConfiguration", "404", "NoSuchConfiguration",
        ):
            print(f"{bucket}: NO lifecycle rule configured.")
            print("Every export ever delivered is retained indefinitely.")
            return
        raise
    rules = current.get("Rules", [])
    print(f"{bucket}: {len(rules)} lifecycle rule(s):")
    for r in rules:
        print(
            f"  - {r.get('ID')}: status={r.get('Status')} "
            f"expire={r.get('Expiration')} "
            f"noncurrent={r.get('NoncurrentVersionExpiration')}"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the rule (default: show only)")
    ap.add_argument(
        "--days", type=int, default=None,
        help=f"max object age (default: EXPORT_RETENTION_DAYS = {settings.EXPORT_RETENTION_DAYS})",
    )
    args = ap.parse_args()

    # `args.days or DEFAULT` would be wrong: 0 is falsy, so `--days 0` would
    # silently become 365 instead of being rejected. On a retention control,
    # quietly substituting a different number than the operator typed is the
    # last thing you want.
    days = settings.EXPORT_RETENTION_DAYS if args.days is None else args.days
    if days < 1:
        raise SystemExit("--days must be at least 1")

    bucket = settings.R2_BUCKET_NAME
    client = _client()

    print("=== before ===")
    _show(client, bucket)

    if not args.apply:
        print(f"\nDRY RUN. Would set '{_RULE_ID}': expire objects after {days} days "
              f"(current AND noncurrent versions), abort incomplete multipart after 7.")
        print("Re-run with --apply to write it.")
        return

    client.put_bucket_lifecycle_configuration(
        Bucket=bucket, LifecycleConfiguration=_desired(days)
    )
    print(f"\napplied '{_RULE_ID}': {days}-day expiry on {bucket}")
    print("=== after ===")
    _show(client, bucket)


if __name__ == "__main__":
    main()
