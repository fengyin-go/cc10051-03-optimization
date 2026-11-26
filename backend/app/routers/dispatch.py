"""发布-分发专用接口：续派明细、队列领取、真实送达与回执按编号对账。

通用资源路由仍承担列表/登记/动作；这里只放需要富返回或跨服务核对的接口，
业务判断全部留在领域服务，路由层不做状态裁决。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import service_for
from app.services.dist_receipt import DistReceiptService
from app.services.dist_task import DistTaskService
from app.services.golden_publish import GoldenPublishService

router = APIRouter(prefix="/api", tags=["发布分发协同"])


class OptionalBody(BaseModel):
    reason: str | None = None
    confirmer: str | None = None
    remark: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)


def _publish_service() -> GoldenPublishService:
    service = service_for("golden_publish")
    assert isinstance(service, GoldenPublishService)
    return service


def _dist_service() -> DistTaskService:
    service = service_for("dist_task")
    assert isinstance(service, DistTaskService)
    return service


def _receipt_service() -> DistReceiptService:
    service = service_for("dist_receipt")
    assert isinstance(service, DistReceiptService)
    return service


# ---- 发布端：整组派送 / 续派明细 / 批量续派 ----

@router.post("/golden_publish/{entry_id}/fanout")
def fanout(entry_id: int) -> dict[str, Any]:
    result = _publish_service().fanout_to_subscribers(entry_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "整组派送失败"))
    return result


@router.get("/golden_publish/{entry_id}/continuation")
def continuation(entry_id: int) -> dict[str, Any]:
    """服务端逐条核对送达结果，返回已送达/仍需派送/送达待确认明细。"""
    result = _publish_service().continuation_detail(entry_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message", "发布批次不存在"))
    return result


@router.post("/golden_publish/{entry_id}/continue")
def continue_dispatch(entry_id: int) -> dict[str, Any]:
    """批量续派：只补仍需派送的安排，已送达订阅方不重发。"""
    result = _publish_service().continue_dispatch(entry_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "续派失败"))
    return result


# ---- 分发端：任务队列领取、真实送达、失败与重试 ----

@router.post("/dist_task/{entry_id}/claim")
def claim(entry_id: int) -> dict[str, Any]:
    result = _dist_service().claim_batch(entry_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "领取失败"))
    return result


@router.post("/dist_task/{entry_id}/confirm")
def confirm(entry_id: int, body: OptionalBody | None = None) -> dict[str, Any]:
    confirmer = (body.confirmer if body and body.confirmer else "分发执行端")
    result = _dist_service().confirm_delivery(entry_id, confirmer=confirmer)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "送达确认失败"))
    return result


@router.post("/dist_task/{entry_id}/fail")
def fail(entry_id: int, body: OptionalBody | None = None) -> dict[str, Any]:
    reason = (body.reason if body and body.reason else "未知原因")
    result = _dist_service().fail_and_requeue(entry_id, reason)
    return {"ok": False, **result}


@router.post("/dist_task/{entry_id}/retry")
def retry(entry_id: int) -> dict[str, Any]:
    """原批次重试：不建单、不换编号，前次派送结果保留。"""
    result = _dist_service().retry_dispatch(entry_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "重试失败"))
    return result


@router.get("/dist_task/lookup")
def lookup_dist(batch_no: str) -> dict[str, Any]:
    """按分发批次编号查回分发端确认证明。"""
    task = _dist_service().find_by_batch_no(batch_no)
    if task is None:
        raise HTTPException(status_code=404, detail=f"分发批次 {batch_no} 不存在")
    return {
        "ok": True,
        "dist_id": task.get("id"),
        "batch_no": batch_no,
        "subscriber": task.get("订阅方", ""),
        "dist_status": task.get("status", ""),
        "receipt_status": task.get("回执状态", ""),
        "confirmer": task.get("送达确认方", ""),
        "delivered_at": task.get("送达时间", ""),
        "attempt": task.get("尝试次数", 1),
        "publish_id": task.get("发布批次id", 0),
    }


# ---- 回执端：按同一编号对账、迟到补报 ----

@router.get("/dist_receipt/lookup")
def lookup_receipt(batch_no: str) -> dict[str, Any]:
    result = _receipt_service().lookup_by_batch_no(batch_no)
    if result is None:
        raise HTTPException(status_code=404, detail=f"分发批次 {batch_no} 无回执记录")
    return {"ok": True, **result}
