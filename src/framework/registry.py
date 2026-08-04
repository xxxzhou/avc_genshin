"""TaskRegistry —— 任务发现/注册/热加载（docs/design/04 §5、00 §5）。

两个扫描根：
  - ``src/tasks/``：持久任务（进 git，可信，importlib 加载）。
  - ``cache/tasks/``：即时任务（AI 现场生成，gitignore，沙箱 exec）。

命名冲突：持久优先（同名即时被忽略并 warn）。降级形态（无 ``@task`` 的 ``def main``）
从文件名/docstring 推断元数据（04 §9）。热加载失败保留旧版本（04 §5.3）。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from framework.errors import AvcsError
from framework.task import TaskDescriptor


class TaskNotFound(AvcsError):
    pass


class TaskRegistry:
    """name → TaskDescriptor。"""

    def __init__(self):
        self._tasks: dict[str, TaskDescriptor] = {}
        self._warnings: list[str] = []

    # ── 发现 ──

    def discover(self, roots=("src/tasks", "cache/tasks")) -> None:
        """扫描两个根并注册。加载失败的单个任务不阻断其余（04 §5.3）。"""
        for root in roots:
            rp = Path(root)
            if not rp.is_dir():
                continue
            kind = "ephemeral" if "cache" in rp.parts else "persistent"
            for py in sorted(rp.rglob("*.py")):
                if py.name.startswith("_"):
                    continue
                try:
                    self._load_file(py, kind)
                except Exception as e:
                    self._warnings.append(f"加载失败 {py}: {e!r}")

    def _load_file(self, path: Path, kind: str) -> TaskDescriptor | None:
        if kind == "persistent":
            main, meta_desc = self._load_persistent(path)
        else:
            main, meta_desc = self._load_ephemeral(path)
        if main is None:
            return None
        # 取元数据：@task 附的 task_descriptor，否则降级推断
        desc: TaskDescriptor
        if meta_desc is not None:
            desc = meta_desc
            desc.main = main
        else:
            desc = TaskDescriptor(
                name=path.stem,
                desc=(main.__doc__ or "").strip().split("\n")[0] or f"即时任务 {path.stem}",
                main=main,
            )
        desc.source = path
        desc.kind = kind
        desc.loaded_at = time.time()
        self.register(desc)
        return desc

    def _load_persistent(self, path: Path):
        import sys
        from types import ModuleType

        mod_name = f"_avcgs_task_{path.stem}_{abs(hash(str(path)))}"
        sys.modules.pop(mod_name, None)
        # 直接 compile 源码 + exec 到新模块：绕过 .pyc 缓存（Windows 上 mtime 分辨率粗，
        # 热加载时 importlib 会误判 pyc 有效而读到旧字节码）。每次都读当前源。
        src = path.read_text(encoding="utf-8")
        code = compile(src, str(path), "exec")
        mod = ModuleType(mod_name)
        mod.__file__ = str(path)
        exec(code, mod.__dict__)  # 运行 `from framework import task` + @task  # noqa: S102
        main = getattr(mod, "main", None)
        meta = getattr(main, "task_descriptor", None) if main is not None else None
        return main, meta

    def _load_ephemeral(self, path: Path):
        from framework.sandbox import exec_sandboxed

        code = path.read_text(encoding="utf-8")
        env = exec_sandboxed(code)  # AST 白名单 + 受限 globals
        main = env.get("main")
        return main, None  # 即时任务降级形态：无 @task，由 _load_file 推断

    # ── 注册 / 查询 ──

    def register(self, desc: TaskDescriptor) -> None:
        existing = self._tasks.get(desc.name)
        if existing is not None:
            # 持久优先：已有持久、新来即时 → 忽略
            if existing.kind == "persistent" and desc.kind == "ephemeral":
                self._warnings.append(f"持久任务 {desc.name} 优先，忽略同名即时任务")
                return
            self._warnings.append(f"任务 {desc.name} 被覆盖（{existing.kind}→{desc.kind}）")
        self._tasks[desc.name] = desc

    def get(self, name: str) -> TaskDescriptor:
        desc = self._tasks.get(name)
        if desc is None:
            raise TaskNotFound(f"未注册的任务：{name}")
        return desc

    def has(self, name: str) -> bool:
        return name in self._tasks

    def list(self, tags: set[str] | None = None) -> list[dict]:
        """精简视图（供 AI 规划器枚举，04 §4）。可按 tags 筛。"""
        out = [d.view() for d in self._tasks.values()]
        if tags:
            out = [v for v in out if tags & set(v["tags"])]
        return sorted(out, key=lambda v: v["name"])

    def names(self) -> list[str]:
        return sorted(self._tasks)

    # ── 热加载 / 卸载 ──

    def reload(self, name: str) -> TaskDescriptor:
        desc = self.get(name)
        if desc.source is None:
            raise TaskNotFound(f"{name} 无源文件（内存任务，不可热加载）")
        kind = desc.kind
        self._load_file(desc.source, kind)  # 失败抛异常 → 调用方保留旧版（04 §5.3）
        return self.get(name)

    def unregister(self, name: str) -> None:
        self._tasks.pop(name, None)

    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)
