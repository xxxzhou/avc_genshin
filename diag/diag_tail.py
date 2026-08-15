"""临时诊断：打印 run 的完整事件流或最后 N 个。"""
import json
import sys

run_id = sys.argv[1] if len(sys.argv) > 1 else None
tail = int(sys.argv[2]) if len(sys.argv) > 2 else 60

lines = open(f"logs/{run_id}.jsonl", encoding="utf-8").read().strip().split("\n")
print(f"total events: {len(lines)}")
for ln in lines[-tail:]:
    try:
        e = json.loads(ln)
        t = e.get("t", 0.0)
        ev = e.get("event", "?")
        ab = e.get("ability", "")
        reason = e.get("reason", "")
        ok = e.get("ok", "")
        extra = {k: v for k, v in e.items()
                 if k not in ("t", "event", "ability", "reason", "ok")}
        print(f"{t:8.1f} [{ab}] {ev} ok={ok} reason={reason} {extra}")
    except Exception:
        pass