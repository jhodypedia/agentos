from fastapi import APIRouter, Request

from app.api.deps import AdminPrincipal
from app.db.session import get_session_factory
from app.telegram.notifications import deliver

router = APIRouter(prefix="/telegram", tags=["telegram"])


@router.post("/notifications/deliver")
async def notifications(principal: AdminPrincipal, request: Request) -> dict[str, int]:
    count = await deliver(get_session_factory(), request.app.state.settings)
    return {"sent": count}
