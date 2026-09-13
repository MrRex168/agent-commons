from fastapi import APIRouter

from agent_commons.agents import router as agents_router
from agent_commons.communication import router as communication_router
from agent_commons.discovery import router as discovery_router
from agent_commons.persistence import router as persistence_router

router = APIRouter(prefix="/api/v1")
router.include_router(agents_router)
router.include_router(communication_router)
router.include_router(persistence_router)
router.include_router(discovery_router)
