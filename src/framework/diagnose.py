"""Diagnose —— 实机诊断「读端汇总器」(给 claude code / AI 看的诊断包)。

读 ``logs/<run_id>.jsonl`` → 整理成一份一眼可消费的诊断包:

- run 元信息(task / 事件数 / 失败数 / last_scene)
- **scene 时序**(场景跳变点 → 看「卡在哪一步、场景有没有推进」)
- 失败按 ability 分组(复用 ``report.summary_text``)
- **事件时序**(关键事件按 ts 排列,带 reason/关键事实 → 看失败的前因后果)
- **可读截图清单**(failure 的 evidence + timeline 周期图)—— AI 直接 ``Read`` 这些路径看画面

**不调任何 LLM / API** —— 判读由 claude code 本体完成(它是眼睛 + 大脑,见
``任务进度.md`` 实机诊断 SOP)。本模块只做「把散在 jsonl 里的诊断素材汇成一份包」。

入口::

    python -m framework.diagnose <run_id>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from framework.report import summarize, summary_text

# 事件时序打印时跳过的「框架噪音」(非 ability 决策点)
_FRAMEWORK_NOISE = {"run_start", "run_end", "mount", "unmount", "task_return", "run_summary"}

# scene 时序跳过的 teardown 噪音（保留 run_start 初始 scene + 各 ability 事件 scene 跳变；
# teardown 事件 run_summary/run_end 往往 scene=None，会在时序末尾污染一个 None）
_SCENE_SKIP = {"run_end", "run_summary", "mount", "unmount", "task_return"}

# 时序行里优先暴露的关键事实(顺序即优先级,最多取 3 个)
_TIMELINE_FACTS = ("reason", "panel", "target", "picked", "picked_d", "dist",
                   "stuck_count", "count", "low_hp", "source", "mode", "attempt")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # 跳过坏行,诊断工具绝不因一行坏 jsonl 崩
    return out


def _scene_timeline(events: list[dict[str, Any]]) -> list[tuple[float, str | None]]:
    """场景跳变点 [(ts, scene)] —— 看 run 过程中场景怎么变(是否推进 / 卡死在某场景)。"""
    changes: list[tuple[float, str | None]] = []
    last: str | None = None
    for e in events:
        if e.get("event") in _SCENE_SKIP:
            continue
        s = e.get("scene")
        if s != last:
            changes.append((float(e.get("ts", 0.0)), s))
            last = s
    return changes


def _collect_evidence_paths(events: list[dict[str, Any]], run_debug: Path) -> list[str]:
    """从失败事件的 ``evidence`` 字段收集截图路径(去重,只列磁盘上存在的)。"""
    paths: list[str] = []
    seen: set[str] = set()
    for e in events:
        ev = e.get("evidence")
        if not isinstance(ev, str) or ev in seen:
            continue
        seen.add(ev)
        p = Path(ev)
        # evidence 可能是绝对/相对路径;相对时也试在 run_debug 下找
        if p.exists():
            paths.append(str(p))
        elif (run_debug / p.name).exists():
            paths.append(str(run_debug / p.name))
    return paths


def _collect_timeline_paths(run_debug: Path) -> list[str]:
    """``timeline_snap`` 守护存的周期截图(``debug/<run_id>/timeline/*.png``),若存在。"""
    tl = run_debug / "timeline"
    if not tl.is_dir():
        return []
    return sorted(str(p) for p in tl.glob("*.png"))


def _event_timeline_text(events: list[dict[str, Any]], max_lines: int = 50) -> str:
    """关键事件按 ts 排列 → 多行文本(失败/决策点带 reason + 关键事实)。"""
    lines: list[str] = []
    shown = 0
    for e in events:
        ev = e.get("event", "")
        if ev in _FRAMEWORK_NOISE:
            continue
        ability = e.get("ability") or e.get("task") or ""
        ok = e.get("ok")
        ts = float(e.get("ts", 0.0))
        tag = "失败" if ok is False else ("ok" if ok is True else "")
        facts = [f"{k}={e[k]}" for k in _TIMELINE_FACTS if k in e][:3]
        prefix = f"  {ts:8.2f}s [{ability}] {ev}" if ability else f"  {ts:8.2f}s {ev}"
        lines.append(f"{prefix} {tag} {' '.join(facts)}".rstrip())
        shown += 1
        if shown >= max_lines:
            lines.append(f"  ... (截断,共 {len(events)} 事件,详见 jsonl)")
            break
    return "\n".join(lines) if lines else "  (无 ability 事件)"


def diagnose_run(
    run_id: str,
    *,
    logs_dir: str | Path | None = None,
    debug_dir: str | Path | None = None,
) -> dict[str, Any]:
    """读 ``logs/<run_id>.jsonl`` → 打印诊断包 + 返回结构化结果。

    返回 ``{ok, run_id, task, summary, scene_timeline, failures, evidence, timeline}``。
    无 jsonl 时返回 ``{ok: False, reason: ...}``,不抛。
    """
    logs_dir = Path(logs_dir) if logs_dir else Path("logs")
    debug_dir = Path(debug_dir) if debug_dir else Path("debug")
    jsonl = logs_dir / f"{run_id}.jsonl"
    events = _read_jsonl(jsonl)
    if not events:
        msg = f"未找到或空: {jsonl}"
        print(msg)
        return {"ok": False, "run_id": run_id, "reason": "no_jsonl"}

    run_debug = debug_dir / run_id
    run_start = next((e for e in events if e.get("event") == "run_start"), {})
    task = run_start.get("task", "?")
    summary_ev = next((e for e in events if e.get("event") == "run_summary"), None)
    if summary_ev and summary_ev.get("summary"):
        summary = summary_ev["summary"]
    else:
        # 无预算 run_summary(任务崩在 teardown 前)→ 现场重算
        summary = summarize([e for e in events if e.get("event") != "run_summary"])

    scene_tl = _scene_timeline(events)
    evidence = _collect_evidence_paths(events, run_debug)
    timeline_imgs = _collect_timeline_paths(run_debug)
    failures = [
        {"ability": ab, **f}
        for ab, rec in summary.get("by_ability", {}).items()
        for f in rec.get("failures", [])
    ]

    # ── 打印诊断包 ──
    total = summary.get("total", 0)
    fail_total = summary.get("fail_total", 0)
    print(f"\n=== 诊断 run_id={run_id}  task={task} ===")
    print(f"事件 {total} / 失败 {fail_total} / last_scene={summary.get('last_scene')}")
    if scene_tl:
        seq = " → ".join(f"{s or 'None'}@{t:.1f}s" for t, s in scene_tl)
        print(f"场景时序: {seq}")
    else:
        print("场景时序: (事件无 scene 字段)")
    print()
    print(summary_text(summary))
    print()
    print("事件时序(关键):")
    print(_event_timeline_text(events))
    print()
    n_ev = len(evidence)
    n_tl = len(timeline_imgs)
    print(f"可读截图: {n_ev} evidence + {n_tl} timeline = {n_ev + n_tl} 张")
    for p in evidence:
        print(f"  [evidence] {p}")
    for p in timeline_imgs:
        print(f"  [timeline] {p}")
    if n_ev + n_tl == 0:
        print("  (无截图——失败未存证或 timeline 守护未挂;画面盲区)")

    return {
        "ok": True,
        "run_id": run_id,
        "task": task,
        "summary": summary,
        "scene_timeline": scene_tl,
        "failures": failures,
        "evidence": evidence,
        "timeline": timeline_imgs,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python -m framework.diagnose <run_id>")
        sys.exit(1)
    diagnose_run(sys.argv[1])
