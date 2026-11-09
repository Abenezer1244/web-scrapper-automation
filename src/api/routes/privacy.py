"""Public privacy requests from people in lead data (Phase 6 P8, design sections 3 and 9).

  POST /privacy/requests          the form. A submission IS the received request (the
                                  statutory clock starts on receipt): migration 137's
                                  privacy_web_submit() records it, unverified, and queues
                                  one email. Every valid submission gets the same 202.
  POST /privacy/requests/confirm  the emailed link's token (the frontend reads it from
                                  the URL fragment, which no server or Referer ever sees,
                                  and posts it here). Always the same 200.

Enumeration-safe by construction: nothing here reads a lead, a customer or an earlier
request; the database function does the same work for every valid submission (a repeat
within a day records nothing, silently) and returns nothing; confirm never says whether
a token existed, expired or was already used. Off until PRIVACY_INTAKE_ENABLED (404),
because the page copy awaits counsel (D9).

Abuse: every zone below fails CLOSED during a Redis outage (rate_limit._FALLBACK_ZONES):
per IP, per /24 (or /48), per address, per address domain, and one global daily budget,
so the form cannot be turned into a mail cannon.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware import audit_log, rate_limit
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


def _enabled() -> None:
    if not settings.PRIVACY_INTAKE_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


def _network(ip: str) -> str:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    prefix = 24 if addr.version == 4 else 48
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))


@router.post("/requests", status_code=status.HTTP_202_ACCEPTED)
async def submit_privacy_request(
    body: PrivacyRequestIn, request: Request, db: AsyncSession = Depends(get_db),
) -> dict:
    _enabled()
    # The address that gets the email: the agent's when an agent submits.
    to = str(body.agent.email if body.agent else body.email)
    to_hmac = blind_index(to)
    ip = client_ip(request)
    await rate_limit(request, zone="privacy_ip", identifier=ip)
    await rate_limit(request, zone="privacy_net", identifier=_network(ip))
    await rate_limit(request, zone="privacy_address", identifier=to_hmac)
    await rate_limit(request, zone="privacy_domain",
                     identifier=hashlib.sha256(to.rsplit("@", 1)[-1].lower().encode()).hexdigest())
    await rate_limit(request, zone="privacy_budget", identifier="global")

    contact = {"email": str(body.email)}
    if body.agent:
        contact["agent"] = body.agent.model_dump(mode="json")
    details = body.model_dump(mode="json", exclude={"kind", "email", "agent"})
    received = await db.execute(text("SELECT now()"))
    deadline = deadline_for(body.kind, received.scalar_one())
    await db.execute(
        text("SELECT privacy_web_submit(:k, :h, :c, :d, :agent, :dl, :rules)"),
        {"k": body.kind, "h": to_hmac,
         "c": encrypt_field(json.dumps(contact, sort_keys=True)),
         "d": encrypt_field(json.dumps(details, sort_keys=True)),
         "agent": body.agent is not None, "dl": deadline, "rules": RULES_VERSION},
    )
    await db.commit()
    # No address, name or request id in the audit trail: the kind is enough to count.
    audit_log(request, "privacy_request_submitted", None, detail=body.kind)
    return _RECEIVED


@router.post("/requests/confirm")
async def confirm_privacy_request(
    body: ConfirmIn, request: Request, db: AsyncSession = Depends(get_db),
) -> dict:
    _enabled()
    await rate_limit(request, zone="privacy_ip", identifier=client_ip(request))
    await db.execute(text("SELECT privacy_web_confirm(:h)"),
                     {"h": hashlib.sha256(body.token.encode()).hexdigest()})
    await db.commit()
    return _DONE
