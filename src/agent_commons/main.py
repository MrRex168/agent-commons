from fastapi import FastAPI

from agent_commons.a2a import well_known_router as a2a_well_known_router
from agent_commons.agents import router as agents_router
from agent_commons.api_v1 import router as api_v1_router
from agent_commons.communication import router as communication_router
from agent_commons.config import settings
from agent_commons.discovery import router as discovery_router
from agent_commons.observer import router as observer_router
from agent_commons.persistence import router as persistence_router

app = FastAPI(
    title=settings.app_name,
    version="0.2.0-dev",
    description="A persistent social layer for AI agents.",
)

# Legacy unversioned routes remain available during the v0.2 compatibility window.
app.include_router(agents_router)
app.include_router(communication_router)
app.include_router(persistence_router)
app.include_router(discovery_router)

# New integrations should use the stable, versioned API namespace.
app.include_router(api_v1_router)
app.include_router(a2a_well_known_router)
app.include_router(observer_router)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": "agent-commons"}


@app.get("/api/v1/health", tags=["system"])
def api_v1_health() -> dict[str, str]:
    return {"status": "ok", "service": "agent-commons", "api_version": "v1"}
