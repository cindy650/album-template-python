from fastapi import APIRouter, HTTPException, Path, status

import qq_idleCopy as core
from backend.context import listener, parse_and_publish, poll_mailbox_once, tasks
from backend.responses import api_success
from backend.schemas import ParseOrderRequest


router = APIRouter(prefix="/tasks", tags=["任务"])


@router.get(
    "/mail-listener",
    summary="查询邮箱监听状态",
    description="查询 QQ 邮箱后台监听任务的运行状态、最近错误和最后处理的邮件 UID。",
)
async def get_listener_status():
    return api_success(listener.status(), message="监听状态查询成功")


@router.post(
    "/mail-listener/start",
    summary="启动邮箱监听",
    description="启动 QQ 邮箱后台监听任务；任务已运行时返回冲突错误。",
)
async def start_listener():
    if not listener.start():
        raise HTTPException(status_code=409, detail="邮箱监听任务已在运行")
    return api_success(listener.status(), message="邮箱监听已启动")


@router.post(
    "/mail-listener/stop",
    summary="停止邮箱监听",
    description="请求停止 QQ 邮箱后台监听任务；任务未运行时返回冲突错误。",
)
async def stop_listener():
    if not listener.stop():
        raise HTTPException(status_code=409, detail="邮箱监听任务未运行")
    return api_success(listener.status(), message="邮箱监听停止请求已发送")


@router.post(
    "/mail-poll",
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交单次邮箱拉取任务",
    description="异步提交一次邮箱拉取与订单处理任务，并返回可用于查询进度的任务 ID。",
)
async def submit_mail_poll():
    if listener.status()["running"]:
        raise HTTPException(
            status_code=409,
            detail="邮箱监听运行时不能执行单次拉取",
        )
    if tasks.has_active("mail-poll"):
        raise HTTPException(status_code=409, detail="单次拉取任务已在运行")
    return api_success(
        tasks.submit("mail-poll", poll_mailbox_once),
        message="单次拉取任务已提交",
        code="TASK_ACCEPTED",
        status_code=status.HTTP_202_ACCEPTED,
    )


@router.post(
    "/orders/parse-and-publish",
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交订单解析写入任务",
    description="异步解析邮件订单并写入 MySQL 订单库和已配置的外部服务，返回任务 ID。",
)
async def submit_order_task(payload: ParseOrderRequest):
    return api_success(
        tasks.submit(
            "order-parse-and-publish",
            lambda: parse_and_publish(payload),
        ),
        message="订单异步任务已提交",
        code="TASK_ACCEPTED",
        status_code=status.HTTP_202_ACCEPTED,
    )


@router.get(
    "/jobs",
    summary="查询后台任务列表",
    description="查询当前进程记录的后台任务及其执行状态和结果。",
)
async def list_jobs():
    return api_success(tasks.list(), message="任务列表查询成功")


@router.get(
    "/jobs/{task_id}",
    summary="查询后台任务详情",
    description="根据任务 ID 查询单个后台任务的状态、结果或错误信息。",
)
async def get_job(task_id: str = Path(description="后台任务 ID")):
    record = tasks.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return api_success(record, message="任务查询成功")
