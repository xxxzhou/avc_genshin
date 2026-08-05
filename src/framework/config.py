"""运行配置（docs/design/03 §9）。

单一加载入口：``config.toml``（可选）→ 环境变量覆盖 → 默认值。
``Config`` 对象注入 Runtime / GameContext。敏感项（LLM key 等，后置）走 ``.env`` 不进 git。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

try:  # Python 3.11+ stdlib；3.10 需 ``pip install tomli``
    import tomllib  # type: ignore[attr-defined]
    _TOMLIB_OK = True
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]
    _TOMLIB_OK = False


def _bgi_root_default() -> Path | None:
    v = os.getenv("BGI_ROOT", "").strip()
    return Path(v) if v else None


@dataclass
class Config:
    # ── 窗口 / 分辨率 ──
    window_title: str = "原神"
    resolution: tuple[int, int] = (1920, 1080)  # 必须 1080p；启动检查（CLAUDE §8）

    # ── 拟人化（avc 不提供 setHumanize，框架层实现，见 utils.py）──
    humanize: bool = True
    jitter_seed: int | None = None  # None=每次随机；整数=可复现（debug/回归）
    move_duration_ms: int = 120  # 鼠标移动动画时长（avc setMoveDurationMs）
    move_steps: int = 24  # 鼠标移动插值步数（avc setMoveSteps）
    key_delay_ms: int = 40  # 按键间延迟（avc setKeyDelayMs）
    op_jitter: float = 0.15  # 操作间隔 0.85–1.15×（CLAUDE §8：0.8–1.2× 抖动）
    click_jitter_px: float = 2.0  # 点击坐标 ±像素抖动
    click_hold_ms: tuple[int, int] = (45, 90)  # 点击按住时长随机区间（ms）

    # ── 路径 ──
    resources_dir: Path = field(default_factory=lambda: Path("resources"))
    bgi_root: Path | None = field(default_factory=_bgi_root_default)
    logs_dir: Path = field(default_factory=lambda: Path("logs"))
    debug_dir: Path = field(default_factory=lambda: Path("debug"))
    cache_dir: Path = field(default_factory=lambda: Path("cache"))

    @classmethod
    def load(cls, path: str | os.PathLike = "config.toml") -> Config:
        """从 ``config.toml``（若存在）加载，环境变量覆盖个别字段。"""
        cfg = cls()
        p = Path(path)
        if p.is_file():
            cfg._merge_toml(p)
        cfg._apply_env()
        return cfg

    def _merge_toml(self, path: Path) -> None:
        if not _TOMLIB_OK:
            return  # 无 tomllib：静默用默认值（环境变量仍生效）
        with path.open("rb") as f:
            data = tomllib.load(f)
        for fld in fields(self):
            if fld.name in data:
                val = data[fld.name]
                if fld.name == "resolution" and isinstance(val, list):
                    val = tuple(val)
                if fld.name in {"resources_dir", "logs_dir", "debug_dir", "cache_dir", "bgi_root"}:
                    val = Path(val) if val else None
                setattr(self, fld.name, val)

    def _apply_env(self) -> None:
        """少量关键字段支持环境变量覆盖（CI / 临时调试用）。"""
        if v := os.getenv("AVC_WINDOW"):
            self.window_title = v
        if v := os.getenv("BGI_ROOT"):
            self.bgi_root = Path(v)
        if v := os.getenv("AVC_JITTER_SEED"):
            try:
                self.jitter_seed = int(v)
            except ValueError:
                pass

    # ── 校验 ──

    def check_resolution(self, width: int, height: int) -> None:
        """启动分辨率检查：游戏画面须 ≥ 1920×1080（CLAUDE §8）。

        容许 buffer 含窗口边框（如 1926×1156），只要裁剪后能得到 1080p 游戏画面。
        不再要求精确匹配，因为 DPI 缩放下 sc.width/height 报逻辑尺寸，buffer 实际像素更大。
        """
        want_w, want_h = self.resolution
        if width < want_w or height < want_h:
            from framework.errors import AvcsError

            raise AvcsError(
                f"分辨率需 ≥ {want_w}×{want_h}，实际 {width}×{height}。"
                f"请将原神设为 1920×1080 窗口模式（CLAUDE §8）。"
            )


# 默认实例（懒求值的模块级单例）：首次访问由 Runtime 显式 ``Config.load()`` 替换。
config = Config.load()
