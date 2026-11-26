"""分发任务领域服务：管理把发布版本分发给订阅方的批次调度、送达确认与失败重试，完成导出动作。

数据链路位置：主数据分发层执行端，负责消费发布版本与订阅过滤，
把送达回执写入回执中心。

队列口径：
- 分发任务队列只领取与发布批次关联、仍处于排队中的分发批次；
- 送达确认保存真实送达结果（确认方、确认时间），并以同一分发批次编号
  写回执中心，发布端可用同一编号找回分发端的确认证明；
- 重试不新建批次：在原批次上重置排队并累计尝试次数，前次派送结果保留；
- 已送达批次不允许再次领取/重试，迟到的回执补报不会触发重复送达。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DistTaskService(BaseModuleService):
    MODULE = "dist_task"
    ENTITY = "分发批次"
    OPERATION_TERM = "导出"
    LIST_FIELDS = ["分发批次", "订阅方", "记录范围", "分发状态", "回执状态"]
    REQUIRED_FIELDS = ["分发批次", "订阅方", "记录范围"]
    STATUS_ORDER = ["排队中", "分发中", "已送达", "分发失败"]
    ACTION_RULES = {
        "开始分发": "分发中",
        "重试分发": "排队中",
        "领取任务": "分发中",
        "确认送达": "已送达",
    }
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action in ("开始分发", "领取任务"):
            result = self.claim_batch(entry_id)
            if not result["ok"]:
                return None, result["message"]
            return store.find(self.MODULE, entry_id), result["message"]
        if action == "重试分发":
            result = self.retry_dispatch(entry_id)
            if not result["ok"]:
                return None, result["message"]
            return store.find(self.MODULE, entry_id), result["message"]
        if action == "确认送达":
            result = self.confirm_delivery(entry_id)
            if not result["ok"]:
                return None, result["message"]
            return store.find(self.MODULE, entry_id), result["message"]
        return super().run_action(entry_id, action)

    # ---- 业务扩展：版本分发与回执回收 ----

    def claim_batch(self, entry_id: int) -> dict[str, Any]:
        """队列领取分发批次：仅排队中的批次可被领取，已送达的不可再领。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "分发批次不存在"}
        if entry.get("作废"):
            return {"ok": False, "message": "分发批次已作废，不能领取"}
        if entry.get("status") == "已送达":
            return {
                "ok": False,
                "message": f"批次 {entry.get('分发批次', '')} 已送达，不能重复领取",
            }
        if entry.get("status") != "排队中":
            return {"ok": False, "message": f"批次当前为{entry.get('status')}，不可领取"}
        subscriber = entry.get("订阅方", "")
        sub = next((s for s in store.rows("subscriber_mgr")
                    if s.get("订阅方标识") == subscriber), None)
        if sub is None or sub.get("status") != "已生效":
            return {"ok": False, "message": f"订阅方 {subscriber} 未生效，拒绝分发"}
        entry["status"] = "分发中"
        entry["分发状态"] = "分发中"
        entry["pending"] = True
        entry["领取时间"] = datetime.now(timezone.utc).isoformat()
        return {
            "ok": True,
            "message": f"已领取批次 {entry.get('分发批次', '')}",
            "subscriber": subscriber,
            "scope": entry.get("记录范围"),
        }

    # 兼容旧调用名
    dispatch_batch = claim_batch

    def confirm_delivery(self, entry_id: int, confirmer: str = "分发执行端") -> dict[str, Any]:
        """确认真实送达并向回执中心登记，确认证明按同一批次编号可查回。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "分发批次不存在"}
        if entry.get("status") == "已送达":
            # 幂等：重复确认不重复写回执、不重复送达
            return {
                "ok": True,
                "message": "批次已送达，确认结果已保留，未重复送达",
                "receipt_id": self._receipt_id_for(entry.get("分发批次", "")),
                "idempotent": True,
            }
        if entry.get("status") != "分发中":
            return {"ok": False, "message": "批次不在分发中状态，无法确认送达"}
        now = datetime.now(timezone.utc).isoformat()
        entry["status"] = "已送达"
        entry["分发状态"] = "已送达"
        entry["回执状态"] = "部分回收"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["送达确认方"] = confirmer
        entry["送达时间"] = now
        receipts = store.rows("dist_receipt")
        receipt_id = max((int(r.get("id", 0)) for r in receipts), default=0) + 1
        receipts.append({
            "id": receipt_id,
            "回执编号": f"rc-{entry_id}-{receipt_id}",
            "分发批次": entry.get("分发批次", ""),
            "订阅方": entry.get("订阅方", ""),
            "回执状态": "部分回收",
            "差异说明": "送达确认已登记，待发布端核对",
            "status": "部分回收",
            "pending": True,
            "abnormal": False,
            "确认方": confirmer,
            "确认时间": now,
            "发布批次id": entry.get("发布批次id", 0),
        })
        return {
            "ok": True,
            "message": f"批次 {entry.get('分发批次', '')} 已确认真实送达，回执待核对",
            "receipt_id": receipt_id,
            "confirmer": confirmer,
        }

    def _receipt_id_for(self, batch_no: str) -> int:
        for receipt in store.rows("dist_receipt"):
            if receipt.get("分发批次") == batch_no:
                return int(receipt.get("id", 0))
        return 0

    def fail_and_requeue(self, entry_id: int, reason: str) -> dict[str, Any]:
        """标记分发失败，保留断点供重试（不新建批次）。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "分发批次不存在"}
        if entry.get("status") == "已送达":
            return {"ok": False, "message": "已送达批次不能改判失败"}
        entry["status"] = "分发失败"
        entry["分发状态"] = "分发失败"
        entry["abnormal"] = True
        entry["pending"] = True
        entry["失败原因"] = reason
        return {"ok": False, "message": f"分发失败: {reason}", "retryable": True}

    def retry_dispatch(self, entry_id: int) -> dict[str, Any]:
        """在原批次上重试：去重不建单，保留前次派送结果与同一批次编号。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "分发批次不存在"}
        if entry.get("作废"):
            return {"ok": False, "message": "分发批次已作废，不能重试"}
        if entry.get("status") == "已送达":
            return {"ok": False, "message": "批次已送达，无需重试，避免重复数据"}
        if entry.get("status") not in ("分发失败", "排队中", "分发中"):
            return {"ok": False, "message": f"批次当前为{entry.get('status')}，不可重试"}
        entry["尝试次数"] = int(entry.get("尝试次数", 1) or 1) + 1
        entry["status"] = "排队中"
        entry["分发状态"] = "排队中"
        entry["pending"] = True
        # 失败原因等前次派送结果原样保留，仅记录重试时间
        entry["重试时间"] = datetime.now(timezone.utc).isoformat()
        return {
            "ok": True,
            "message": (
                f"原批次 {entry.get('分发批次', '')} 第 {entry['尝试次数']} 次回到队列，"
                "前次派送结果已保留"
            ),
            "dist_id": entry_id,
            "attempt": entry["尝试次数"],
        }

    def find_by_batch_no(self, batch_no: str) -> dict[str, Any] | None:
        """按分发批次编号查回分发端记录（发布端对账入口）。"""
        return next((t for t in store.rows(self.MODULE)
                     if t.get("分发批次") == batch_no), None)

    def consume_publish_scope(self, entry_id: int) -> dict[str, Any] | None:
        """读取发布模块的版本范围，校验分发覆盖。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        scope = str(entry.get("记录范围", "")).replace("版本 ", "")
        return next((p for p in store.rows("golden_publish")
                     if p.get("版本标识") == scope), None)

    def resolve_backlog(self, entry_id: int) -> int:
        """按发布批次聚合积压分发任务数。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return 0
        publish_id = int(entry.get("发布批次id", 0) or 0)
        if publish_id:
            return sum(
                1 for t in store.rows(self.MODULE)
                if int(t.get("发布批次id", 0) or 0) == publish_id
                and t.get("status") in ("排队中", "分发中")
            )
        scope = str(entry.get("记录范围", "")).replace("版本 ", "")
        return sum(1 for t in store.rows(self.MODULE)
                   if str(t.get("记录范围", "")).endswith(scope)
                   and t.get("status") in ("排队中", "分发中"))

    def notify_subscriber_rollback(self, entry_id: int, new_version: str) -> str:
        """回滚后通知订阅方新版本口径。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return "分发批次不存在"
        subscriber = entry.get("订阅方", "")
        for sub in store.rows("subscriber_mgr"):
            if sub.get("订阅方标识") == subscriber:
                sub["pending"] = True
        entry["记录范围"] = f"版本 {new_version}"
        entry["status"] = "排队中"
        entry["pending"] = True
        return f"已通知 {subscriber} 切换到 {new_version}"
