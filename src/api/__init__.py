from .routes.auth import router as auth_router
from .routes.batches import router as batches_router
from .routes.billing import router as billing_router
from .routes.jobs import router as jobs_router
from .routes.notifications import router as notifications_router
from .routes.scrapers import router as scrapers_router
from .routes.segments import router as segments_router
from .routes.webhooks import router as webhooks_router

__all__ = [
    "auth_router",
    "scrapers_router",
    "jobs_router",
    "billing_router",
    "webhooks_router",
    "segments_router",
    "batches_router",
    "notifications_router",
]
