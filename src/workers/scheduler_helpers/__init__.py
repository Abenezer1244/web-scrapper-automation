"""Beat-task body logic, extracted from src/workers/scheduler.py.

Celery task REGISTRATION stays in scheduler.py (decorator + name= string +
signature unchanged); these modules hold the moved task BODIES verbatim so
the registration surface is byte-identical before/after the decomposition.
Each task in scheduler.py is a thin wrapper that calls one `_impl` here.
"""
