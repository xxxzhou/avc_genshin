"""vision_llm + llm_watch 守护离线测试（mock 视觉 API，零网络）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from framework import vision_llm
from framework.vision_llm import DEFAULT_MODEL, describe_image, look


class _FakeContent(list):
    pass


def _fake_response(text: str):
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


class TestDescribeImage:
    def test_path_png_happy_path(self, tmp_path, monkeypatch):
        png = tmp_path / "f.png"
        png.write_bytes(b"\x89PNG fake")
        captured = {}

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            @property
            def messages(self):
                return self

            def create(self, *, model, max_tokens, messages):
                captured.update(model=model, messages=messages)
                return _fake_response("这是大地图界面")

        monkeypatch.setattr(vision_llm, "_client", lambda: FakeClient())
        out = describe_image(png, "描述画面")
        assert out == "这是大地图界面"
        # 请求结构：base64 图片块 + 文本块
        assert captured["model"] == DEFAULT_MODEL
        blocks = captured["messages"][0]["content"]
        assert blocks[0]["type"] == "image"
        assert blocks[0]["source"]["media_type"] == "image/png"
        assert blocks[1]["type"] == "text" and blocks[1]["text"] == "描述画面"

    def test_model_override(self, tmp_path, monkeypatch):
        png = tmp_path / "f.png"
        png.write_bytes(b"x")
        monkeypatch.setenv("VISION_LLM_MODEL", "glm-5v")
        captured = {}

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            @property
            def messages(self):
                return self

            def create(self, *, model, **kw):
                captured["model"] = model
                return _fake_response("ok")

        monkeypatch.setattr(vision_llm, "_client", lambda: FakeClient())
        assert describe_image(png, "p") == "ok"
        assert captured["model"] == "glm-5v"

    def test_missing_file_returns_err(self, tmp_path):
        out = describe_image(tmp_path / "nope.png", "p")
        assert out.startswith("ERR")

    def test_api_failure_returns_err_not_raise(self, tmp_path, monkeypatch):
        png = tmp_path / "f.png"
        png.write_bytes(b"x")

        def boom():
            raise RuntimeError("no key")

        monkeypatch.setattr(vision_llm, "_client", boom)
        out = describe_image(png, "p")
        assert out.startswith("ERR") and "RuntimeError" in out

    def test_oversized_image_rejected(self, tmp_path, monkeypatch):
        png = tmp_path / "big.png"
        png.write_bytes(b"x" * (vision_llm.MAX_IMAGE_BYTES + 1))
        out = describe_image(png, "p")
        assert out.startswith("ERR image_too_large")

    def test_empty_response_text(self, tmp_path, monkeypatch):
        png = tmp_path / "f.png"
        png.write_bytes(b"x")

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            @property
            def messages(self):
                return self

            def create(self, **kw):
                return _fake_response("   ")

        monkeypatch.setattr(vision_llm, "_client", lambda: FakeClient())
        assert describe_image(png, "p").startswith("ERR empty_response")


class _FakeCtx:
    def __init__(self, frame):
        self._frame = frame
        self.events = []
        self.observe = SimpleNamespace(
            event=lambda kind, **kw: self.events.append((kind, kw)),
        )

    def capture(self):
        return self._frame


class TestLook:
    def test_look_no_frame(self):
        ctx = _FakeCtx(None)
        assert look(ctx, "p") == "ERR no_frame"
        assert ctx.events[0][1]["ok"] is False

    def test_look_ok_and_quiet(self, tmp_path, monkeypatch):
        png = tmp_path / "f.png"
        png.write_bytes(b"x")
        monkeypatch.setattr(vision_llm, "describe_image", lambda img, p, **kw: "主界面")
        frame = SimpleNamespace()
        ctx = _FakeCtx(frame)
        assert look(ctx, "p") == "主界面"
        kind, kw = ctx.events[0]
        assert kind == "vision.look" and kw["ok"] is True


class TestLlmWatchDaemon:
    def test_registered(self):
        from framework.daemons import get_daemon_class, list_daemons

        assert "llm_watch" in list_daemons()
        assert get_daemon_class("llm_watch") is not None

    def test_step_writes_txt_and_event(self, tmp_path, monkeypatch):
        from framework.daemons import llm_watch as lw

        monkeypatch.setattr(lw, "describe_image", lambda img, p: "1) 主界面\n2) 有小地图")
        LlmWatchDaemon = lw.LlmWatchDaemon
        daemon_inst = LlmWatchDaemon()

        llm_dir = tmp_path / "llm"
        events = []

        class _Obs:
            debug_dir = tmp_path

            def event(self, kind, **kw):
                events.append((kind, kw))

        class _Logger:
            @staticmethod
            def ts():
                return 1.0

        dctx = SimpleNamespace(
            ctx=SimpleNamespace(capture=lambda: SimpleNamespace()),
            shared=SimpleNamespace(frame=SimpleNamespace()),
            observe=_Obs(),
        )
        dctx.observe.logger = _Logger()

        asyncio.run(daemon_inst.step(dctx))
        files = list(llm_dir.glob("*.txt"))
        assert len(files) == 1
        assert files[0].read_text(encoding="utf-8").startswith("1) 主界面")
        kind, kw = events[0]
        assert kind == "llm.watch" and kw["ok"] is True and kw["seq"] == 0

    def test_interval_throttle(self, monkeypatch):
        from framework.daemons import llm_watch as lw

        LlmWatchDaemon = lw.LlmWatchDaemon
        calls = []

        def fake_describe(img, p):
            calls.append(1)
            return "ok"

        monkeypatch.setattr(lw, "describe_image", fake_describe)
        inst = LlmWatchDaemon()

        class _Obs:
            debug_dir = None

            def event(self, *a, **kw):
                pass

        dctx = SimpleNamespace(
            ctx=SimpleNamespace(capture=lambda: SimpleNamespace()),
            shared=SimpleNamespace(frame=SimpleNamespace()),
            observe=_Obs(),
        )
        asyncio.run(inst.step(dctx))  # 第 1 次：立即判读
        asyncio.run(inst.step(dctx))  # 第 2 次：间隔内 → 跳过
        assert len(calls) == 1

    def test_err_no_txt_but_event(self, tmp_path, monkeypatch):
        from framework.daemons import llm_watch as lw

        monkeypatch.setattr(lw, "describe_image", lambda img, p: "ERR RuntimeError: x")
        LlmWatchDaemon = lw.LlmWatchDaemon
        inst = LlmWatchDaemon()
        events = []

        class _Obs:
            debug_dir = tmp_path

            def event(self, kind, **kw):
                events.append((kind, kw))

        class _Logger:
            @staticmethod
            def ts():
                return 1.0

        dctx = SimpleNamespace(
            ctx=SimpleNamespace(capture=lambda: SimpleNamespace()),
            shared=SimpleNamespace(frame=SimpleNamespace()),
            observe=_Obs(),
        )
        dctx.observe.logger = _Logger()
        asyncio.run(inst.step(dctx))
        assert not (tmp_path / "llm").exists() or not list((tmp_path / "llm").glob("*.txt"))
        assert events[0][1]["ok"] is False
        assert "RuntimeError" in events[0][1]["reason"]
