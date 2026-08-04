#!/usr/bin/env python3
"""avc_genshin 薄入口壳。

真代码在 ``src/``（``pip install -e .`` 后可 ``import framework``）。
本文件仅转发到 ``framework.__main__:main``，也支持 ``python -m framework``。

用法（规划，见 IMPLEMENTATION §9）：
    python main.py                      # 交互式
    python main.py --intent "完成日常"   # 单次意图
    python main.py --task daily_quest   # 直接跑已注册任务
    python main.py --proto capture      # 阶段一：基础链路原型
"""

import sys

from framework.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
