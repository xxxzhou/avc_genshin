"""资源定位器 ``res``（docs/design/03 §5）。

统一访问 templates / paths / models / ocr / map，**禁止硬编码相对路径**。
``resources/`` 优先；缺失且配了 ``BGI_ROOT`` → 回退到本地 BetterGI 仓库（免复制大文件）。
"""

from __future__ import annotations

import os
from pathlib import Path


def _bgi_root_default() -> Path | None:
    v = os.getenv("BGI_ROOT", "").strip()
    return Path(v) if v else None


class Resources:
    """资源定位器。先查 ``root/<rel>``，不存在则回退 BGI_ROOT 目录树。"""

    def __init__(self, root: Path | str = "resources", bgi_root: Path | str | None = None):
        self.root = Path(root)
        self.bgi_root = Path(bgi_root) if bgi_root else _bgi_root_default()
        self._bgi_index: dict[str, Path] | None = None  # 懒构建：basename → 首个命中

    # ── 主入口 ──

    def path(self, rel: str | Path) -> Path:
        """解析相对路径。本地有就用本地；否则 BGI_ROOT 回退。"""
        rel = Path(rel)
        local = self.root / rel
        if local.exists():
            return local
        fallback = self._bgi_fallback(rel.name)
        if fallback is not None:
            return fallback
        # 都没有：返回本地路径（调用方拿到后报错更直观）
        return local

    def exists(self, rel: str | Path) -> bool:
        p = self.path(rel)
        return p.exists()

    # ── 语义快捷 ──

    def template(self, name: str | Path) -> Path:
        return self.path(Path("templates") / name)

    def model(self, name: str | Path) -> Path:
        return self.path(Path("models") / name)

    def path_json(self, name: str | Path) -> Path:
        return self.path(Path("paths") / name)

    def map(self, name: str | Path) -> Path:
        return self.path(Path("map") / name)

    # ── 模板子目录快捷（Phase A 新增）──

    def template_ui(self, name: str | Path) -> Path:
        return self.template(Path("ui") / name)

    def template_dialog(self, name: str | Path) -> Path:
        return self.template(Path("dialog") / name)

    def template_pick(self, name: str | Path) -> Path:
        return self.template(Path("pick") / name)

    def template_eat(self, name: str | Path) -> Path:
        return self.template(Path("eat") / name)

    def template_chest(self, name: str | Path) -> Path:
        return self.template(Path("chest") / name)

    def template_teleport(self, name: str | Path) -> Path:
        return self.template(Path("teleport") / name)

    def template_loading(self, name: str | Path) -> Path:
        return self.template(Path("loading") / name)

    # ── BGI 回退 ──

    def _bgi_fallback(self, basename: str) -> Path | None:
        """在 BGI_ROOT 目录树按文件名查找（缓存）。模型/模板/路径都走这条。"""
        if self.bgi_root is None or not self.bgi_root.is_dir():
            return None
        if self._bgi_index is None:
            self._bgi_index = self._index_bgi()
        return self._bgi_index.get(basename)

    def _index_bgi(self) -> dict[str, Path]:
        """一次性扫描 BGI_ROOT 的资源目录，建 basename → Path 索引。

        只扫资源相关子目录（Assets/Model、Assets、User 目录等），避免遍历整个仓库。
        """
        index: dict[str, Path] = {}
        if self.bgi_root is None:
            return index
        # BetterGI 模型在 Assets/Model/...；模板在 GameTask/*/Assets/1920x1080/。
        candidates = [
            self.bgi_root / "Assets" / "Model",
            self.bgi_root / "Assets",
        ]
        # 扫描 GameTask/*/Assets/1920x1080/ 下的模板图 + Pathing/ 下的路径 JSON
        gt = self.bgi_root / "GameTask"
        if gt.is_dir():
            for task_dir in gt.iterdir():
                assets = task_dir / "Assets" / "1920x1080"
                if assets.is_dir():
                    candidates.append(assets)
                # Phase B: 扫描路径 JSON 文件
                pathing = task_dir / "Assets" / "Pathing"
                if pathing.is_dir():
                    candidates.append(pathing)
        for base in candidates:
            if not base.is_dir():
                continue
            for p in base.rglob("*"):
                if p.is_file() and p.name not in index:
                    index[p.name] = p
        return index


# 模块级单例（从环境初始化）。Runtime 启动时可用 ``Resources`` + config 重建。
res = Resources()
