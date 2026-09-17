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

    railway run --service worker python scripts/set_r2_lifecycle.py
    railway run --service worker python scripts/set_r2_lifecycle.py \
        --apply --yes-bucket bridgeleads-exports

`--yes-bucket` must match the configured bucket exactly. It is not ceremony: this
writes a DELETION policy across a whole bucket, the call runs unattended through
`railway run` where there is no prompt to catch a mistake, and the bucket name is
the one thing an operator can get wrong that nothing else would catch.

IT MERGES, IT DOES NOT REPLACE (Codex, Critical). S3's
PutBucketLifecycleConfiguration replaces the ENTIRE configuration for the bucket.
Writing our rule alone would silently delete every other rule someone had set.
This reads the current configuration, substitutes only our own rule by ID, keeps
everything else byte-for-byte, and prints both the before and after so the change
is auditable from the deploy log.

NO NONCURRENT-VERSION EXPIRY (Codex, High). An earlier draft set
NoncurrentVersionExpiration on the theory that bucket versioning could hide old
copies. R2 has no S3-equivalent object versioning, the field is not part of R2's
documented lifecycle model, and sending it risks the API rejecting the whole
request — which would leave the bucket with NO rule while looking like a clean
failure. If versioned copies ever become a concern they need an R2-native answer,
not this field.

TWO THINGS THIS DOES NOT PROVE, both of which need watching afterwards:
  * Lifecycle application is ASYNCHRONOUS. A 200 means the rule is stored, not
    that anything has been deleted yet; existing objects can take over a day.
  * If an export URL was ever served through a CDN, a cached copy can outlive the
    R2 object. R2_ALLOW_PUBLIC_URLS defaults false, so this should not apply, but
    confirm rather than assume before treating deletion as complete.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

from src.config import settings  # noqa: E402

_RULE_ID = "bridgeleads-export-retention"
_ABSENT = ("NoSuchLifecycleConfiguration", "404", "NoSuchConfiguration")


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
        region_name="auto",  # correct for R2; the endpoint carries the account
    )


def _our_rule(days: int) -> dict:
    return {
        "ID": _RULE_ID,
        "Status": "Enabled",
        # Whole bucket. The exporter writes under exports/<user>/<job>/, but a
        # prefix filter would silently miss anything written elsewhere, and
        # "silently missed" is the failure this rule exists to cover. This is why
        # --yes-bucket exists: it is only safe if the bucket holds exports only.
        "Filter": {"Prefix": ""},
        "Expiration": {"Days": days},
        # Failed multipart uploads hold real bytes of real exports.
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
    }


def _current_rules(client, bucket: str) -> list:
    try:
        return client.get_bucket_lifecycle_configuration(Bucket=bucket).get("Rules", [])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in _ABSENT:
            return []
        raise


def _show(rules: list, label: str) -> None:
    print(f"--- {label}: {len(rules)} rule(s) ---")
    if not rules:
        print("  (none — every export ever delivered is retained indefinitely)")
    for r in rules:
        print(f"  {json.dumps(r, default=str, sort_keys=True)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the rule (default: show only)")
    ap.add_argument("--yes-bucket", default="", help="must equal R2_BUCKET_NAME; required with --apply")
    ap.add_argument(
        "--days", type=int, default=None,
        help=f"max object age (default: EXPORT_RETENTION_DAYS = {settings.EXPORT_RETENTION_DAYS})",
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
    print(f"bucket:   {bucket}")
    print(f"endpoint: {settings.R2_ENDPOINT_URL}")
    print(f"rule:     {_RULE_ID} — expire objects after {days} days\n")

    client = _client()
    before = _current_rules(client, bucket)
    _show(before, "before")

    if not args.apply:
        print(f"\nDRY RUN. Nothing written. Re-run with:\n"
              f"  --apply --yes-bucket {bucket}")
        return

    if args.yes_bucket != bucket:
        raise SystemExit(
            f"\nrefusing to apply: --yes-bucket {args.yes_bucket!r} does not match the "
            f"configured bucket {bucket!r}. This writes a deletion policy across the "
            f"whole bucket; name it explicitly."
        )

    # MERGE. Keep every rule that is not ours, substitute ours by ID.
    kept = [r for r in before if r.get("ID") != _RULE_ID]
    merged = kept + [_our_rule(days)]
    if kept:
        print(f"\npreserving {len(kept)} pre-existing rule(s): "
              f"{', '.join(str(r.get('ID')) for r in kept)}")

    client.put_bucket_lifecycle_configuration(
        Bucket=bucket, LifecycleConfiguration={"Rules": merged}
    )
    print(f"\napplied '{_RULE_ID}' ({days}-day expiry) to {bucket}\n")
    _show(_current_rules(client, bucket), "after")
    print(
        "\nNOTE: a successful write does NOT mean anything is deleted yet. R2 applies "
        "lifecycle asynchronously and existing objects can take over 24h. Re-check "
        "before reporting the retention promise as performed."
    )


if __name__ == "__main__":
    main()
