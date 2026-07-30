"""Dump Celery task names + FastAPI routes for before/after refactor diffing."""
import sys

sys.path.insert(0, ".")
from main import app as fastapi_app

from src.workers import app as celery_app

celery_app.loader.import_default_modules()  # force-register tasks from include=[...]
tasks = sorted(n for n in celery_app.tasks if not n.startswith("celery."))
routes = sorted(
    f"{','.join(sorted(getattr(r, 'methods', []) or []))} {getattr(r, 'path', '')}"
    for r in fastapi_app.routes
)
print(f"=== CELERY TASKS ({len(tasks)}) ===")
for t in tasks:
    print(t)
print(f"=== API ROUTES ({len(routes)}) ===")
for r in routes:
    print(r)
