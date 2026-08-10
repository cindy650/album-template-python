from fastapi import APIRouter, Depends

from backend.api.routes import configuration, events, orders, system, tasks
from backend.security import verify_api_key


api_router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(verify_api_key)],
)
api_router.include_router(system.router)
api_router.include_router(configuration.router)
api_router.include_router(orders.router)
api_router.include_router(tasks.router)
api_router.include_router(events.router)
