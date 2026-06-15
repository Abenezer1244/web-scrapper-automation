"""Helper modules for src/api/routes/auth.py.

Each module here holds the VERBATIM body logic extracted from an auth route
handler so that auth.py stays a thin layer of FastAPI route declarations. The
route decorators, signatures, and all security logic live unchanged in auth.py;
these helpers only hold the moved inner code, called by the handler with the
exact same objects it received.
"""
