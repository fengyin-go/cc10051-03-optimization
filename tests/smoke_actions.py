"""通用 /actions 入口与列表接口冒烟：覆盖页面按钮实际走的链路。"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8021"
RUN = str(int(time.time()))
failures: list[str] = []


def call(method: str, path: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"ok": False, "message": raw}


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f" -- {detail}"))
    if not cond:
        failures.append(name)


for m in ("golden_publish", "dist_task", "dist_receipt", "subscriber_mgr"):
    s, b = call("GET", f"/api/{m}?size=200")
    check(f"列表 /api/{m} 正常", s == 200 and "items" in b, str(b))

# 完整页面动作链路
s, b = call("POST", "/api/golden_publish", {"values": {
    "发布批次": f"动作链路冒烟-{RUN}", "实体编码": f"ENT-SMOKE-{RUN}", "金记录数": "5",
    "发布时间": "", "版本标识": ""}})
pid = b["entry"]["id"]
s, b = call("POST", f"/api/golden_publish/{pid}/actions", {"action": "发布批次"})
check("发布批次动作生成版本", b["ok"] and b["entry"]["版本标识"].startswith("pub-v"), str(b))

s, b = call("POST", "/api/subscriber_mgr", {"values": {
    "订阅方标识": f"冒烟订阅方S-{RUN}", "订阅实体": f"ENT-SMOKE-{RUN}", "分发格式": "JSON",
    "订阅过滤": "", "生效状态": ""}})
sid = b["entry"]["id"]
s, b = call("POST", f"/api/subscriber_mgr/{sid}/actions", {"action": "生效订阅"})
check("订阅生效", b["ok"], str(b))

s, b = call("POST", f"/api/golden_publish/{pid}/actions", {"action": "整组派送"})
check("整组派送动作", b["ok"], str(b))
s, detail = call("GET", f"/api/golden_publish/{pid}/continuation")
tid = detail["pending_dispatch"][0]["dist_id"]

s, b = call("POST", f"/api/dist_task/{tid}/actions", {"action": "领取任务"})
check("领取任务动作", b["ok"] and b["entry"]["status"] == "分发中", str(b))
s, b = call("POST", f"/api/dist_task/{tid}/actions", {"action": "确认送达"})
check("确认送达动作", b["ok"] and b["entry"]["status"] == "已送达", str(b))
batch_no = b["entry"]["分发批次"]

s, rp = call("GET", "/api/dist_receipt?size=200")
rc = next(r for r in rp["items"] if r["分发批次"] == batch_no)
s, b2 = call("POST", f"/api/dist_receipt/{rc['id']}/actions", {"action": "核对回执"})
check("核对回执动作", b2["ok"] and b2["entry"]["status"] == "已回收", str(b2))

# 非法流转必须返回结构化失败而不是 500
s, b3 = call("POST", f"/api/dist_task/{tid}/actions", {"action": "领取任务"})
check("已送达再领取被结构化拒绝（非500）", s == 200 and not b3["ok"] and b3["message"], str(b3))
s, b4 = call("POST", f"/api/dist_task/{tid}/actions", {"action": "重试分发"})
check("已送达重试被结构化拒绝（非500）", s == 200 and not b4["ok"] and b4["message"], str(b4))

# 种子旧批次（无发布关联、订阅方是样例数据）走重试动作也不能 500
s, b5 = call("POST", "/api/dist_task/1/actions", {"action": "重试分发"})
check("种子旧批次重试不抛500", s == 200 and isinstance(b5.get("message"), str), str(b5))

# 回执补报动作
s, b6 = call("POST", f"/api/dist_receipt/{rc['id']}/actions", {"action": "回执补报"})
check("已核对回执的迟到补报幂等通过", b6["ok"], str(b6))

# 未知动作
s, b7 = call("POST", f"/api/golden_publish/{pid}/actions", {"action": "不存在的动作"})
check("未知动作被拒绝", not b7["ok"], str(b7))

print()
print("全部冒烟通过" if not failures else f"失败: {failures}")
raise SystemExit(1 if failures else 0)
