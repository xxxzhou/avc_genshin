"""GameContext —— avc 实例管理（docs/design/01、IMPLEMENTATION §4.1）。

任务侧的 ``ctx``：持有 avc 的 sc/ic/tm/ocr 四件套，提供截图与基础（拟人化）输入。
高层语义在 ``high_level_api.py``（g.*），任务组合在 ``ctx.run``（阶段四）。

实现要点（对照真实 avc 绑定 swig/python/avc/）：
- avc 懒导入：实例化时才 ``from avc import Input, Vision, Image``，使本模块在
  无 avc 环境仍可 import（便于单测数据结构）。
- ``sc.getBuffer()`` 返回**原生** IImageBuffer（借用，生命周期归 sc），须用
  ``Image.IImageBuffer(native)`` 包一层才有 ``to_bytes()`` / ``crop()``。
- ``ic.click`` 用三参形式 ``click(x, y, MouseButton.left)``（非 ``click(x, y)``）。
- 拟人化由**框架层**实现（avc 无 setHumanize，见 utils.py 说明）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from framework import utils
from framework.config import Config, config as _default_config

if TYPE_CHECKING:  # 仅类型标注用，运行时不导入 avc
    from avc.image import IImageBuffer
    from avc.input import IInputController, IScreenCapture
    from avc.vision import ITemplateMatcher, ITextRecognizer


def _import_avc():
    """惰性导入 avc 四件套 + MouseButton。失败给清晰的安装/配置提示。"""
    try:
        from avc import Image, Input, Vision
        from avc._core import MouseButton
    except Exception as e:  # ImportError / DLL 加载失败
        raise ImportError(
            "无法导入 avc（C++ SDK）。请先构建 avc，并设环境变量 AVC_HOME 指向含 "
            "avc.dll 的目录（swig/python/avc/__init__.py 据此自动配置 PATH/sys.path）。"
        ) from e
    return Input, Vision, Image, MouseButton


class GameContext:
    """avc 实例（sc/ic/tm/ocr）单一入口 + 基础（拟人化）输入。"""

    def __init__(self, window_title: str = "原神", cfg: Config | None = None):
        Input, Vision, Image, MouseButton = _import_avc()

        self.cfg = cfg or _default_config
        # 拟人化 RNG 与 config 对齐（可复现 / 真随机）
        utils.set_seed(self.cfg.jitter_seed)

        # ── avc 四件套 ──
        self.sc: IScreenCapture = Input.createScreenCapture()
        self.ic: IInputController = Input.createInputController()
        # tm/ocr 在 avc 未启用 opencv/ocr 插件时返回 None（降级）
        self.tm: ITemplateMatcher | None = Vision.createTemplateMatcher()
        self.ocr: ITextRecognizer | None = Vision.createTextRecognizer()
        self._Image = Image
        self._MouseButton = MouseButton

        # ── 配置窗口 + 输入平滑（avc 提供的部分）──
        self.sc.setWindow(window_title)
        self.sc.activateWindow(window_title)
        self.sc.refresh()
        self.ic.setMoveDurationMs(self.cfg.move_duration_ms)
        self.ic.setMoveSteps(self.cfg.move_steps)
        self.ic.setKeyDelayMs(self.cfg.key_delay_ms)

        # ── 启动分辨率检查（CLAUDE §8：必须 1920×1080）──
        self.cfg.check_resolution(self.sc.width(), self.sc.height())

    # ── 截图 ──

    def capture(self) -> IImageBuffer | None:
        """刷新并取最新一帧。返回高层 IImageBuffer（有 to_bytes/crop/save），失败 None。

        注意：底层 ``sc.getBuffer()`` 返回原生借用 buffer，生命周期归 sc；
        下次 refresh 前用完。这里包成高层 IImageBuffer 以获得便利方法。
        """
        self.sc.refresh()
        nb = self.sc.getBuffer()
        if not nb:
            return None
        return self._Image.IImageBuffer(nb)

    def to_screen(self, buf_x: float, buf_y: float) -> tuple[int, int]:
        """截图缓冲坐标 → 屏幕坐标。"""
        sp = self.sc.toScreen(int(buf_x), int(buf_y))
        return sp.x, sp.y

    def save_debug(self, path: str) -> None:
        """保存当前帧到 debug/ 存证。"""
        self.sc.save(path)

    # ── 输入（拟人化由框架层套用；avc 无 setHumanize）──

    def _humanize_on(self) -> bool:
        return self.cfg.humanize

    def click_at(self, bx: float, by: float, button: str | int = "left") -> None:
        """点击截图坐标 (bx, by)。button: 'left'/'right'/'middle' 或 MouseButton。

        拟人化：坐标 ±像素抖动、moveTo 走 avc 动画（setMoveDurationMs）、
        按住时长随机（click_hold_ms 区间）。
        """
        btn = self._MouseButton[button] if isinstance(button, str) else button
        sx, sy = self.to_screen(bx, by)
        if self._humanize_on():
            sx, sy = utils.jitter_coord(sx, sy, self.cfg.click_jitter_px)
        self.ic.moveTo(int(sx), int(sy))
        self.ic.mouseDown(btn)
        if self._humanize_on():
            utils.sleep(utils.rand_in_range(*self.cfg.click_hold_ms) / 1000.0)
        self.ic.mouseUp(btn)

    def click_center(self, rect) -> None:
        """点击矩形中心。rect 需有 .x/.y/.w/.h（avc MatchResult / 自定义 Rect）。"""
        self.click_at(rect.x + rect.w / 2, rect.y + rect.h / 2)

    def press(self, key, hold: float = 0.0) -> None:
        """按键。hold 秒（拟人化时叠加 0.8–1.2× 抖动）。"""
        hold_ms = int(hold * 1000)
        if self._humanize_on() and hold_ms > 0:
            hold_ms = int(utils.jitter(hold_ms, self.cfg.op_jitter))
        self.ic.press(key, max(0, hold_ms))

    def hotkey(self, *keys) -> None:
        """组合键（如 hotkey(KeyCode.ctrl, KeyCode.c)）。"""
        self.ic.hotkey(*keys)

    def type_text(self, text: str) -> None:
        self.ic.typeText(text)

    # ── 运行时控制（委托 Runtime；Runtime 构造时绑定 self.runtime）──

    runtime = None  # Runtime 构造时设置（ctx.runtime = self），见 runtime.py

    def mount(self, name: str, **opts) -> None:
        self.runtime.mount(name, **opts)

    def unmount(self, name: str) -> None:
        self.runtime.unmount(name)

    def suspend_all(self) -> None:
        self.runtime.suspend_all()

    def resume_all(self) -> None:
        self.runtime.resume_all()

    def run(self, name: str, **params):
        """任务组合：在当前 run 内调用子任务（纯 Python 调用，非配置编排，04 §6）。"""
        if self.runtime is None:
            raise RuntimeError("GameContext 未绑定 Runtime")
        return self.runtime._run_inline(name, **params)

    # ── 生命周期 ──

    def release_all_keys(self) -> None:
        """取消/异常出口：尽力释放常用修饰/移动键，避免游戏留按住的键（01 §8.4）。

        avc 无显式 ReleaseAllKey；枚举可能被按住的键逐个 keyUp。阶段三 Runtime
        统一取消时调用。
        """
        from avc._core import KeyCode

        for k in (
            KeyCode.w, KeyCode.a, KeyCode.s, KeyCode.d,
            KeyCode.space, KeyCode.shift, KeyCode.ctrl, KeyCode.alt,
            KeyCode.e, KeyCode.q, KeyCode.f,
        ):
            try:
                self.ic.keyUp(k)
            except Exception:
                pass
