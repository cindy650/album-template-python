from fastapi import APIRouter, Depends

from backend.api.routes import (
    catalog,
    configuration,
    events,
    image_map,
    orders,
    system,
    tasks,
    template_catalog,
    template_imports,
)
from backend.security import verify_api_key


api_router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(verify_api_key)],
)
api_router.include_router(system.router)
api_router.include_router(configuration.router)
api_router.include_router(catalog.router)
api_router.include_router(template_catalog.router)
api_router.include_router(orders.router)
api_router.include_router(tasks.router)
api_router.include_router(events.router)
api_router.include_router(template_imports.router)
api_router.include_router(image_map.router)
