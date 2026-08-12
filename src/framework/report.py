"""Report —— Observe 的**读端**（与 ``observe.py`` 写端分离）。

把时间线渲染成两类「AI / 人能直接消费」的输出：

- ``live_line(event)``：一条事件 → 一行终端实时打印（``runtime`` 注册的 stderr 订阅者用它）。
- ``summarize(timeline)`` / ``summary_text(summary)``：整 run 时间线 → 按 ``ability`` 分组的
  通过/失败摘要，写进 ``run_summary`` 事件 + teardown 时 stderr 打印。

设计目标（见 ``设计实现.md §2``）：跑 task → AI 拿 jsonl 里 ``run_summary`` 一行 → 按
``ability`` 分组 → 配合用户提示**点名坏在哪个 ability 的哪个 stage / reason**。
"""

from __future__ import annotations

from typing import Any

# 一行里优先暴露的「关键事实」字段（顺序即优先级）—— 让 AI 一眼看到决定性数值
_KEY_FACTS = (
    "reason", "panel", "mode", "action",
    "selected", "target", "nearest_tp", "count", "hit",
    "dist", "heading_diff", "stuck", "score", "pos", "slot",
)


def _fmt(v: Any) -> str:
    """紧凑格式化一个事实值（浮点 2 位、字符串截断、容器取长度）。"""
    if isinstance(v, float):
        return f"{v:.2f}"
    if isinstance(v, (list, tuple, dict)):
        return f"<{len(v)}>"
    s = str(v)
    if len(s) > 28:
        s = s[:25] + "..."
    return s


def live_line(event: dict[str, Any]) -> str:
    """一条事件 → 一行终端打印。

    形如：``[tp] tp.resolve 失败 sift_no_match target=蒙德城``、
    ``[nav] nav.step (省略12) ok dist=4.31``、``[fighter] survival.check 吃药``。

    ascii-safe 友好（不依赖终端 emoji 渲染），失败 / 节流采样必显。
    """
    ability = event.get("ability") or event.get("task") or "?"
    kind = event.get("event", "?")
    ok = event.get("ok")
    parts: list[str] = [f"[{ability}] {kind}"]
    if ok is False:
        parts.append("失败")
    elif ok is True:
        parts.append("ok")
    # 关键事实：按优先级取前几个（reason 优先，其余最多再补 2 个数值事实）
    facts: list[str] = []
    for k in _KEY_FACTS:
        if k in event:
            facts.append(f"{k}={_fmt(event[k])}")
        if len(facts) >= 3:
            break
    if facts:
        parts.append(" ".join(facts))
    if event.get("sampled"):
        parts.append(f"(省略{event.get('elided', 0)})")
    return " ".join(parts)


def summarize(timeline: list[dict[str, Any]]) -> dict[str, Any]:
    """整 run 时间线 → 按 ``ability`` 分组摘要。

    返回::

        {
            "by_ability": {
                "tp":   {"ok": 5, "fail": 2, "failures": [{"stage","reason","attempt","evidence"}]},
                "nav":  {"ok": 20, "fail": 1, "failures": [...]},
            },
            "last_scene": "MAIN_UI",
            "total": 40, "fail_total": 3,
        }

    ``ok`` 字段缺省（纯观测）的事件不计通过也不计失败，但仍进 total。
    """
    by_ability: dict[str, dict[str, Any]] = {}
    last_scene: Any = None
    total = 0
    fail_total = 0
    for e in timeline:
        if e.get("event") == "run_summary":  # 防重复自引用
            continue
        last_scene = e.get("scene") or last_scene
        total += 1
        ability = e.get("ability") or e.get("task") or "(framework)"
        ok = e.get("ok")
        rec = by_ability.setdefault(ability, {"ok": 0, "fail": 0, "failures": []})
        if ok is True:
            rec["ok"] += 1
        elif ok is False:
            rec["fail"] += 1
            fail_total += 1
            rec["failures"].append({
                "stage": e.get("event"),
                "reason": e.get("reason"),
                "attempt": e.get("attempt"),
                "evidence": e.get("evidence"),
            })
    return {
        "by_ability": by_ability,
        "last_scene": last_scene,
        "total": total,
        "fail_total": fail_total,
    }


def summary_text(summary: dict[str, Any]) -> str:
    """``summarize`` 结果 → 多行终端摘要（teardown 时 stderr 打印）。

    只列**有失败的 ability** + 全局计数；无失败时一行「全绿」。
    AI 拿这屏就能点名嫌疑 ability。
    """
    fail_total = summary.get("fail_total", 0)
    total = summary.get("total", 0)
    header = f"运行摘要: 共 {total} 事件, 失败 {fail_total}"
    if fail_total == 0:
        return header + "（无失败 ability）"
    lines = [header]
    for ability, rec in summary.get("by_ability", {}).items():
        if not rec["failures"]:
            continue
        reasons = ";".join(
            f"{f['stage']}:{f['reason'] or '?'}" for f in rec["failures"][:6]
        )
        lines.append(
            f"  [{ability}] ok={rec['ok']} 失败={rec['fail']} → {reasons}"
        )
    return "\n".join(lines)
