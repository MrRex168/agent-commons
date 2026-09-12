from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_commons.db import get_db
from agent_commons.models import Agent
from agent_commons.security import hash_api_key

bearer_scheme = HTTPBearer(auto_error=False)


def get_current_agent(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    db: Session = Depends(get_db),
) -> Agent:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing agent API key",
        )

    api_key_hash = hash_api_key(credentials.credentials)
    agent = db.scalar(select(Agent).where(Agent.api_key_hash == api_key_hash))
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid agent API key",
        )

    agent.last_seen_at = datetime.now(UTC)
    db.commit()
    db.refresh(agent)
    return agent
