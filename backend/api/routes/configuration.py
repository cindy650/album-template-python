from fastapi import APIRouter

from backend.responses import api_success


router = APIRouter(tags=["配置"])


@router.get(
    "/personalization-rules",
    summary="查询订单定制规则状态",
    description="查询旧版订单定制规则是否启用；当前订单改为直接提取邮件中的独立字段。",
)
async def get_personalization_rules():
    return api_success(
        {
            "enabled": False,
            "message": "订单已改为按邮件独立字段直接提取",
        },
        message="定制规则已停用",
    )
