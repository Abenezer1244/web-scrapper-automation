"""No CI workflow may reach a production database or production key.

Production migrations run in one place, the API's boot (docs/deployment/migrations.md).
A `deploy-production` job used to run a second, unlocked `alembic upgrade head`
against production on every push to main, with the production DSN and keys as
GitHub secrets. This pins that it stays gone: the test job builds its own Postgres
service, so nothing in CI has a reason to name these secrets.
"""

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

_FORBIDDEN = re.compile(
    r"secrets\.(DATABASE_URL\w*|SECRET_KEY|BLIND_INDEX_KEY|FIELD_ENCRYPTION_KEY"
    r"|REDIS_URL|STRIPE_\w+|RESEND_\w+|TRACERFY_\w+)\b"
    r"|^\s*environment:\s*['\"]?production\b",
    re.MULTILINE,
)


def forbidden_references(yaml_text: str) -> list[str]:
    """Every production-secret reference outside a full-line comment."""
    live = "\n".join(line for line in yaml_text.splitlines() if not line.lstrip().startswith("#"))
    return [m.group(0).strip() for m in _FORBIDDEN.finditer(live)]


def test_no_workflow_references_a_production_database_or_key():
    files = sorted(WORKFLOWS.glob("*.y*ml"))
    assert files, f"no workflows found under {WORKFLOWS}"
    hits = {f.name: forbidden_references(f.read_text(encoding="utf-8")) for f in files}
    assert not {name: refs for name, refs in hits.items() if refs}


def test_the_scan_catches_the_job_it_exists_for():
    # The removed job, verbatim in the parts that matter.
    removed = """
  deploy-production:
    name: Run Migrations
    environment: production
    steps:
      - name: Run DB migrations
        env:
          DATABASE_URL_SYNC: ${{ secrets.DATABASE_URL_SYNC }}
          SECRET_KEY: ${{ secrets.SECRET_KEY || 'placeholder' }}
          BLIND_INDEX_KEY: ${{ secrets.BLIND_INDEX_KEY }}
        run: alembic upgrade head
"""
    assert forbidden_references(removed) == [
        "environment: production",
        "secrets.DATABASE_URL_SYNC",
        "secrets.SECRET_KEY",
        "secrets.BLIND_INDEX_KEY",
    ]


def test_the_scan_ignores_comments_and_the_secrets_ci_legitimately_uses():
    allowed = """
      # never put ${{ secrets.DATABASE_URL }} here
      env:
        RAILWAY_TOKEN: ${{ secrets.RAILWAY_TOKEN_STAGING }}
        GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
    environment: staging
"""
    assert forbidden_references(allowed) == []
