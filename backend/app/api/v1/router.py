from fastapi import APIRouter

from app.api.v1 import (
    agents,
    approvals,
    auth,
    bounties,
    dashboard,
    memory,
    ops,
    orchestration,
    projects,
    providers,
    research,
    settings,
    skills,
    system,
    tasks,
    telegram,
    telegram_notify,
)

api_router = APIRouter()
api_router.include_router(system.router)
api_router.include_router(auth.router)
api_router.include_router(providers.router)
api_router.include_router(agents.router)
api_router.include_router(agents.tools_router)
api_router.include_router(tasks.router)
api_router.include_router(orchestration.router)
api_router.include_router(approvals.router)
api_router.include_router(skills.router)
api_router.include_router(memory.router)
api_router.include_router(projects.router)
api_router.include_router(bounties.router)
api_router.include_router(research.router)
api_router.include_router(dashboard.router)
api_router.include_router(settings.router)
api_router.include_router(ops.router)
api_router.include_router(telegram.router)
api_router.include_router(telegram_notify.router)
