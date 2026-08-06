"""GameContext —— avc 实例管理（docs/design/01、IMPLEMENTATION §4.1）。

任务侧的 ``ctx``：持有 avc 的 sc/ic/tm/ocr 四件套，提供截图与基础（拟人化）输入。
高层语义在 ``high_level_api.py``（g.*），任务组合在 ``ctx.run``（阶段四）。

截图架构：
- 用 ``ISourcePlayer`` + ``ISurfaceRender.screenShot`` 做高频截图（绑定窗口，持续打开，
  每帧只做 GPU→CPU 回读，不重建链路）。
- 用 ``IScreenCapture`` 做初始窗口枚举/定位/激活（一次性）。
- ``capture()`` 返回纯 1080p 游戏画面 buffer（裁掉窗口边框），下游零改动。
- ``to_screen()`` 把 1080p buffer 坐标加回边框偏移后转屏幕坐标。

DPI / 窗口边框归一化：
- avc 截图拿到的是**整个窗口含边框**（如 1926×1156），``sc.width/height`` 报 DPI
  缩放后的逻辑尺寸（如 781×467），两者都不等于游戏画面 1920×1080。
- ``capture()`` 自动裁掉窗口边框，返回纯 1080p 游戏画面 buffer，下游零改动。
- 初始化时用 Win32 API 算出边框偏移和 DPI 缩放比。
"""

from __future__ import annotations

import ctypes
import sys
from typing import TYPE_CHECKING

from framework import utils
from framework.config import Config, config as _default_config

if TYPE_CHECKING:  # 仅类型标注用，运行时不导入 avc
    from avc.image import IImageBuffer
    from avc.input import IInputController, IScreenCapture
    from avc.player import ISourcePlayer
    from avc.vision import ITemplateMatcher, ITextRecognizer


def _import_avc():
    """惰性导入 avc + MouseButton。失败给清晰的安装/配置提示。"""
    try:
        from avc import Image, Input, Vision
        from avc._core import MouseButton, YuvType
    except Exception as e:  # ImportError / DLL 加载失败
        raise ImportError(
            "无法导入 avc（C++ SDK）。请先构建 avc，并设环境变量 AVC_HOME 指向含 "
            "avc.dll 的目录（swig/python/avc/__init__.py 据此自动配置 PATH/sys.path）。"
        ) from e
    return Input, Vision, Image, MouseButton, YuvType


class GameContext:
    """avc 实例（sc/ic/tm/ocr）单一入口 + 基础（拟人化）输入。"""

    def __init__(self, window_title: str = "原神", cfg: Config | None = None):
        Input, Vision, Image, MouseButton, YuvType = _import_avc()

        self.cfg = cfg or _default_config
        # 拟人化 RNG 与 config 对齐（可复现 / 真随机）
        utils.set_seed(self.cfg.jitter_seed)

        # ── avc 四件套 ──
        self.ic: IInputController = Input.createInputController()
        # tm/ocr 在 avc 未启用 opencv/ocr 插件时返回 None（降级）
        self.tm: ITemplateMatcher | None = Vision.createTemplateMatcher()
        self.ocr: ITextRecognizer | None = Vision.createTextRecognizer()
        self._Image = Image
        self._MouseButton = MouseButton
        self._YuvType = YuvType

        # ── 窗口定位（一次性 IScreenCapture）──
        self.sc: IScreenCapture = Input.createScreenCapture()
        self.sc.setWindow(window_title)
        self.sc.refresh()

        # ── 高频截图（SourcePlayer 持续打开，screenShot 直接取帧）──
        self._player: ISourcePlayer | None = None
        self._shot_buf: IImageBuffer | None = None  # 预分配截图 buffer
        self._init_source_player(window_title)

        # ── 输入平滑（avc 提供的部分）──
        self.ic.setMoveDurationMs(self.cfg.move_duration_ms)
        self.ic.setMoveSteps(self.cfg.move_steps)
        self.ic.setKeyDelayMs(self.cfg.key_delay_ms)

        # ── DPI / 窗口边框归一化 ──
        self._border_left: int = 0   # 窗口左边框像素（buffer 坐标系）
        self._border_top: int = 0    # 窗口标题栏+上边框像素（buffer 坐标系）
        self._dpi_scale: float = 1.0  # Windows DPI 缩放比（如 2.5 = 250%）
        self._calc_window_offsets(window_title)

        # ── 启动分辨率检查（CLAUDE §8：游戏画面须 ≥ 1920×1080）──
        # 仅 warn 不报错，允许 verify 在非原神窗口上跑诊断
        frame = self.capture()
        if frame is not None:
            want_w, want_h = self.cfg.resolution
            if frame.width < want_w or frame.height < want_h:
                print(
                    f"[warn] 分辨率 {frame.width}×{frame.height} < {want_w}×{want_h}，"
                    f"部分检测可能不可靠。原神须 1920×1080 窗口模式。"
                )
        elif self.sc.width() > 0 and self.sc.height() > 0:
            want_w, want_h = self.cfg.resolution
            if self.sc.width() < want_w or self.sc.height() < want_h:
                print(
                    f"[warn] 分辨率 {self.sc.width()}×{self.sc.height()} < {want_w}×{want_h}，"
                    f"部分检测可能不可靠。原神须 1920×1080 窗口模式。"
                )

    # ── 截图 ──

    def _init_source_player(self, window_title: str) -> None:
        """用 SourcePlayer 绑定窗口，持续打开，用于高频截图。

        流程：枚举窗口 → 找到目标窗口的 VideoSource → SourcePlayer 打开 → 保持活跃。
        之后 capture() 只做 render.screenShot(buf)，不重建链路。
        失败时回退到 IScreenCapture（不影响运行，只是截图慢）。
        """
        from avc.player import ISourcePlayer
        from avc.source import getVideoManager

        mgr = getVideoManager()
        if not mgr:
            print(f"[warn] SourcePlayer: 无 VideoManager，回退 IScreenCapture")
            return

        # 找到标题含 window_title 的窗口设备（子串匹配，大小写不敏感）
        source = None
        for i in range(mgr.getDeviceCount()):
            dev = mgr.getDevice(i)
            if dev is None:
                continue
            name = dev.getDeviceName()
            if name and window_title.lower() in name.lower():
                source = dev
                break

        if source is None:
            print(f"[warn] SourcePlayer: 未找到窗口 '{window_title}'，回退 IScreenCapture")
            return

        # 创建 SourcePlayer，绑定该窗口源
        player = ISourcePlayer()
        player.setVideoSource(source.native)
        sr = player.getSurfaceRender()
        if sr:
            sr.setOffSurface(self._YuvType.other)
            # GPU 侧缩放到目标分辨率（如 1920×1080），截图直接拿到归一化画面
            want_w, want_h = self.cfg.resolution
            sr.enableSizeScale(True)
            sr.enableSizeChange(want_w, want_h)

        if not player.open():
            print(f"[warn] SourcePlayer: 打开失败，回退 IScreenCapture")
            return

        # 等首帧就绪
        from avc._core import PlayerState
        for _ in range(30):
            if player.state == PlayerState.playing:
                break
            import time
            time.sleep(0.05)

        if player.state != PlayerState.playing:
            print(f"[warn] SourcePlayer: 首帧超时 (state={player.state})，回退 IScreenCapture")
            try:
                player.close()
            except Exception:
                pass
            return

        # 预分配截图 buffer
        shot_buf = self._Image.IImageBuffer()

        self._player = player
        self._shot_buf = shot_buf

    def capture(self) -> IImageBuffer | None:
        """取最新一帧，返回纯 1080p 游戏画面 buffer。

        优先用 SourcePlayer（高频，screenShot 直接取帧，GPU 侧已缩放到目标分辨率）；
        回退到 IScreenCapture（首次/Player 未就绪时，需裁边框+缩放）。
        """
        buf = None
        from_player = False
        # 优先 SourcePlayer（已 enableSizeChange，直接输出目标分辨率，无需裁剪）
        if self._player is not None and self._shot_buf is not None:
            sr = self._player.getSurfaceRender()
            if sr and sr.screenShot(self._shot_buf):
                buf = self._shot_buf
                from_player = True

        # 回退 IScreenCapture
        if buf is None:
            self.sc.refresh()
            nb = self.sc.getBuffer()
            if nb:
                buf = self._Image.IImageBuffer(nb)

        if buf is None:
            return None

        # SourcePlayer 已归一化，直接返回
        if from_player:
            return buf

        # IScreenCapture 回退：裁掉窗口边框，缩放到目标分辨率
        if self._border_left > 0 or self._border_top > 0:
            want_w, want_h = self.cfg.resolution
            try:
                # 先裁出纯游戏画面（实际像素尺寸）
                cli_w = buf.width - 2 * self._border_left
                cli_h = buf.height - self._border_top - self._border_left
                cropped = self._Image.crop(
                    buf, self._border_left, self._border_top, cli_w, cli_h
                )
                if cropped is not None:
                    # 若裁出尺寸 ≠ 目标分辨率，缩放
                    if cropped.width != want_w or cropped.height != want_h:
                        resized = self._Image.resize(cropped, want_w, want_h)
                        if resized is not None:
                            return resized
                    return cropped
            except Exception:
                pass  # crop/resize 失败回退原始 buffer
        return buf

    def to_screen(self, buf_x: float, buf_y: float) -> tuple[int, int]:
        """1080p buffer 坐标 → 屏幕坐标（加回边框偏移后经 sc.toScreen 转换）。"""
        # buffer 坐标是裁剪后的 1080p 坐标，需加回边框偏移才能对应原始 buffer
        wx = int(buf_x) + self._border_left
        wy = int(buf_y) + self._border_top
        sp = self.sc.toScreen(wx, wy)
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

    def close(self) -> None:
        """关闭 SourcePlayer，释放截图资源。"""
        if self._player is not None:
            try:
                self._player.close()
            except Exception:
                pass
            self._player = None
        self._shot_buf = None

    # ── DPI / 窗口边框计算 ──

    def _calc_window_offsets(self, window_title: str) -> None:
        """用 Win32 API + avc buffer 实际像素 计算窗口边框偏移和 DPI 缩放比。

        avc 截图 buffer 包含整个窗口（含标题栏+边框），但下游代码需要纯游戏画面。
        这里算出边框在 buffer 中的像素偏移，供 capture() 裁剪和 to_screen() 坐标转换。

        计算逻辑：
        1. ib.width/height → buffer 实际像素尺寸（含边框，不受 DPI 影响）
        2. GetClientRect → 客户区逻辑尺寸（DPI 缩放后）
        3. GetDpiForWindow → DPI 缩放比
        4. 客户区真实像素 = ClientRect × DPI/96
        5. border_left = (buffer_width - client_real_width) // 2
        6. border_top = buffer_height - client_real_height - border_left
           （假设左右边框等宽，底边框=左右边框）

        ⚠ 不能用 GetWindowRect 算：DPI 缩放下它返回逻辑像素，不是真实像素。
        非 Windows 平台或找不到窗口时保持默认值 0（无边框偏移）。
        """
        if sys.platform != "win32":
            return
        try:
            user32 = ctypes.windll.user32

            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            hwnd = user32.FindWindowW(None, window_title)
            if not hwnd:
                return

            # DPI 缩放比
            dpi = user32.GetDpiForWindow(hwnd)
            if dpi > 0:
                self._dpi_scale = dpi / 96.0

            # buffer 实际像素尺寸（含边框）
            nb = self.sc.getBuffer()
            if not nb:
                return
            ib = self._Image.IImageBuffer(nb)
            buf_w, buf_h = ib.width, ib.height

            # 客户区逻辑尺寸 → 真实像素
            cli_rect = RECT()
            user32.GetClientRect(hwnd, ctypes.byref(cli_rect))
            cli_real_w = int(cli_rect.right * self._dpi_scale)
            cli_real_h = int(cli_rect.bottom * self._dpi_scale)

            # 边框偏移（buffer 坐标系 = 真实像素）
            # 左右边框等宽，底边框=左右边框
            border_h = (buf_w - cli_real_w) // 2
            border_v_top = buf_h - cli_real_h - border_h

            if border_h >= 0 and border_v_top >= 0:
                self._border_left = border_h
                self._border_top = border_v_top
        except Exception:
            pass  # Win32 API 失败时保持默认值 0
