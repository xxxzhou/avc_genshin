"""沙箱 + 代码校验（docs/design/01 §5、04 §9、06 §4）。

即时任务（AI 现场生成，``cache/tasks/<hash>.py``）**必须**经沙箱 exec：先过 CodeValidator
（AST 白名单，禁 import/open/exec/eval/dunder），再用受限 globals exec。持久任务
（``src/tasks/``，人/AI 审定过、进 git）可信，正常 importlib 加载，不经沙箱。

沙箱是"AI 生成代码可被信任"的一环（与 02 §5 Policy 互补）：Policy 管"做什么"（不花钱），
沙箱管"能做什么"（不能读文件/逃逸）。Python 沙箱非绝对安全（AST + 受限 builtins 是
best-effort），故**仅用于即时任务**；提升为持久任务时转可信加载。
"""

from __future__ import annotations

import ast
import builtins as _bi
from typing import Any

# 禁止的内置/函数名（即时任务不得调用）
_FORBIDDEN_NAMES = frozenset(
    {
        "open", "exec", "eval", "compile", "__import__", "globals", "vars",
        "locals", "getattr", "setattr", "delattr", "breakpoint", "exit", "quit",
        "input", "memoryview",
    }
)

# 允许的内置（name → 真实 builtin 的映射；True/False/None 是关键字但也收录以防万一）
_SAFE_BUILTINS: dict[str, Any] = {
    n: getattr(_bi, n)
    for n in (
        "len", "range", "int", "float", "str", "bool", "list", "dict", "tuple",
        "set", "min", "max", "abs", "round", "enumerate", "zip", "sorted", "reversed",
        "any", "all", "sum", "map", "filter", "isinstance", "print",
    )
    if hasattr(_bi, n)
}
_SAFE_BUILTINS.update({"True": True, "False": False, "None": None})


class CodeValidator:
    """AST 白名单校验。``validate(code) -> (ok, err)``。"""

    @staticmethod
    def validate(code: str) -> tuple[bool, str]:
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return False, f"语法错误：{e.msg}（行 {e.lineno}）"
        for node in ast.walk(tree):
            # 禁 import
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                return False, "禁止 import：即时任务用注入的 ctx/g/sleep/KeyCode，不导入"
            # 禁危险函数名调用
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Name) and fn.id in _FORBIDDEN_NAMES:
                    return False, f"禁止调用 {fn.id}()"
            if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES and isinstance(
                node.ctx, ast.Load
            ):
                return False, f"禁止访问 {node.id}"
            # 禁 dunder 属性（防 __class__/__globals__ 逃逸）
            if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
                return False, f"禁止访问下划线属性 .{node.attr}"
        return True, ""


def make_safe_globals(extras: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造沙箱 globals：受限 builtins + 注入的工具（ctx/g/sleep/wait_until/KeyCode）。"""
    g: dict[str, Any] = {"__builtins__": dict(_SAFE_BUILTINS)}
    if extras:
        g.update(extras)
    return g


def exec_sandboxed(code: str, extras: dict[str, Any] | None = None) -> dict[str, Any]:
    """校验 + 受限 exec。返回模块 globals（含定义的 ``main``）。违例抛 ValueError。"""
    ok, err = CodeValidator.validate(code)
    if not ok:
        raise ValueError(f"沙箱校验失败：{err}")
    env = make_safe_globals(extras)
    exec(compile(code, "<ephemeral>", "exec"), env)  # noqa: S102 — 已 AST 白名单
    return env
