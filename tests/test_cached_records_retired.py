"""GET /scrapers/{id}/records stays retired.

The endpoint served the shared county_records cache. It was retired in UX 3.13a: the frontend
stopped calling it in 3.12 (its records page is now a redirect), the cache is frozen, and the
endpoint read that shared PII table without the quota, payment or Starter-delay gates
(SECURITY-AUDIT S3-21). The test is synchronous, needs no fixture and no database, and runs no
app lifespan.
"""
import uuid

from fastapi.testclient import TestClient
from main import app
from starlette.routing import Match


def test_the_cached_records_endpoint_is_retired():
    """No route serves the path: FastAPI answers its default 404 before any dependency
    (no auth, no DB, no rate limit), and the OpenAPI contract no longer lists it."""
    path = f"/scrapers/{uuid.uuid4()}/records"
    response = TestClient(app).get(path)  # no `with`: no lifespan, no startup work
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "",
        "query_string": b"", "headers": [(b"host", b"test")], "server": ("test", 80),
        "client": ("127.0.0.1", 50000), "state": {}, "app": app,
    }
    assert [r for r in app.routes if r.matches(scope)[0] == Match.FULL] == []

    spec = app.openapi()
    assert "/scrapers/{config_id}/records" not in spec["paths"]
    assert not {"CachedRecordRow", "CachedResultsPage"} & set(spec["components"]["schemas"])
