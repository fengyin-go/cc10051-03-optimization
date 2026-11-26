"""金记录发布领域服务：管理金记录版本的发布批次、版本标识、扇出分发与回滚衔接，完成入库动作。

数据链路位置：主数据发布层，负责消费合并产物，把发布版本
扇出给订阅方并衔接版本回滚任务。

续派口径（与分发任务队列、回执中心共用同一批编号）：
- 分发资格只来自订阅了本发布批次实体的生效订阅方，其他实体的收件方不混入；
- 同一发布版本对同一订阅方最多保留一份有效分发批次，重试/续派按
  ``(发布批次id, 订阅方)`` 去重，前次派送结果原样保留；
- 续派明细由服务端逐条核对真实送达结果与回执核对状态后输出，
  页面只按核对结果展示，不重新整组提交。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 续派明细的三类安排：已送达 / 仍需派送 / 送达待确认
DELIVERED = "已送达"
PENDING_DISPATCH = "仍需派送"
PENDING_VERIFY = "送达待确认"


class GoldenPublishService(BaseModuleService):
    MODULE = "golden_publish"
    ENTITY = "发布批次"
    OPERATION_TERM = "入库"
    LIST_FIELDS = ["发布批次", "实体编码", "金记录数", "发布时间", "版本标识"]
    REQUIRED_FIELDS = ["发布批次", "实体编码", "金记录数"]
    STATUS_ORDER = ["待发布", "已发布", "已回滚"]
    ACTION_RULES = {
        "发布批次": "已发布",
        "回滚批次": "已回滚",
        "整组派送": "已发布",
        "续派": "已发布",
    }
    NEGATIVE_ACTIONS = ["回滚批次"]

    # ---- 动作入口：状态流转只在服务层落业务判断 ----

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "发布批次":
            result = self.release_version(entry_id)
            if not result["ok"]:
                return None, result["message"]
            return store.find(self.MODULE, entry_id), "发布批次已发布，版本标识已生成"
        if action == "整组派送":
            result = self.fanout_to_subscribers(entry_id)
            if not result["ok"]:
                return None, result["message"]
            return store.find(self.MODULE, entry_id), result["message"]
        if action == "续派":
            result = self.continue_dispatch(entry_id)
            if not result["ok"]:
                return None, result["message"]
            return store.find(self.MODULE, entry_id), result["message"]
        return super().run_action(entry_id, action)

    # ---- 业务扩展：金记录发布与分发衔接 ----

    def release_version(self, entry_id: int) -> dict[str, Any]:
        """发布金记录版本，写发布时间与版本标识。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "待发布":
            return {"ok": False, "message": "发布批次不在待发布状态"}
        now = datetime.now(timezone.utc).isoformat()
        version = f"pub-v{entry_id}-{now[:10]}"
        entry["发布时间"] = now
        entry["版本标识"] = version
        entry["status"] = "已发布"
        entry["pending"] = False
        return {
            "ok": True,
            "message": "发布批次已发布，版本标识已生成",
            "version": version,
            "released_at": now,
        }

    # -- 订阅资格与去重的统一口径 --

    def _eligible_subscribers(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        """分发资格：仅生效且订阅实体与发布实体一致的订阅方。

        其他实体的收件方（例如订阅账单实体的 C）不会进入本次派送名单。
        """
        entity = str(entry.get("实体编码", "")).strip()
        eligible: list[dict[str, Any]] = []
        for sub in store.rows("subscriber_mgr"):
            if sub.get("status") != "已生效":
                continue
            if str(sub.get("订阅实体", "")).strip() != entity:
                continue
            eligible.append(sub)
        return eligible

    def _tasks_of_publish(self, entry_id: int) -> list[dict[str, Any]]:
        """取回同一发布版本下的全部分发批次（按 id 排序，原批次编号可查回）。"""
        tasks = [
            t for t in store.rows("dist_task")
            if int(t.get("发布批次id", 0) or 0) == entry_id
        ]
        return sorted(tasks, key=lambda t: int(t.get("id", 0)))

    @staticmethod
    def _is_active_task(task: dict[str, Any]) -> bool:
        """有效分发：排队中/分发中/已送达都占住该订阅方的唯一名额。

        已作废（被去重替换或回滚）的批次不再占名额。
        """
        return task.get("status") in ("排队中", "分发中", "已送达") and not task.get("作废")

    def _active_task_for(
        self, entry_id: int, subscriber: str,
    ) -> dict[str, Any] | None:
        for task in self._tasks_of_publish(entry_id):
            if task.get("订阅方") == subscriber and self._is_active_task(task):
                return task
        return None

    def _next_dist_id(self) -> int:
        return max((int(r.get("id", 0)) for r in store.rows("dist_task")), default=0) + 1

    def fanout_to_subscribers(self, entry_id: int) -> dict[str, Any]:
        """把已发布版本整组派送给具备资格的订阅方，逐条去重。

        重试时对逐条生成的分发批次去重：已有有效批次的订阅方直接复用原单，
        前次派送结果（送达状态、回执、失败原因）原样保留，不重复建单。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "发布批次不存在"}
        if entry.get("status") != "已发布":
            return {"ok": False, "message": "发布批次不在已发布状态，无法派送"}
        tasks = store.rows("dist_task")
        version = str(entry.get("版本标识", ""))
        created: list[int] = []
        reused: list[int] = []
        skipped: list[str] = []
        for sub in self._eligible_subscribers(entry):
            subscriber = str(sub.get("订阅方标识", ""))
            existing = self._active_task_for(entry_id, subscriber)
            if existing is not None:
                # 同一版本同一订阅方已有有效分发：保留前次派送结果，绝不重复建单
                reused.append(int(existing.get("id", 0)))
                continue
            task_id = self._next_dist_id()
            tasks.append({
                "id": task_id,
                "分发批次": f"dist-pub{entry_id}-{task_id}",
                "订阅方": subscriber,
                "记录范围": f"版本 {version}",
                "分发状态": "排队中",
                "回执状态": "未回收",
                "status": "排队中",
                "pending": True,
                "abnormal": False,
                "发布批次id": entry_id,
                "发布批次": entry.get("发布批次", ""),
                "版本标识": version,
                "实体编码": entry.get("实体编码", ""),
                "尝试次数": 1,
                "作废": False,
                "送达确认方": "",
                "送达时间": "",
            })
            created.append(task_id)
        for sub in store.rows("subscriber_mgr"):
            if sub.get("status") != "已生效":
                continue
            if str(sub.get("订阅实体", "")).strip() != str(entry.get("实体编码", "")).strip():
                skipped.append(str(sub.get("订阅方标识", "")))
        entry["pending"] = bool(created) or bool(reused)
        return {
            "ok": True,
            "created": created,
            "reused": reused,
            "excluded_subscribers": skipped,
            "message": (
                f"整组派送完成：新建 {len(created)} 单，复用既有批次 {len(reused)} 单，"
                f"非本实体订阅方排除 {len(skipped)} 个"
            ),
        }

    def continuation_detail(self, entry_id: int) -> dict[str, Any]:
        """服务端逐条核对送达结果，输出续派明细。

        三类安排：
        - 已送达：分发端确认真实送达，且回执已核对；
        - 送达待确认：分发端已送达，但发布端尚未完成回执核对（含迟到反馈）；
        - 仍需派送：排队中/分发中/分发失败，分发队列后续可继续领取。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "发布批次不存在"}
        receipts = store.rows("dist_receipt")

        def receipt_proof(task: dict[str, Any]) -> dict[str, Any]:
            batch = str(task.get("分发批次", ""))
            matched = [r for r in receipts if r.get("分发批次") == batch]
            verified = next((r for r in matched if r.get("status") == "已回收"), None)
            latest = verified or (matched[-1] if matched else None)
            return {
                "has_receipt": latest is not None,
                "receipt_id": latest.get("回执编号", "") if latest else "",
                "receipt_status": latest.get("status", "") if latest else "未回收",
                "verified": verified is not None,
                "confirmer": str(task.get("送达确认方", "") or ""),
            }

        groups: dict[str, list[dict[str, Any]]] = {
            DELIVERED: [], PENDING_VERIFY: [], PENDING_DISPATCH: [],
        }
        for task in self._tasks_of_publish(entry_id):
            if task.get("作废"):
                continue
            proof = receipt_proof(task)
            line = {
                "dist_id": task.get("id"),
                "分发批次": task.get("分发批次", ""),
                "订阅方": task.get("订阅方", ""),
                "分发状态": task.get("status", ""),
                "回执状态": task.get("回执状态", "未回收"),
                "尝试次数": task.get("尝试次数", 1),
                "失败原因": task.get("失败原因", ""),
                "送达时间": task.get("送达时间", ""),
                "receipt": proof,
            }
            status = task.get("status")
            if status == "已送达":
                bucket = DELIVERED if proof["verified"] else PENDING_VERIFY
            else:
                bucket = PENDING_DISPATCH
            groups[bucket].append(line)

        eligible = [str(s.get("订阅方标识", "")) for s in self._eligible_subscribers(entry)]
        covered = {t.get("订阅方") for t in self._tasks_of_publish(entry_id) if not t.get("作废")}
        missing = [s for s in eligible if s not in covered]
        return {
            "ok": True,
            "publish_id": entry_id,
            "发布批次": entry.get("发布批次", ""),
            "版本标识": entry.get("版本标识", ""),
            "实体编码": entry.get("实体编码", ""),
            "eligible_subscribers": eligible,
            "missing_subscribers": missing,
            "delivered": groups[DELIVERED],
            "pending_verify": groups[PENDING_VERIFY],
            "pending_dispatch": groups[PENDING_DISPATCH],
            "counts": {
                "delivered": len(groups[DELIVERED]),
                "pending_verify": len(groups[PENDING_VERIFY]),
                "pending_dispatch": len(groups[PENDING_DISPATCH]),
                "missing": len(missing),
            },
        }

    def continue_dispatch(self, entry_id: int) -> dict[str, Any]:
        """批量续派：服务端核对后只为仍需派送/缺单的订阅方补单。

        已送达（含回执待核对）的订阅方不会再生成任何分发批次，
        页面关闭明细再打开也只能继续未完成的部分，不会整组重提。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "发布批次不存在"}
        if entry.get("status") != "已发布":
            return {"ok": False, "message": "发布批次不在已发布状态，无法续派"}
        detail = self.continuation_detail(entry_id)
        tasks = store.rows("dist_task")
        version = str(entry.get("版本标识", ""))
        created: list[int] = []
        retained: list[int] = []
        # 仍需派送：保留原批次编号，失败/在途单交给队列重试或继续领取
        for line in detail["pending_dispatch"]:
            task = store.find("dist_task", int(line["dist_id"]))
            if task is not None:
                task["尝试次数"] = int(task.get("尝试次数", 1) or 1) + 1
                if task.get("status") == "分发失败":
                    task["status"] = "排队中"
                    task["分发状态"] = "排队中"
                    task["pending"] = True
                    task["abnormal"] = False
                retained.append(int(line["dist_id"]))
        # 资格内还没有任何有效批次的订阅方：补一份排队单
        for subscriber in detail["missing_subscribers"]:
            if self._active_task_for(entry_id, subscriber) is not None:
                continue
            task_id = self._next_dist_id()
            tasks.append({
                "id": task_id,
                "分发批次": f"dist-pub{entry_id}-{task_id}",
                "订阅方": subscriber,
                "记录范围": f"版本 {version}",
                "分发状态": "排队中",
                "回执状态": "未回收",
                "status": "排队中",
                "pending": True,
                "abnormal": False,
                "发布批次id": entry_id,
                "发布批次": entry.get("发布批次", ""),
                "版本标识": version,
                "实体编码": entry.get("实体编码", ""),
                "尝试次数": 1,
                "作废": False,
                "送达确认方": "",
                "送达时间": "",
            })
            created.append(task_id)
        final_detail = self.continuation_detail(entry_id)
        return {
            "ok": True,
            "created": created,
            "retained": retained,
            "message": (
                f"续派完成：补建 {len(created)} 单，原批次继续派送 {len(retained)} 单；"
                f"已送达 {final_detail['counts']['delivered']} 单、"
                f"送达待确认 {final_detail['counts']['pending_verify']} 单均未重发"
            ),
            "detail": final_detail,
        }

    def rollback_version(self, entry_id: int, reason: str) -> dict[str, Any]:
        """回滚发布版本并触发版本回滚任务。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已发布":
            return {"ok": False, "message": "批次未发布，无法回滚"}
        entry["status"] = "已回滚"
        entry["abnormal"] = True
        rollbacks = store.rows("version_rollback")
        rb_id = max((int(r.get("id", 0)) for r in rollbacks), default=0) + 1
        rollbacks.append({
            "id": rb_id,
            "回滚任务": f"rb-{entry_id}-{rb_id}",
            "目标版本": entry.get("版本标识", ""),
            "回滚原因": reason,
            "执行状态": "待执行",
            "影响订阅方": "",
            "status": "待执行",
            "pending": True,
            "abnormal": True,
        })
        return {"ok": True, "rollback_id": rb_id, "message": "回滚任务已创建"}

    def consume_merge_output(self, entry_id: int) -> list[dict[str, Any]]:
        """读取合并模块产出的金记录，作为发布输入。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return []
        return [m for m in store.rows("golden_merge")
                if m.get("实体编码") == entry.get("实体编码") and m.get("status") == "已合并"]

    def emit_stats_snapshot(self, entry_id: int) -> int:
        """发布完成后向治理看板输黄金记录数快照。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return 0
        stats = store.rows("stats_overview")
        stat_id = max((int(r.get("id", 0)) for r in stats), default=0) + 1
        stats.append({
            "id": stat_id,
            "统计时间": datetime.now(timezone.utc).isoformat(),
            "实体编码": entry.get("实体编码", ""),
            "金记录数": str(entry.get("金记录数", "0")),
            "重复率": "0%",
            "分发延迟": "0",
            "status": "正常",
            "pending": False,
            "abnormal": False,
        })
        return stat_id

    def register_publish_lineage(self, entry_id: int) -> str | None:
        """登记发布批次到分发模块的血缘边。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        edges = store.rows("lineage_graph")
        edge_id = max((int(r.get("id", 0)) for r in edges), default=0) + 1
        edges.append({
            "id": edge_id,
            "血缘边标识": f"edge-pub-{edge_id}",
            "上游节点": entry.get("发布批次", ""),
            "下游节点": "dist_task",
            "变换规则": "版本分发",
            "登记时间": datetime.now(timezone.utc).isoformat(),
            "status": "已登记",
            "pending": False,
            "abnormal": False,
        })
        return f"edge-pub-{edge_id}"
