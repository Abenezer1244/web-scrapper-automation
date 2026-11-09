"""Public privacy requests from people in lead data (Phase 6 P8, design sections 3 and 9).

  POST /privacy/requests          the form. A submission IS the received request (the
                                  statutory clock starts on receipt): migration 137's
                                  privacy_web_submit() records it, unverified, and queues
                                  one email. Every valid submission gets the same 202.
  POST /privacy/requests/confirm  the emailed link's token (the frontend reads it from
                                  the URL fragment, which no server or Referer ever sees,
                                  and posts it here). Always the same 200.

Enumeration-safe by construction: nothing here reads a lead, a customer or an earlier
request; a repeat within a day records nothing and is answered exactly like the first;
confirm never says whether a token existed, expired or was already used; both answer no
faster than _FLOOR_S, so the shorter repeat path cannot be told apart by timing. Nothing
personal is logged or audited (not even the submitter's IP). Off until
PRIVACY_INTAKE_ENABLED: 404 before the body is even validated, because the page copy
awaits counsel (D9).

Abuse: _gate charges every attempt, valid or not, to the caller's IP and /24 (or /48)
before the body is read. Every valid submission, new or a repeat, is then charged to its
address, its address's domain and one global daily budget, so a refusal never depends on
whether a request already exists (a repeat records and sends nothing, but costs the same).
Every privacy zone refuses (503) while Redis is down (rate_limit._HARD_FAIL_ZONES).
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware import rate_limit
from src.api.middleware.rate_limit import client_ip
from src.config import settings
from src.db.session import get_db
from src.privacy.deadlines import RULES_VERSION, deadline_for
from src.utils.crypto import blind_index, encrypt_field

router = APIRouter(prefix="/privacy", tags=["privacy"])

_RECEIVED = {"status": "received"}
_DONE = {"status": "ok"}


class Agent(BaseModel):
    """Someone acting for the person (D1: needs their signed permission)."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    authority: str = Field(min_length=1, max_length=1000)


class PrivacyRequestIn(BaseModel):
    """What the person asks and what lets the operator find their records. Every field
    is a matching hint, not proof of identity; the email only gets the link."""
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern=r"^(opt_out|delete|access|correct)$")
    email: EmailStr
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    property_address: str | None = Field(default=None, max_length=200)
    parcel: str | None = Field(default=None, max_length=40)
    county: str | None = Field(default=None, max_length=60)
    state: str | None = Field(default=None, pattern=r"^[A-Za-z]{2}$")
    details: str | None = Field(default=None, max_length=2000)
    agent: Agent | None = None

    @model_validator(mode="after")
    def _one_property_hint(self) -> PrivacyRequestIn:
        if not (self.property_address or self.parcel):
            raise ValueError("a property address or a parcel number is required")
        return self


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=256)


# Every answer takes at least this long: a repeat (same quotas, but no insert and no
# email) or a garbage token is otherwise measurably faster than a new request or a real
# confirmation.
_FLOOR_S = 0.6


def _network(ip: str) -> str:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    prefix = 24 if addr.version == 4 else 48
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))


async def _gate(request: Request) -> None:
    """Runs before the body is read or validated: off is a 404 whatever was sent, and
    every attempt, valid or not, spends the caller's per-IP and per-network budget."""
    if not settings.PRIVACY_INTAKE_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    ip = client_ip(request)
    await rate_limit(request, zone="privacy_ip", identifier=ip)
    await rate_limit(request, zone="privacy_net", identifier=_network(ip))


async def _floor(started: float) -> None:
    remaining = _FLOOR_S - (time.monotonic() - started)
    if remaining > 0:
        await asyncio.sleep(remaining)


@router.post("/requests", status_code=status.HTTP_202_ACCEPTED,
             dependencies=[Depends(_gate)])
async def submit_privacy_request(
    body: PrivacyRequestIn, request: Request, db: AsyncSession = Depends(get_db),
) -> dict:
    started = time.monotonic()
    # The address that gets the email: the agent's when an agent submits.
    to = str(body.agent.email if body.agent else body.email)
    to_hmac = blind_index(to)
    # Every valid submission, new or a repeat, is charged the same quotas BEFORE the
    # database is touched: a 429/503 must not depend on whether a request already
    # exists (Codex P8 round 2). A repeat records and sends nothing, but costs the same.
    domain = hashlib.sha256(to.rsplit("@", 1)[-1].lower().encode()).hexdigest()
    await rate_limit(request, zone="privacy_address", identifier=to_hmac)
    await rate_limit(request, zone="privacy_domain", identifier=domain)
    await rate_limit(request, zone="privacy_budget", identifier="global")
    contact = {"email": str(body.email)}
    if body.agent:
        contact["agent"] = body.agent.model_dump(mode="json")
    details = body.model_dump(mode="json", exclude={"kind", "email", "agent"})
    received = await db.execute(text("SELECT now()"))
    deadline = deadline_for(body.kind, received.scalar_one())
    # Its result (new or repeat) is deliberately unused: nothing downstream may differ.
    await db.execute(
        text("SELECT privacy_web_submit(:k, :h, :c, :d, :agent, :dl, :rules)"),
        {"k": body.kind, "h": to_hmac,
         "c": encrypt_field(json.dumps(contact, sort_keys=True)),
         "d": encrypt_field(json.dumps(details, sort_keys=True)),
         "agent": body.agent is not None, "dl": deadline, "rules": RULES_VERSION},
    )
    await db.commit()
    await _floor(started)
    return _RECEIVED


@router.post("/requests/confirm", dependencies=[Depends(_gate)])
async def confirm_privacy_request(
    body: ConfirmIn, db: AsyncSession = Depends(get_db),
) -> dict:
    started = time.monotonic()
    await db.execute(text("SELECT privacy_web_confirm(:h)"),
                     {"h": hashlib.sha256(body.token.encode()).hexdigest()})
    await db.commit()
    await _floor(started)
    return _DONE
