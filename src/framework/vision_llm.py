"""视觉 LLM 判读 —— 让 AI「看见」游戏画面（CLAUDE.md §9 实机验证要求）。

背景（2026-08-15）：实机验证时 AI 只能靠模板匹配/OCR 间接推断画面，模板匹配猜谜式
归因效率低（如「传送点击未生效 vs 画面冻结」需反复推断）。本模块把截图 base64 提交
视觉大模型（BigModel Anthropic 兼容端点，``glm-4.5v``），返回自然语言画面描述。

- **定位靠 OCR/模板，理解靠 LLM**：按钮坐标等精确位置用 OCR/模板匹配（LLM 坐标粗糙）；
  LLM 负责「这是什么界面 / 卡在哪 / 画面里有什么」这类理解性问题。
- **判读失败不抛异常**：任何错误（无 key / 网络失败 / 模型错误）返回 ``"ERR ..."`` 串，
  调用方（守护/探针）不会因判读失败拖垮任务。
- **零配置**：复用环境变量 ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_AUTH_TOKEN``；
  模型可用 ``VISION_LLM_MODEL`` 覆盖（默认 ``glm-4.5v``）。

用法：
    from framework.vision_llm import look, describe_image
    text = look(ctx, "当前是什么界面？有什么按钮？")     # 截当前帧判读
    text = describe_image("debug/r_xxx/timeline/0001.png", "这张图卡在哪一步？")  # 判读已有截图
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from avc.image import IImageBuffer

    from framework.context import GameContext

DEFAULT_MODEL = "glm-4.5v"
DEFAULT_TIMEOUT_S = 60.0
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # base64 后 ~10.7MB；glm-4.5v 请求上限内


def _client():  # pragma: no cover - 纯 SDK 封装
    """懒加载 Anthropic 客户端（复用环境变量 base_url/auth_token）。失败抛异常由上层兜。"""
    from anthropic import Anthropic

    return Anthropic(timeout=DEFAULT_TIMEOUT_S)


def _png_bytes(frame: "IImageBuffer") -> bytes:
    """IImageBuffer → PNG bytes（avc save 需要文件路径，这里走临时文件）。"""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        path = f.name
    try:
        frame.save(path)
        return Path(path).read_bytes()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def describe_image(
    image: "str | Path | bytes | IImageBuffer",
    prompt: str,
    model: str | None = None,
) -> str:
    """判读一张图（PNG 路径 / bytes / IImageBuffer），返回模型文本。失败返回 ``"ERR ..."``。"""
    model = model or os.environ.get("VISION_LLM_MODEL", DEFAULT_MODEL)
    try:
        if isinstance(image, (str, Path)):
            data = Path(image).read_bytes()
        elif isinstance(image, (bytes, bytearray)):
            data = bytes(image)
        else:  # IImageBuffer
            data = _png_bytes(image)
        if not data:
            return "ERR empty_image"
        if len(data) > MAX_IMAGE_BYTES:
            return f"ERR image_too_large {len(data)}"
        b64 = base64.b64encode(data).decode()
        msg = _client().messages.create(
            model=model,
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        parts = [getattr(b, "text", "") for b in msg.content]
        text = "".join(parts).strip()
        return text or "ERR empty_response"
    except Exception as e:  # noqa: BLE001 — 判读失败不拖垮调用方
        return f"ERR {type(e).__name__}: {e}"


def look(ctx: "GameContext", prompt: str, frame: "IImageBuffer | None" = None) -> str:
    """截当前帧（或用传入帧）判读。判读本身发 observe 事件（ability=vision_llm）。"""
    buf = frame if frame is not None else ctx.capture()
    if buf is None:
        ctx.observe.event("vision.look", ability="vision_llm", ok=False, reason="no_frame")
        return "ERR no_frame"
    text = describe_image(buf, prompt)
    ok = not text.startswith("ERR")
    ctx.observe.event(
        "vision.look", ability="vision_llm", ok=ok,
        reason=None if ok else text[:120],
        _quiet=True,  # 判读内容由调用方（守护/probe）落盘，事件只记成败
    )
    return text


# 守护/诊断共用的标准判读 prompt（CLAUDE.md §9：当前在哪个界面 / 卡在哪步 / 下一步做什么）。
WATCH_PROMPT = (
    "这是原神游戏的一帧截图。请简短回答（每项一行）：\n"
    "1) 当前是什么界面（主界面/大地图/对话/菜单/加载/战斗/其他）？\n"
    "2) 画面里有哪些显著 UI 元素或文字（地名/按钮/弹窗）？\n"
    "3) 如果任务流程卡在某个界面，最可能卡在哪一步？"
)
