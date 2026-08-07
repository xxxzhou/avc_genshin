# avc_genshin

一个让 AI 持续、动态地添加**原神自动化任务**的 Python 框架。

AI 生成 Python 任务脚本，框架加载并运行它，通过计算机视觉（CV）+ 模拟输入操作游戏。任务是一等公民——写一个 `tasks/*.py` 即插件，热加载，注册后立即可按名调用、可组合。

> ⚠️ 仅供技术研究与学习。使用自动化工具可能违反游戏服务条款。

## 背景

本项目脱胎于一个播放器：在播放器核心功能完成后，借助 AI 整合了语音转文字、超分辨率、翻译、去水印、OCR 等 AI 能力，并把 OpenCV 识别与底层 IO 封装成 [avc](../avc) C++ SDK（经 SWIG 暴露为 Python `avc` 模块）。

在这套 CV+IO 底座之上，想做一个 Computer Use 方向（电脑操作自动化）的 agent。先想着试试用agent处理自动玩原神来测试下,让 AI 按需求快速编写自动化逻辑，框架只负责安全地跑。于是有了 avc_genshin——avc 提供通用 CV+IO，本项目负责框架与原神领域能力，AI 动态产出任务插件。

## 核心理念

- **Python 代码即流程**：任务流程用纯 Python（`if/while/for/await`），不用 JSON 流程定义、不用状态机类、不用中央轮询调度器。
- **AI 动态添加任务**：持久任务放 `tasks/`，即时任务由 AI 现场生成并缓存，可选提升为持久任务。
- **基础设施不侵入流程**：Runtime 只负责"安全地跑你写的 Python"，不规定流程怎么走。

## 三层架构

| 层 | 位置 | 职责 |
|---|---|---|
| **L1 avc**（C++ SDK） | [avc](../avc) | 通用 CV+IO：截图、输入、模板匹配、OCR、ONNX，经 SWIG 暴露为 Python `avc` 模块 |
| **L2 avc_genshin**（本仓库） | 本项目 | 框架：任务体系、Runtime、守护任务库、高层 API、导航/战斗等领域能力 |
| **L3 AI 任务插件** | `tasks/*.py` | 具体业务（领日常、刷秘境、采集…），由 AI 动态生成并添加 |

## 任务示例

```python
from framework import task

@task(name="daily_quest", desc="完成每日委托并领取奖励", daemons=["auto_skip"])
def main(ctx, g, count=4):
    g.teleport_to("蒙德城")
    g.go_to(katherine_pos)
    g.talk("领取每日委托奖励")
    return {"claimed": True}
```

`ctx` 提供 avc 实例与守护挂载，`g` 提供导航/战斗/对话等高层 API。

## 项目结构

```
src/framework/   框架核心 + 公共层（任务契约 / Runtime / 高层 API）
src/abilities/   领域能力：navigation / fighter / detector / game_state
src/tasks/       L3 持久任务插件（AI 写入；内置示例进 git）
resources/       游戏资源：templates / paths / models / map（大文件 gitignore）
```

## 运行

处于设计与早期实现阶段，依赖需先构建的 [avc](../avc) C++ SDK、`onnxruntime`、`numpy`、`opencv-python`、`anthropic`。

```bash
pip install -e .
python main.py                         # 交互式：输入意图
python main.py --intent "完成日常"      # 单次意图
python main.py --task daily_quest      # 直接跑已注册任务
```

## 致谢

本项目参考并借鉴了开源项目 [better-genshin-impact](https://github.com/babalae/better-genshin-impact)（BetterGI）的双回路、上下文、取消机制、1080p 归一化、ONNX 模型与路径数据等设计，特此感谢。
