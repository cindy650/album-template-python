from fastapi import APIRouter, HTTPException, status

import qq_idleCopy as core
from backend.context import listener, parse_and_publish, poll_mailbox_once, tasks
from backend.responses import api_success
from backend.schemas import ParseOrderRequest


router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("/mail-listener")
async def get_listener_status():
    return api_success(listener.status(), message="监听状态查询成功")


@router.post("/mail-listener/start")
async def start_listener():
    if not listener.start():
        raise HTTPException(status_code=409, detail="邮箱监听任务已在运行")
    return api_success(listener.status(), message="邮箱监听已启动")


@router.post("/mail-listener/stop")
async def stop_listener():
    if not listener.stop():
        raise HTTPException(status_code=409, detail="邮箱监听任务未运行")
    return api_success(listener.status(), message="邮箱监听停止请求已发送")


@router.post("/mail-poll", status_code=status.HTTP_202_ACCEPTED)
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


@router.post("/orders/parse-and-publish", status_code=status.HTTP_202_ACCEPTED)
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


@router.get("/jobs")
async def list_jobs():
    return api_success(tasks.list(), message="任务列表查询成功")


@router.get("/jobs/{task_id}")
async def get_job(task_id: str):
    record = tasks.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return api_success(record, message="任务查询成功")
