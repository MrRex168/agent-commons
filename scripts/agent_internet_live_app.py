"""Loopback-only fixture entry point. Production starts agent_commons.main:app."""

import json
import os
import re
import ssl
from uuid import UUID

import httpx
from fastapi import Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from agent_commons import remote_resolution
from agent_commons.a2a_tasks import RPCRequest
from agent_commons.auth import get_current_agent
from agent_commons.config import settings
from agent_commons.db import get_db
from agent_commons.main import app
from agent_commons.models import Agent
from agent_commons.remote_models import RemoteAgentReference

ORIGIN = r"https://127\.0\.0\.1:[0-9]{1,5}"
database = make_url(settings.database_url)
if (
    database.host not in {"127.0.0.1", "localhost", "::1"}
    or not re.fullmatch(r"ac_live_[0-9a-f]{16}_(atlas|source|target)", database.database or "")
    or database.username != database.database
    or not re.fullmatch(ORIGIN, settings.api_url)
):
    raise RuntimeError("Demo fixture requires a provisioned local database")
allowed_cards = frozenset(json.loads(os.environ["AGENT_INTERNET_LIVE_CARDS"]))
links = json.loads(os.environ["AGENT_INTERNET_LIVE_LINKS"])
if any(
    not re.fullmatch(ORIGIN + r"/api/v1/agents/(atlas|nova)/agent-card", url)
    for url in allowed_cards
) or any(not re.fullmatch(ORIGIN + r"/a2a", link["endpoint"]) for link in links):
    raise RuntimeError("Demo fixture accepts only provisioned loopback routes")


def _validate_fixture_card(url: str) -> None:
    if url not in allowed_cards:
        raise ValueError("Card is outside the isolated demo fixture")


# Replace only network address admission in this dedicated subprocess. The
# production fetch, parsing, signature checks, and progression still run.
remote_resolution._validate_card_url = _validate_fixture_card
tls = ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])


class Dispatch(BaseModel):
    reference_id: UUID
    rpc: RPCRequest


@app.post("/__demo/dispatch", include_in_schema=False)
def dispatch(
    payload: Dispatch, caller: Agent = Depends(get_current_agent), db: Session = Depends(get_db)
) -> dict:
    reference = db.get(RemoteAgentReference, payload.reference_id)
    if (
        reference is None
        or reference.endpoint_status() != "authorized"
        or reference.preferred_transport != "JSONRPC"
    ):
        raise HTTPException(403, "A current sovereign JSONRPC location is required")
    for link in links:
        if (
            link["caller_id"] == str(caller.id)
            and link["recipient_root"] == reference.root_fingerprint
            and link["endpoint"] == reference.a2a_url
        ):
            break
    else:
        raise HTTPException(403, "Destination has no outbound enrollment")
    if payload.rpc.method not in ("message/send", "tasks/get"):
        raise HTTPException(403, "Demo dispatch supports only send and get")
    try:
        with httpx.Client(verify=tls, trust_env=False, follow_redirects=False, timeout=5) as client:
            response = client.post(
                link["endpoint"],
                headers={"Authorization": "Bearer " + link["token"]},
                json=payload.rpc.model_dump(),
            )
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "Enrolled peer task request failed") from exc
