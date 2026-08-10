from fastapi import APIRouter

import qq_idleCopy as core
from backend.responses import api_success


router = APIRouter(tags=["configuration"])


@router.get("/personalization-rules")
async def get_personalization_rules():
    return api_success(
        core.validate_personalization_config(core.read_personalization_config()),
        message="定制规则查询成功",
    )
