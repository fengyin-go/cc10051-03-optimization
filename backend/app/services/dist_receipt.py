"""分发回执中心领域服务：管理订阅方分发回执的回收核对、差异标记与重发请求。

数据链路位置：分发层对账端，负责消费分发批次的送达结果，
把回执差异回写给分发任务与订阅方。

核对口径：
- 回执与分发端确认证明通过同一个分发批次编号关联，发布端汇总续派回执时
  可用该编号找回分发端的确认方、确认时间；
- 迟到的回执补报只更新/补齐回执记录与核对状态，绝不重新触发分发，
  已经送达的订阅方不会因补报再次收到相同数据；
- 核对以分发端真实送达状态为准：已送达才能核对为已回收。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DistReceiptService(BaseModuleService):
    MODULE = "dist_receipt"
    ENTITY = "回执记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["回执编号", "分发批次", "订阅方", "回执状态", "差异说明"]
    REQUIRED_FIELDS = ["回执编号", "分发批次", "订阅方"]
    STATUS_ORDER = ["已回收", "部分回收", "未回收"]
    ACTION_RULES = {
        "重发回执请求": "部分回收",
        "标记差异": "未回收",
        "核对回执": "已回收",
        "回执补报": "已回收",
    }
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "核对回执":
            result = self.verify_receipt(entry_id)
            if not result["ok"]:
                return None, result["message"]
            return store.find(self.MODULE, entry_id), result["message"]
        if action == "回执补报":
            result = self.report_late_receipt(entry_id)
            if not result["ok"]:
                return None, result["message"]
            return store.find(self.MODULE, entry_id), result["message"]
        return super().run_action(entry_id, action)

    # ---- 业务扩展：回执核对与差异处置 ----

    def _find_task(self, batch: str) -> dict[str, Any] | None:
        return next((t for t in store.rows("dist_task")
                     if t.get("分发批次") == batch), None)

    def verify_receipt(self, entry_id: int) -> dict[str, Any]:
        """核对回执：按分发批次编号校验分发端真实送达结果。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "回执记录不存在"}
        batch = entry.get("分发批次", "")
        task = self._find_task(batch)
        if task is None:
            entry["status"] = "未回收"
            entry["差异说明"] = "分发批次不存在"
            return {"ok": False, "message": f"分发批次 {batch} 不存在"}
        if task.get("status") == "已送达":
            entry["status"] = "已回收"
            entry["回执状态"] = "已回收"
            entry["pending"] = False
            entry["abnormal"] = False
            entry["核对时间"] = datetime.now(timezone.utc).isoformat()
            entry["确认方"] = task.get("送达确认方", entry.get("确认方", ""))
            # 回执核对通过后同步分发批次的回执状态，不改变送达事实
            task["回执状态"] = "已回收"
            return {
                "ok": True,
                "message": "回执与分发结果一致",
                "confirmer": entry.get("确认方", ""),
                "batch_no": batch,
            }
        entry["status"] = "部分回收"
        entry["差异说明"] = f"分发状态为 {task.get('status')}，仍有待核对工作"
        return {"ok": False, "message": "回执与分发结果不一致，仍有待核对工作"}

    def mark_diff(self, entry_id: int, diff: str) -> str:
        """标记回执差异并把分发任务置为失败重试。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return "回执记录不存在"
        entry["status"] = "未回收"
        entry["差异说明"] = diff
        entry["abnormal"] = True
        batch = entry.get("分发批次", "")
        for task in store.rows("dist_task"):
            if task.get("分发批次") == batch and task.get("status") != "已送达":
                task["status"] = "分发失败"
                task["abnormal"] = True
        return f"差异已标记: {diff}"

    def resend_request(self, entry_id: int) -> dict[str, Any]:
        """重发回执请求并等待订阅方回传（只动回执，不重发数据）。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "回执记录不存在"}
        entry["status"] = "部分回收"
        entry["pending"] = True
        entry["重发时间"] = datetime.now(timezone.utc).isoformat()
        return {"ok": True, "message": "回执请求已重发，未触发任何分发"}

    def report_late_receipt(self, entry_id: int) -> dict[str, Any]:
        """迟到回执补报：补齐核对材料，不重新分发、不新建分发批次。

        已送达的订阅方只完成回执核对闭环；若分发端尚未送达，补报仅登记为
        部分回收，等待真实送达，不能凭补报倒推送达。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "回执记录不存在"}
        batch = entry.get("分发批次", "")
        task = self._find_task(batch)
        now = datetime.now(timezone.utc).isoformat()
        entry["补报时间"] = now
        if task is None:
            entry["status"] = "未回收"
            entry["差异说明"] = "补报找不到对应分发批次"
            return {"ok": False, "message": f"分发批次 {batch} 不存在，补报无法核对"}
        # 关键约束：任何情况下补报都不改分发批次状态、不触发再次派送
        if task.get("status") == "已送达":
            entry["status"] = "已回收"
            entry["回执状态"] = "已回收"
            entry["pending"] = False
            entry["差异说明"] = "迟到回执补报，已与分发端送达证明核对"
            entry["确认方"] = task.get("送达确认方", entry.get("确认方", ""))
            entry["核对时间"] = now
            task["回执状态"] = "已回收"
            return {
                "ok": True,
                "message": "迟到回执已核对，未重新分发，订阅方未重复收数",
                "batch_no": batch,
                "confirmer": entry.get("确认方", ""),
            }
        entry["status"] = "部分回收"
        entry["差异说明"] = f"补报先行，分发端状态为 {task.get('status')}，待真实送达"
        return {
            "ok": False,
            "message": "补报已登记，分发端尚未送达，等待真实送达后再核对",
        }

    def lookup_by_batch_no(self, batch_no: str) -> dict[str, Any] | None:
        """发布端汇总续派回执时，用同一分发批次编号找回确认证明。"""
        task = self._find_task(batch_no)
        receipt = next((r for r in store.rows(self.MODULE)
                        if r.get("分发批次") == batch_no), None)
        if task is None and receipt is None:
            return None
        return {
            "batch_no": batch_no,
            "dist_status": task.get("status", "") if task else "",
            "confirmer": (task or {}).get("送达确认方", ""),
            "delivered_at": (task or {}).get("送达时间", ""),
            "receipt_id": (receipt or {}).get("回执编号", ""),
            "receipt_status": (receipt or {}).get("status", "未回收"),
            "verified": bool(receipt and receipt.get("status") == "已回收"),
        }

    def consume_dist_results(self, entry_id: int) -> list[dict[str, Any]]:
        """读取分发模块的送达批次，作为核对输入。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return []
        subscriber = entry.get("订阅方", "")
        return [t for t in store.rows("dist_task")
                if t.get("订阅方") == subscriber and t.get("status") in ("已送达", "分发失败")]

    def stats_by_subscriber(self) -> dict[str, dict[str, int]]:
        """按订阅方统计回执回收情况，供对账看板消费。"""
        stats: dict[str, dict[str, int]] = {}
        for row in store.rows(self.MODULE):
            subscriber = row.get("订阅方", "未知")
            bucket = stats.setdefault(subscriber, {"已回收": 0, "部分回收": 0, "未回收": 0})
            status = row.get("status", "未回收")
            if status in bucket:
                bucket[status] += 1
        return stats

    def notify_receipt_gap(self, entry_id: int) -> str:
        """把回执缺口通知订阅方重新上传。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return "回执记录不存在"
        subscriber = entry.get("订阅方", "")
        for sub in store.rows("subscriber_mgr"):
            if sub.get("订阅方标识") == subscriber:
                sub["pending"] = True
        return f"已通知 {subscriber} 补传回执"
