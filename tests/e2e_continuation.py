"""端到端核对脚本：复现 v12 续派场景（A 已送达/回执迟到、B 未派出、C 无资格）。

对运行中的后端（默认 http://127.0.0.1:8021）打真实 HTTP 请求，仅用标准库。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8021"
# 每次运行用唯一 token，保证场景不与历史运行残留数据互相干扰。
RUN = str(int(time.time()))

failures: list[str] = []


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def reset_scenario() -> int:
    """每次运行新建一套带唯一标识的 v12 场景。"""
    status, body = call("POST", "/api/golden_publish", {
        "values": {
            "发布批次": f"金记录发布-v12-e2e-{RUN}",
            "实体编码": f"GOLD-V12-E2E-{RUN}",
            "金记录数": "128",
            "发布时间": "",
            "版本标识": "",
        },
    })
    assert body["ok"], body
    pub_id = int(body["entry"]["id"])
    status, body = call("POST", f"/api/golden_publish/{pub_id}/actions",
                        {"action": "发布批次"})
    assert body["ok"], body
    return pub_id


def ensure_subscriber(identifier: str, entity: str, fmt: str) -> None:
    keyword = urllib.parse.quote(identifier)
    _, page = call("GET", f"/api/subscriber_mgr?keyword={keyword}&size=200")
    if any(it.get("订阅方标识") == identifier for it in page["items"]):
        return
    status, body = call("POST", "/api/subscriber_mgr", {
        "values": {
            "订阅方标识": identifier,
            "订阅实体": entity,
            "分发格式": fmt,
            "订阅过滤": "",
            "生效状态": "",
        },
    })
    assert body["ok"], body
    sub_id = int(body["entry"]["id"])
    status, body = call("POST", f"/api/subscriber_mgr/{sub_id}/actions",
                        {"action": "生效订阅"})
    assert body["ok"], body


def list_tasks(pub_id: int) -> list[dict]:
    _, page = call("GET", "/api/dist_task?size=200")
    return sorted(
        (it for it in page["items"] if int(it.get("发布批次id", 0) or 0) == pub_id),
        key=lambda r: int(r["id"]),
    )


def task_for(pub_id: int, subscriber: str) -> dict:
    rows = [t for t in list_tasks(pub_id) if t["订阅方"] == subscriber and not t.get("作废")]
    assert len(rows) == 1, f"{subscriber} 有效分发批次数量异常: {len(rows)}"
    return rows[0]


# 健康检查
status, body = call("GET", "/api/health")
assert status == 200, "后端未启动"
print("后端健康检查通过\n")

pub_id = reset_scenario()
SUB_A = f"订阅方A-e2e-{RUN}"
SUB_B = f"订阅方B-e2e-{RUN}"
SUB_C = f"订阅账单C-e2e-{RUN}"
ENT = f"GOLD-V12-E2E-{RUN}"
ENT_C = f"ENT-BILL-E2E-{RUN}"
ensure_subscriber(SUB_A, ENT, "JSON")
ensure_subscriber(SUB_B, ENT, "JSON")
ensure_subscriber(SUB_C, ENT_C, "CSV")

# 1) 整组派送：只给 A、B 建单，C 被资格过滤
status, body = call("POST", f"/api/golden_publish/{pub_id}/fanout")
check("整组派送接口成功", status == 200, str(body))
check("仅 A/B 建单，C 被排除",
      SUB_C in body["excluded_subscribers"] and len(body["created"]) == 2,
      str(body))
check("dist_task 里没有 C 的分发",
      all(t["订阅方"] != SUB_C for t in list_tasks(pub_id)))
check("每单都回链发布批次 id 与版本",
      all(int(t["发布批次id"]) == pub_id and t["版本标识"] for t in list_tasks(pub_id)))

# 2) 重复整组派送（重试）：去重，不新建单
before_ids = {t["id"] for t in list_tasks(pub_id)}
status, body = call("POST", f"/api/golden_publish/{pub_id}/fanout")
check("重试整组派送成功", status == 200, str(body))
check("重试零新建、全部复用原批次",
      body["created"] == [] and sorted(body["reused"]) == sorted(before_ids), str(body))
check("重试后批次总数不变（去重）",
      {t["id"] for t in list_tasks(pub_id)} == before_ids)

# 3) 队列领取 A 并确认真实送达；B 保持排队
task_a = task_for(pub_id, SUB_A)
status, body = call("POST", f"/api/dist_task/{task_a['id']}/claim")
check("A 批次可被队列领取", status == 200 and body["ok"], str(body))
status, body = call("POST", f"/api/dist_task/{task_a['id']}/confirm",
                    {"confirmer": "分发队列worker-1"})
check("A 确认送达成功", status == 200 and body["ok"], str(body))
task_a = task_for(pub_id, SUB_A)
check("A 保存真实送达结果与确认方",
      task_a["status"] == "已送达" and task_a["送达确认方"] == "分发队列worker-1"
      and bool(task_a["送达时间"]), str(task_a))
check("送达后回执为部分回收（待发布端核对）", task_a["回执状态"] == "部分回收")

status, _ = call("POST", f"/api/dist_task/{task_a['id']}/claim")
check("已送达批次拒绝重复领取", status == 400)
status, _ = call("POST", f"/api/dist_task/{task_a['id']}/retry")
check("已送达批次拒绝重试，避免重复数据", status == 400)

# 重复确认幂等：统计回执行数
_, receipt_page = call("GET", "/api/dist_receipt?size=200")
rc_before = sum(1 for r in receipt_page["items"] if r["分发批次"] == task_a["分发批次"])
status, body = call("POST", f"/api/dist_task/{task_a['id']}/confirm",
                    {"confirmer": "分发队列worker-1"})
_, receipt_page = call("GET", "/api/dist_receipt?size=200")
rc_after = sum(1 for r in receipt_page["items"] if r["分发批次"] == task_a["分发批次"])
check("重复送达确认幂等，不重复写回执",
      status == 200 and body.get("idempotent") and rc_before == rc_after, str(body))

# 4) 续派明细：A 送达待确认，B 仍需派送
status, detail = call("GET", f"/api/golden_publish/{pub_id}/continuation")
check("续派明细接口成功", status == 200, str(detail))
check("明细计数：待确认 1、仍需派送 1、已送达 0",
      detail["counts"] == {"delivered": 0, "pending_verify": 1,
                           "pending_dispatch": 1, "missing": 0}, str(detail["counts"]))
pv = detail["pending_verify"][0]
check("送达待确认行定位 A 原单编号与确认方",
      pv["订阅方"] == SUB_A and pv["分发批次"] == task_a["分发批次"]
      and pv["receipt"]["confirmer"] == "分发队列worker-1"
      and pv["receipt"]["verified"] is False, str(pv))
pd_line = detail["pending_dispatch"][0]
check("仍需派送行定位 B 待领单",
      pd_line["订阅方"] == SUB_B and pd_line["分发状态"] == "排队中", str(pd_line))

# 5) 批量续派：不重发 A，B 沿用原批次
status, cont = call("POST", f"/api/golden_publish/{pub_id}/continue")
check("批量续派接口成功", status == 200, str(cont))
check("续派零新建，B 原批次保留",
      cont["created"] == [] and cont["retained"] == [task_for(pub_id, SUB_B)["id"]],
      str(cont))
check("A 仍只有原来一份分发批次且编号不变",
      task_for(pub_id, SUB_A)["分发批次"] == task_a["分发批次"])

# 关闭明细再打开：计数不变，不整组重提
status, detail2 = call("GET", f"/api/golden_publish/{pub_id}/continuation")
check("重开明细计数不变（未整组重提）",
      detail2["counts"] == detail["counts"], str(detail2["counts"]))
check("重开后总批次数仍是 2（A、B 各一份）", len(list_tasks(pub_id)) == 2)

# 6) B 领取并送达 v12
task_b = task_for(pub_id, SUB_B)
status, body = call("POST", f"/api/dist_task/{task_b['id']}/claim")
check("B 可领取 v12 批次", status == 200, str(body))
status, body = call("POST", f"/api/dist_task/{task_b['id']}/confirm",
                    {"confirmer": "分发队列worker-2"})
check("B 确认送达", status == 200 and body["ok"], str(body))

# 7) A 迟到回执补报：只核对回执，不重新分发
a_id_before = task_for(pub_id, SUB_A)["id"]
_, receipt_page = call("GET", "/api/dist_receipt?size=200")
a_receipt = next(r for r in receipt_page["items"] if r["分发批次"] == task_a["分发批次"])
status, body = call("POST", f"/api/dist_receipt/{a_receipt['id']}/actions",
                    {"action": "回执补报"})
check("迟到回执补报成功", status == 200 and body["ok"], str(body))
check("补报后 A 仍只有原批次、仍已送达",
      task_for(pub_id, SUB_A)["id"] == a_id_before
      and task_for(pub_id, SUB_A)["status"] == "已送达")
status, receipt = call("GET", f"/api/dist_receipt/{a_receipt['id']}")
check("补报后回执已回收（待核对工作清零）", receipt["status"] == "已回收", str(receipt))

status, detail3 = call("GET", f"/api/golden_publish/{pub_id}/continuation")
check("补报核对后 A 进已送达、A 的待核对清零（B 仍待核对）",
      detail3["counts"]["delivered"] == 1 and detail3["counts"]["pending_verify"] == 1
      and detail3["counts"]["pending_dispatch"] == 0, str(detail3["counts"]))
delivered_line = detail3["delivered"][0]
check("已送达组里是 A 的原单且回执已核对",
      delivered_line["订阅方"] == SUB_A
      and delivered_line["分发批次"] == task_a["分发批次"]
      and delivered_line["receipt"]["verified"] is True, str(delivered_line))
check("待确认组里只剩 B",
      len(detail3["pending_verify"]) == 1
      and detail3["pending_verify"][0]["订阅方"] == SUB_B,
      str(detail3["pending_verify"]))
check("C 始终没有任何分发",
      all(t["订阅方"] != SUB_C for t in list_tasks(pub_id)))

# 8) 同一编号跨端对账
status, body = call("GET", f"/api/dist_task/lookup?batch_no={task_a['分发批次']}")
check("按批次编号查回分发端确认证明",
      status == 200 and body["confirmer"] == "分发队列worker-1"
      and body["dist_status"] == "已送达", str(body))
status, body = call("GET", f"/api/dist_receipt/lookup?batch_no={task_a['分发批次']}")
check("按同一编号查回回执端核对证明",
      status == 200 and body["verified"] is True
      and body["receipt_id"] == a_receipt["回执编号"], str(body))

# 9) 失败重试在原批次累计，不换编号、不丢前次结果
# 新建一个发布批次专门验证失败重试，避免污染上面的已完成场景
_, body = call("POST", "/api/golden_publish", {
    "values": {"发布批次": f"金记录发布-retry-e2e-{RUN}", "实体编码": f"GOLD-RETRY-E2E-{RUN}",
               "金记录数": "3", "发布时间": "", "版本标识": ""},
})
rp_id = int(body["entry"]["id"])
call("POST", f"/api/golden_publish/{rp_id}/actions", {"action": "发布批次"})
ensure_subscriber(f"订阅方R-e2e-{RUN}", f"GOLD-RETRY-E2E-{RUN}", "JSON")
call("POST", f"/api/golden_publish/{rp_id}/fanout")
r_task = task_for(rp_id, f"订阅方R-e2e-{RUN}")
call("POST", f"/api/dist_task/{r_task['id']}/claim")
call("POST", f"/api/dist_task/{r_task['id']}/fail", {"reason": "网络抖动"})
status, r_task_now = call("GET", f"/api/dist_task/{r_task['id']}")
check("失败保留原因与原编号",
      r_task_now["status"] == "分发失败" and r_task_now["失败原因"] == "网络抖动")
status, body = call("POST", f"/api/dist_task/{r_task['id']}/retry")
check("原批次重试成功，尝试次数累计为 2", status == 200 and body["attempt"] == 2, str(body))
status, r2 = call("GET", f"/api/dist_task/{r_task['id']}")
check("重试不换编号、回到排队且保留失败原因",
      r2["分发批次"] == r_task_now["分发批次"] and r2["status"] == "排队中"
      and r2["尝试次数"] == 2 and r2["失败原因"] == "网络抖动", str(r2))

# 10) 通用动作入口
status, body = call("POST", f"/api/golden_publish/{pub_id}/actions", {"action": "续派"})
check("通用动作入口支持续派", status == 200 and body["ok"], str(body))
status, body = call("POST", f"/api/golden_publish/{pub_id}/actions", {"action": "整组派送"})
check("通用动作入口支持整组派送（幂等去重）", status == 200 and body["ok"], str(body))

print()
if failures:
    print(f"共 {len(failures)} 项失败: {failures}")
    sys.exit(1)
print("全部核对通过")
