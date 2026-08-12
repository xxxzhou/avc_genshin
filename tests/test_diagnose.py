"""Diagnose 读端汇总器的离线测试。

合成 jsonl + 合成截图,验证诊断包的各读端逻辑(不依赖实机/网络)。
可 ``python -m pytest tests/test_diagnose.py -v``。
"""

from __future__ import annotations

import json
from pathlib import Path


# ── 辅助 ──


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")


def _summary_with(failures: dict[str, list[dict]]) -> dict:
    """造一份 by_ability 结构的 summary。failures={ability:[{stage,reason,...}]}。"""
    by_ability = {}
    for ab, flist in failures.items():
        by_ability[ab] = {"ok": 0, "fail": len(flist), "failures": flist}
    return {"by_ability": by_ability, "last_scene": "main_ui", "total": 10, "fail_total": sum(len(v) for v in failures.values())}


class TestDiagnose:
    def test_no_jsonl_returns_not_ok(self, tmp_path):
        from framework.diagnose import diagnose_run

        r = diagnose_run("nonexistent", logs_dir=tmp_path / "logs", debug_dir=tmp_path / "debug")
        assert r["ok"] is False
        assert r["reason"] == "no_jsonl"

    def test_reads_summary_task_and_failures(self, tmp_path):
        from framework.diagnose import diagnose_run

        events = [
            {"ts": 0.0, "run_id": "r_test", "event": "run_start", "task": "auto_ley_line", "scene": "main_ui"},
            {"ts": 1.0, "event": "tp.resolve", "ability": "tp", "phase": "decide", "target": "花", "ok": True},
            {"ts": 2.0, "event": "tp.confirm", "ability": "tp", "phase": "act", "ok": False, "reason": "pin_covered"},
            {"ts": 3.0, "event": "run_summary", "summary": _summary_with({
                "tp": [{"stage": "tp.confirm", "reason": "pin_covered", "attempt": None, "evidence": None}],
            })},
            {"ts": 3.0, "event": "run_end"},
        ]
        _write_jsonl(tmp_path / "logs" / "r_test.jsonl", events)

        r = diagnose_run("r_test", logs_dir=tmp_path / "logs", debug_dir=tmp_path / "debug")

        assert r["ok"] is True
        assert r["task"] == "auto_ley_line"
        assert ("tp", "pin_covered") in [(f["ability"], f["reason"]) for f in r["failures"]]

    def test_scene_timeline_tracks_changes(self, tmp_path):
        from framework.diagnose import diagnose_run

        events = [
            {"ts": 0.0, "event": "run_start", "task": "t", "scene": "main_ui"},
            {"ts": 1.0, "event": "nav.step", "ability": "nav", "scene": "main_ui"},
            {"ts": 2.0, "event": "tp.open", "ability": "tp", "scene": "map"},
            {"ts": 3.0, "event": "fight.start", "ability": "fighter", "scene": "combat"},
            {"ts": 4.0, "event": "run_summary", "summary": _summary_with({})},
        ]
        _write_jsonl(tmp_path / "logs" / "r_test.jsonl", events)

        r = diagnose_run("r_test", logs_dir=tmp_path / "logs", debug_dir=tmp_path / "debug")

        scenes = [s for _, s in r["scene_timeline"]]
        assert scenes == ["main_ui", "map", "combat"]  # 跳变点(连续 main_ui 折叠)

    def test_collects_existing_evidence(self, tmp_path):
        from framework.diagnose import diagnose_run

        debug = tmp_path / "debug"
        ev_dir = debug / "r_test"
        ev_dir.mkdir(parents=True)
        ev_file = ev_dir / "2.00_evidence.png"
        ev_file.write_bytes(b"fake png")  # 真实占位图

        events = [
            {"ts": 0.0, "event": "run_start", "task": "t"},
            {"ts": 2.0, "event": "tp.confirm", "ability": "tp", "ok": False, "reason": "pin_covered",
             "evidence": str(ev_file)},
            {"ts": 3.0, "event": "run_summary", "summary": _summary_with(
                {"tp": [{"stage": "tp.confirm", "reason": "pin_covered", "attempt": None, "evidence": str(ev_file)}]})},
        ]
        _write_jsonl(tmp_path / "logs" / "r_test.jsonl", events)

        r = diagnose_run("r_test", logs_dir=tmp_path / "logs", debug_dir=debug)

        assert r["evidence"] == [str(ev_file)]

    def test_missing_evidence_file_skipped(self, tmp_path):
        """事件带 evidence 路径但磁盘上文件已删 → 跳过(不列不存在的)。"""
        from framework.diagnose import diagnose_run

        events = [
            {"ts": 0.0, "event": "run_start", "task": "t"},
            {"ts": 2.0, "event": "tp.confirm", "ability": "tp", "ok": False, "reason": "pin_covered",
             "evidence": "/nonexistent/deleted.png"},
            {"ts": 3.0, "event": "run_summary", "summary": _summary_with(
                {"tp": [{"stage": "tp.confirm", "reason": "pin_covered", "attempt": None, "evidence": None}]})},
        ]
        _write_jsonl(tmp_path / "logs" / "r_test.jsonl", events)

        r = diagnose_run("r_test", logs_dir=tmp_path / "logs", debug_dir=tmp_path / "debug")

        assert r["evidence"] == []

    def test_fallback_when_no_run_summary(self, tmp_path):
        """jsonl 无 run_summary(任务崩在 teardown 前)→ 现场用 report.summarize 重算。"""
        from framework.diagnose import diagnose_run

        events = [
            {"ts": 0.0, "event": "run_start", "task": "t"},
            {"ts": 1.0, "event": "tp.resolve", "ability": "tp", "ok": True},
            {"ts": 2.0, "event": "tp.confirm", "ability": "tp", "ok": False, "reason": "pin_covered"},
            # 故意无 run_summary / run_end
        ]
        _write_jsonl(tmp_path / "logs" / "r_test.jsonl", events)

        r = diagnose_run("r_test", logs_dir=tmp_path / "logs", debug_dir=tmp_path / "debug")

        assert r["ok"] is True
        assert r["summary"]["fail_total"] == 1
        assert "tp" in r["summary"]["by_ability"]

    def test_collects_timeline_dir(self, tmp_path):
        """debug/<run_id>/timeline/*.png(timeline 守护存的周期图)被收集。"""
        from framework.diagnose import diagnose_run

        debug = tmp_path / "debug"
        tl_dir = debug / "r_test" / "timeline"
        tl_dir.mkdir(parents=True)
        (tl_dir / "0001_main_ui.png").write_bytes(b"x")
        (tl_dir / "0002_combat.png").write_bytes(b"x")

        events = [{"ts": 0.0, "event": "run_start", "task": "t"},
                  {"ts": 1.0, "event": "run_summary", "summary": _summary_with({})}]
        _write_jsonl(tmp_path / "logs" / "r_test.jsonl", events)

        r = diagnose_run("r_test", logs_dir=tmp_path / "logs", debug_dir=debug)

        assert len(r["timeline"]) == 2
        assert all("timeline" in p for p in r["timeline"])

    def test_bad_json_line_skipped(self, tmp_path):
        """一行坏 jsonl 不致整个诊断崩(跳过坏行)。"""
        from framework.diagnose import diagnose_run

        log = tmp_path / "logs" / "r_test.jsonl"
        log.parent.mkdir(parents=True)
        good = json.dumps({"ts": 0.0, "event": "run_start", "task": "t"})
        log.write_text(good + "\n{坏 json}\n" + json.dumps({"ts": 1, "event": "run_summary", "summary": _summary_with({})}), encoding="utf-8")

        r = diagnose_run("r_test", logs_dir=tmp_path / "logs", debug_dir=tmp_path / "debug")
        assert r["ok"] is True
