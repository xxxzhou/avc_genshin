"""命令行入口（``python -m framework`` 与 ``main.py`` 共用）。

阶段一：仅 ``--proto``（基础链路原型）可用。
``--intent``（阶段五 AI 协作）/ ``--task``（阶段四任务体系）暂未实现。
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="avc-genshin",
        description="以 AI 动态添加任务为核心的原神自动化框架。",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--intent", metavar="TEXT", help="自然语言意图（阶段五）")
    g.add_argument("--task", metavar="NAME", help="直接运行已注册任务（阶段四）")
    g.add_argument(
        "--proto",
        choices=["capture", "vision", "detect"],
        help="阶段一基础链路原型：capture=截图存盘 / vision=模板匹配 / detect=YOLO 检测",
    )
    p.add_argument("--window", default="原神", help="目标窗口标题子串（默认：原神）")
    p.add_argument("--template", help="[proto=vision] 模板图路径（经 res.template 解析）")
    p.add_argument("--model", default="bgi_world.onnx", help="[proto=detect] ONNX 模型名")
    return p


def _run_proto(args: argparse.Namespace) -> int:
    from framework import _proto  # 懒导入：避免无 avc 时 import framework 失败

    if args.proto == "capture":
        return _proto.proto_capture(args.window)
    if args.proto == "vision":
        if not args.template:
            print("proto=vision 需要 --template", file=sys.stderr)
            return 2
        return _proto.proto_vision(args.window, args.template)
    if args.proto == "detect":
        return _proto.proto_detect(args.window, args.model)
    return 2


def _run_task(args: argparse.Namespace) -> int:
    from framework.runtime import Runtime

    rt = Runtime(window=args.window)
    try:
        result = rt.run_task(args.task)
        print(f"[task] {args.task} → {result}")
        return 0
    except Exception as e:
        print(f"[task] {args.task} 失败：{e!r}", file=sys.stderr)
        return 1
    finally:
        rt.shutdown()


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.proto:
        return _run_proto(args)
    if args.intent:
        raise SystemExit("run_intent 未实现（阶段五：AI 协作层）")
    if args.task:
        return _run_task(args)

    _build_parser().print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
