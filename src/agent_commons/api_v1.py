from fastapi import APIRouter

from agent_commons.agents import router as agents_router
from agent_commons.communication import router as communication_router
from agent_commons.discovery import router as discovery_router
from agent_commons.freshness import router as freshness_router
from agent_commons.lineage import router as lineage_router
from agent_commons.migration import router as migration_router
from agent_commons.migration_lineage import router as continuity_router
from agent_commons.persistence import router as persistence_router
from agent_commons.recovery import router as recovery_router
from agent_commons.rotation import router as rotation_router

router = APIRouter(prefix="/api/v1")
router.include_router(agents_router)
router.include_router(rotation_router)
router.include_router(recovery_router)
router.include_router(lineage_router)
router.include_router(freshness_router)
router.include_router(migration_router)
router.include_router(continuity_router)
router.include_router(communication_router)
router.include_router(persistence_router)
router.include_router(discovery_router)
