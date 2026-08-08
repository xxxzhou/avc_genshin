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


class _RECT(ctypes.Structure):
    """Win32 RECT（GetWindowRect/GetClientRect 用，本进程系统 DPI=96 时返回物理像素）。"""

    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


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
        self.window_title = window_title  # 前台检查/激活用（ensure_foreground）
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

    def _window_geometry(
        self,
    ) -> tuple[int, int, int, int, int, int] | None:
        """当前窗口物理几何：(win_l, win_t, side_border, top_border, cli_w, cli_h)。

        ⚠ DPI 说明（2026-08-08 实机探针定位）：本进程 DPI-unaware 且系统 DPI 常为 96，
        GetWindowRect/GetClientRect 因此返回**物理像素**；而 avc 的 ``sc.toScreen`` 却用
        窗口 DPI（如 240）把坐标逻辑化（span 缩小 1/dpi），与 ``ic.moveTo``（物理像素）
        不同空间 —— 这就是鼠标偏左上的根因，to_screen 必须绕开 sc.toScreen。

        GetClientRect 在个别环境可能被 DPI 虚拟化（返回逻辑值）：当 ``cli×dpi`` 比原始值
        更接近窗口尺寸时判定为逻辑值并还原成物理。
        """
        if sys.platform != "win32":
            return None
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, self.window_title)
            if not hwnd:
                return None
            wr = _RECT()
            cr = _RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(wr))
            user32.GetClientRect(hwnd, ctypes.byref(cr))
            win_w = wr.right - wr.left
            win_h = wr.bottom - wr.top
            cli_w, cli_h = cr.right, cr.bottom
            if cli_w <= 0 or cli_h <= 0 or win_w <= 0 or win_h <= 0:
                return None
            # 判定 GetClientRect 是否被 DPI 虚拟化（逻辑值还原后应接近窗口尺寸）
            dpi = self._dpi_scale or 1.0
            if abs(cli_w * dpi - win_w) < abs(cli_w - win_w):
                cli_w, cli_h = int(cli_w * dpi), int(cli_h * dpi)
            if cli_w >= win_w or cli_h >= win_h:
                return None
            side = (win_w - cli_w) // 2
            top = win_h - cli_h - side
            return wr.left, wr.top, side, top, cli_w, cli_h
        except Exception:
            return None

    def to_screen(self, buf_x: float, buf_y: float) -> tuple[int, int]:
        """1080p buffer 坐标 → 物理屏幕坐标（供 ic.moveTo 使用）。

        每次现算窗口几何（免疫窗口缩放），1080p 线性映射到客户区：
            屏_x = win_left + 侧边框 + buf_x × 客户宽/1920
            屏_y = win_top  + 顶边框 + buf_y × 客户高/1080
        返回物理像素，与 ``ic.moveTo``/``getCursorPos`` 同一空间。
        """
        g = self._window_geometry()
        if g is None:
            # fallback: 无窗口时 buffer 坐标即屏幕坐标（测试/mock 场景）
            return int(buf_x), int(buf_y)
        win_l, win_t, side, top, cli_w, cli_h = g
        sx = win_l + side + buf_x * cli_w / 1920.0
        sy = win_t + top + buf_y * cli_h / 1080.0
        return int(sx), int(sy)

    def save_debug(self, path: str) -> None:
        """保存当前帧到 debug/ 存证。"""
        self.sc.save(path)

    # ── 输入（拟人化由框架层套用；avc 无 setHumanize）──

    def ensure_foreground(self, wait_s: float = 0.2) -> bool:
        """确保游戏窗口在前台（程序自保证，不依赖手动切窗）。

        每次鼠标/键盘操作前调用，保证操作发到游戏而非其它窗口。三步：
        1. 零开销检查：GetForegroundWindow 标题已是游戏 → 直接返回 True。
        2. 最小化则先还原（ShowWindow SW_RESTORE）——最小化窗口激活无效。
        3. 激活：AVC activateWindow + ALT 键技巧绕过 Windows 前台锁
           （后台进程直接 SetForegroundWindow 常被 Windows 拒绝）。

        Returns:
            True=已在前台（无需激活）；False=刚激活过。非 Windows / 检查失败返回 True（不阻断）。
        """
        if sys.platform != "win32":
            return True
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, self.window_title)
            if hwnd:
                # 1) 最小化 → 先还原（SW_RESTORE=9）。最小化窗口无渲染帧，
                #    SourcePlayer 截不到图，且激活无效——必须最先处理。
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, 9)
                    utils.sleep(0.2)
                # 2) 已在后台前 → 零开销返回
                fg = user32.GetForegroundWindow()
                if fg and fg == hwnd:
                    return True
                if fg:
                    n = user32.GetWindowTextLengthW(fg)
                    if n > 0:
                        buf = ctypes.create_unicode_buffer(n + 1)
                        user32.GetWindowTextW(fg, buf, n + 1)
                        if self.window_title.lower() in buf.value.lower():
                            return True
                # 3) 激活：avc 激活 + ALT 技巧绕过前台锁
                try:
                    self.sc.activateWindow(self.window_title)
                except Exception:
                    pass
                user32.keybd_event(0x12, 0, 0, 0)  # ALT down
                user32.SetForegroundWindow(hwnd)
                user32.keybd_event(0x12, 0, 2, 0)  # ALT up
            else:
                try:
                    self.sc.activateWindow(self.window_title)
                except Exception:
                    pass
            if wait_s > 0:
                utils.sleep(wait_s)  # 前台切换后稍候，避免操作落在旧窗口
            return False
        except Exception:
            return True

    def _humanize_on(self) -> bool:
        return self.cfg.humanize

    def click_at(self, bx: float, by: float, button: str | int = "left") -> None:
        """点击截图坐标 (bx, by)。button: 'left'/'right'/'middle' 或 MouseButton。

        拟人化：坐标 ±像素抖动、moveTo 走 avc 动画（setMoveDurationMs）、
        按住时长随机（click_hold_ms 区间）。
        """
        self.ensure_foreground()
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
        self.ensure_foreground()
        hold_ms = int(hold * 1000)
        if self._humanize_on() and hold_ms > 0:
            hold_ms = int(utils.jitter(hold_ms, self.cfg.op_jitter))
        self.ic.press(key, max(0, hold_ms))

    def hotkey(self, *keys) -> None:
        """组合键（如 hotkey(KeyCode.ctrl, KeyCode.c)）。"""
        self.ensure_foreground()
        self.ic.hotkey(*keys)

    def type_text(self, text: str) -> None:
        self.ensure_foreground()
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
        """用 Win32 API 计算窗口边框偏移和 DPI 缩放比。

        avc 截图 buffer 包含整个窗口（含标题栏+边框），但下游代码需要纯游戏画面。
        这里算出边框偏移，供 capture() 裁剪（IScreenCapture 回退路径）使用。

        本进程 DPI-unaware 且系统 DPI 常为 96 → GetWindowRect/GetClientRect 返回物理像素，
        无需再乘 DPI（旧实现把已物理的 GetClientRect 又乘 2.5 得到 3200×1800，导致边框陈旧
        且误判——2026-08-08 实机定位）。逻辑/物理判定交给 ``_window_geometry`` 统一处理。

        注意：窗口缩放后此值可能陈旧；``to_screen`` 已改为每次现算几何（见 _window_geometry），
        不受影响。非 Windows 或找不到窗口时保持默认值 0。
        """
        if sys.platform != "win32":
            return
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, window_title)
            if not hwnd:
                return
            dpi = user32.GetDpiForWindow(hwnd)
            if dpi > 0:
                self._dpi_scale = dpi / 96.0
            g = self._window_geometry()
            if g is None:
                return
            _, _, side, top, _, _ = g
            self._border_left = side
            self._border_top = top
        except Exception:
            pass  # Win32 API 失败时保持默认值 0
