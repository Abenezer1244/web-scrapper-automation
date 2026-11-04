"""Celery entry point for test_error_tracking_celery only (P0-03 5c).

``celery -A tests._p003_celery_app worker|beat`` loads the REAL app and the REAL
signal receivers from src.workers. Before Celery fires celeryd_init / beat_init,
this module initialises error tracking for the same service on a transport that
writes each serialised envelope to P003_ENVELOPE_DIR, so the receivers' own init
call is the idempotent no-op and every envelope a worker child or beat would send
lands on disk for the test to read. It adds one task that always fails and, for
beat, a schedule whose only entry cannot be serialised, since no production task
or entry fails on demand.
"""
import os
import uuid
from pathlib import Path

from sentry_sdk.transport import Transport

from src.utils.error_tracking import init_error_tracking
from src.workers import app


class DirTransport(Transport):
    def capture_envelope(self, envelope) -> None:
        out = Path(os.environ["P003_ENVELOPE_DIR"]) / f"{os.getpid()}-{uuid.uuid4().hex}.envelope"
        out.write_bytes(envelope.serialize())


SERVICE = os.environ["P003_SERVICE"]
if not init_error_tracking(SERVICE, transport=DirTransport()):
    raise RuntimeError("P003 harness: error tracking did not initialise")


@app.task(name="p003.fail_owner_lookup")
def fail_owner_lookup(address: str) -> None:
    raise ConnectionError("lookup failed for " + address)


if SERVICE == "beat":
    # scheduler.py assigns the production schedule on import; import it first so
    # this assignment is the one beat runs (it never publishes a real task).
    import src.workers.scheduler  # noqa: F401
    # object() pickles into the schedule file but cannot be JSON-encoded, so every
    # publish fails inside beat's apply_entry, which logs it instead of raising.
    app.conf.beat_schedule = {
        "p003-unpublishable": {"task": "p003.fail_owner_lookup", "schedule": 1.0, "args": (object(),)}
    }
