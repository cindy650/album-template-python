from fastapi import APIRouter

from backend.responses import api_success


router = APIRouter(tags=["configuration"])


@router.get("/personalization-rules")
async def get_personalization_rules():
    return api_success(
        {
            "enabled": False,
            "message": "订单已改为按邮件独立字段直接提取",
        },
        message="定制规则已停用",
    )
