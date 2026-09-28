"""The run-refusal 402: a machine-readable code beside the unchanged sentence.

``POST /jobs`` and ``POST /batches`` refuse a run for the AI monthly limit or the
account rule (frozen, ended, over the record limit). ``detail`` stays the same
sentence it has always been, byte for byte, so a client that reads it as a
string keeps working; ``code`` and ``resumes_at`` are added BESIDE it, which
``HTTPException`` cannot do, hence the subclass and its handler.

The subclass matters twice: Starlette picks the handler by the exception's MRO,
so this one wins over FastAPI's HTTPException handler and the catch-all; and if
the handler were ever not registered, the exception still renders as today's
plain ``{"detail": "<sentence>"}``.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse

# The codes a run refusal can carry: the evaluator's codes that end in a prose
# 402. run_in_flight (409) and not_entitled (structured 402) have their own
# bodies. RunRefusalResponse.code is tested equal to this.
RUN_REFUSAL_CODES = ("ai_limit", "frozen", "ended", "over_limit")


class RunRefusedHTTPException(HTTPException):
    def __init__(
        self,
        code: str,
        message: str,
        resumes_at: datetime | None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=message, headers=headers
        )
        self.code = code
        self.resumes_at = resumes_at


def run_refusal_http(code: str | None, message: str, resumes_at: datetime | None) -> HTTPException:
    """The 402 for a refused run. A code outside RUN_REFUSAL_CODES (none can
    reach the callers today) keeps the plain sentence, so an unexpected code
    never becomes a machine code a client would act on."""
    if code in RUN_REFUSAL_CODES:
        return RunRefusedHTTPException(code, message, resumes_at)
    return HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=message)


async def run_refused_handler(request: Request, exc: RunRefusedHTTPException) -> JSONResponse:
    from src.api.schemas import RunRefusalResponse

    # Through the model, not a raw dict: JSONResponse cannot encode a datetime,
    # and this is the serializer GET /scrapers uses for the same resumes_at, so
    # the page and the 402 carry the identical string.
    body = RunRefusalResponse(detail=exc.detail, code=exc.code, resumes_at=exc.resumes_at)
    return JSONResponse(
        status_code=exc.status_code,
        content=body.model_dump(mode="json"),
        headers=exc.headers,
    )
