from contextlib import asynccontextmanager

from fastapi import FastAPI

from agent_commons import models  # noqa: F401
from agent_commons.agents import router as agents_router
from agent_commons.communication import router as communication_router
from agent_commons.config import settings
from agent_commons.db import Base, engine
from agent_commons.discovery import router as discovery_router
from agent_commons.observer import router as observer_router
from agent_commons.persistence import router as persistence_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="A persistent social layer for AI agents.",
    lifespan=lifespan,
)

app.include_router(agents_router)
app.include_router(communication_router)
app.include_router(persistence_router)
app.include_router(discovery_router)
app.include_router(observer_router)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": "agent-commons"}
