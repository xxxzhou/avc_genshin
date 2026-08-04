# tasks/ —— L3 持久任务插件

这里是 **AI 动态添加任务**的落点：一个 `.py` 文件即一个任务插件。

## 约定（契约见 `docs/design/04-任务契约与注册.md`）

```python
# src/tasks/daily_quest.py
from framework import task

@task(
    name="daily_quest",
    desc="完成每日委托并领取奖励",
    daemons=["auto_skip"],
    params={"count": {"type": "int", "default": 4, "desc": "委托数量"}},
)
def main(ctx, g, count=4):
    g.teleport_to("蒙德城")
    g.talk("领取每日委托奖励")
    return {"claimed": True}
```

## 边界（docs/design/03 §3）

- **本目录不是 import 包**（无 `__init__.py`）。Runtime 用 `importlib` 按**文件**加载，
  不 `import tasks.xxx`。启动时扫描本目录 + `cache/tasks/`（即时任务）。
- **AI 写入域**：AI 只往本目录（持久）或 `cache/tasks/`（即时）写；
  **不改** `framework/` / `abilities/`（框架本体）。这条物理边界让"AI 添加任务"安全。
- **打包**：本目录随仓库 git（审定过的持久任务 + 内置示例），但不进 wheel
  （`pyproject.toml` 只打包 `framework`/`abilities`）。
- **提升**：即时任务成熟后从 `cache/tasks/` 移入本目录并补 `@task` 元数据、提交 git。

> 阶段四（@task 契约 + Registry）落地前，本目录为空是正常的。
