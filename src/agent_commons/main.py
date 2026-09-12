from fastapi import FastAPI

from agent_commons.config import settings

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="A persistent social layer for AI agents.",
)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": "agent-commons"}
