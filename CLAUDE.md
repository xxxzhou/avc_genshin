# CLAUDE.md

> 本项目指导文件。**新会话读本文件 + `任务进度.md` + `设计实现.md` 即可上手。**
> avc_genshin 处于设计/早期实现阶段，本文描述目标架构。

## 1. 项目是什么

**avc_genshin** —— 一个让 AI 持续、动态地添加原神自动化任务的 Python 框架。
AI 生成 Python 任务脚本，框架加载并运行它，通过计算机视觉 + 模拟输入操作游戏。

**三层分工（边界清晰，勿跨层）：**

| 层 | 位置 | 职责 |
|---|---|---|
| **L1 avc**（C++ SDK） | `D:/Work/github/avc` | 通用 CV+IO：截图、输入、模板匹配、OCR、ONNX。SWIG→Python `avc` 模块 |
| **L2 avc_genshin**（本仓库） | 本文 | 框架：任务体系、Runtime、守护任务库、高层 API、导航/战斗等领域能力 |
| **L3 AI 任务插件** | `tasks/*.py` | 具体业务（领日常、刷秘境、采集…），**由 AI 动态生成并添加** |

## 2. 核心原则（务必遵守）

1. **Python 代码即流程**：任务流程用纯 Python（`if/while/for/await`）。**不用** JSON 流程定义、**不用** 状态机类、**不用** 中央轮询调度器。（"无框架/无状态机/无轮询"指流程层；基础设施 Runtime 是要的。）
2. **AI 动态添加任务**：任务是一等公民。写 `tasks/*.py` 即插件，热加载，注册后立即可按名调用、可组合。
3. **基础设施不侵入流程**：Runtime 负责"安全地跑你写的 Python"，**不规定流程怎么走**。

> 对照 BetterGI（`D:/Work/github/better-genshin-impact`）：借鉴其双回路/上下文/取消/1080p 归一化/ONNX 模型与路径数据；**摒弃**其 JSON 编排、`StateMachineBase`、`TaskTriggerDispatcher` 中央轮询。

## 3. 执行引擎（Runtime）要点

- **同步外壳、异步内核**：AI 写同步代码；引擎内部 asyncio。主脚本跑工作线程，经同步桥调用 loop。
- **单线程 loop 天然解决 avc 并发安全**：所有 avc 调用都在 loop 线程，协作式调度，无需加锁。
- **守护任务 = 自己的 `while+sleep` 循环**（控制流在自己手里），**非** BetterGI 那种"调度器每帧回调触发器"。
- **统一 `CancellationToken`**：取消/超时/异常 → 卸载所有守护 + 释放按键。
- 详见 `docs/design/01-执行引擎.md`。

## 4. 怎么写一个任务（任务契约）

```python
# tasks/daily_quest.py
from framework import task

@task(
    name="daily_quest",
    desc="完成每日委托并领取奖励",      # AI 规划时据此判断是否复用
    daemons=["auto_skip"],            # 运行时自动挂载的守护
    params={"count": {"type": "int", "default": 4, "desc": "委托数量"}},
)
def main(ctx, g, count=4):
    g.teleport_to("蒙德城")
    ctx.mount("auto_pick")            # 边走边按 F（并发守护）
    g.go_to(katherine_pos)
    ctx.unmount("auto_pick")
    g.talk("领取每日委托奖励")
    return {"claimed": True}
```

- `ctx` = `GameContext`（avc 实例：`sc/ic/tm/ocr`）；`g` = 高层 API。
- 任务可调用任务：`ctx.run("farm_domain", name="绝缘", times=3)`（纯 Python 调用，非配置编排）。
- 持久任务放 `tasks/`；即时任务由 AI 现场生成，缓存到 `cache/tasks/`，可选提升为持久任务。

## 5. avc API 速查（最常用）

```python
from avc import Input, Vision, Image
from avc._core import KeyCode, MouseButton

# 截图
sc = Input.createScreenCapture()
sc.setWindow("原神"); sc.activateWindow("原神")
sc.refresh(); buf = sc.getBuffer()          # IImageBuffer (BGRA8)
sx, sy = sc.toScreen(buf_x, buf_y)          # 截图坐标→屏幕坐标

# 输入
ic = Input.createInputController()
ic.click(x, y); ic.press(KeyCode.f); ic.press(KeyCode.w, holdMs=500)
ic.hotkey(KeyCode.ctrl, KeyCode.c); ic.typeText("txt")
ic.setMoveDurationMs(200); ic.setHumanize(True)   # 拟人化必须启用

# 模板匹配
tm = Vision.createTemplateMatcher()
tm.addTemplatePath("templates/x.png", 0.8); n = tm.match(buf); r = tm.getMatch(0)

# OCR
ocr = Vision.createTextRecognizer()
n = ocr.recognize(buf); text, r = ocr.getMatch(0)

# 图像
raw = buf.to_bytes()                         # → bytes，喂 YOLO
cropped = Image.crop(buf, x, y, w, h)
```

完整 API 见 `设计实现.md §6`。

## 6. 高层 API（`g.*`，AI 默认用这层）

```
g.teleport_to(name)  g.go_to(pos)  g.talk(option)  g.talk_skip()
g.wait_text(kw, timeout)  g.wait_template(path)  g.wait_main_ui(timeout)
g.has_enemy()  g.find_nearest_enemy()  g.is_loading()
g.click(x,y)  g.press(key, hold=0)
ctx.mount(name)  ctx.unmount(name)  ctx.suspend_all()  ctx.run(name, **params)
```

## 7. 项目结构（src layout，详见 `docs/design/03-项目结构与公共层.md`）

```
avc_genshin/
├── src/                     所有代码（三个顶层包）
│   ├── framework/           框架核心+公共层（import: from framework import task）
│   ├── abilities/           领域能力：navigation/fighter/detector/vision_utils/game_state
│   └── tasks/               L3 持久任务插件（AI 写入；内置示例进 git）
├── resources/               游戏资源：templates/paths/models/ocr/map（大文件 gitignore）
├── cache/ logs/ debug/      即时缓存 / 结构化日志(JSONL) / 失败存证
├── main.py                  薄入口壳（真代码在 src；也支持 python -m framework）
├── pyproject.toml           src layout（pip install -e .）
└── docs/ CLAUDE.md 设计实现.md
```

四类分离：**代码**(src/) · **插件**(tasks/) · **资源**(resources/) · **产物**(cache/logs/debug)。

## 8. 约定

- **分辨率 1920×1080**，所有坐标基于 1080p；启动检查，不符报错。
- **拟人化必须启用**——⚠️ avc 的 Python 绑定**没有** `setHumanize`（实测 `swig/python/avc/input.py` 仅 `setMoveDurationMs`/`setMoveSteps`/`setKeyDelayMs`）。拟人化（坐标抖动 + 0.8–1.2× 随机操作间隔 + 按住时长随机）由**框架层**实现：`framework/utils.py` 原语 + `GameContext`/`high_level_api` 每次输入套用。原神以管理员运行，本程序亦须管理员。
- **BetterGI ONNX = YoloSharp = 标准 Ultralytics YOLOv8/YOLO11 格式**（源码核实：BGI 无自定义解码，全委托 YoloSharp NuGet）。真正坑：①**别再做 sigmoid**（已 bake 进图）②布局 `[1,4+nc,N]` 需转置 ③letterbox min 比例+居中 padding，框按 `(x-pad)/scale` 反变换（conf 0.3 / IoU 0.45，NMS 按类纯 IoU）。类名从 ONNX 元数据 `names` 读（旧 `label.json` 已废弃）。实现见 `src/abilities/detector.py`。
- 仅供技术研究学习；使用自动化工具可能违反游戏服务条款。

## 9. 运行（规划）

```bash
python main.py                         # 交互式：输入意图
python main.py --intent "完成日常"      # 单次意图
python main.py --task daily_quest      # 直接跑已注册任务
```
依赖：`avc`（C++ SDK，需先构建）、`onnxruntime`、`numpy`、`opencv-python`、`anthropic`。

## 10. 参考文档

| 文档 | 用途 |
|---|---|
| `任务进度.md` | **当前进度快照**（阶段状态/交付入口/变更日志；每完成任务更新） |
| `设计实现.md` | **完整设计与实现**（本文件的展开） |
| `docs/design/00-框架总纲.md` | 框架纲领：三层 / 任务契约 / 动态添加 / AI 工作流 |
| `docs/design/01-执行引擎.md` | Runtime / 守护 / 取消 / 沙箱 |
| `docs/design/02-可靠性三件套.md` | 场景估计 / 并发权属 / 可观测 / 护栏 |
| `docs/design/03-项目结构与公共层.md` | src 布局 / 公共层 / 资源 / 结构化日志 |
| `docs/bgi-framework/00-主体框架.md` | BetterGI 源码分析（借鉴对象） |
| `D:/Work/github/better-genshin-impact` | BetterGI 源码（参考） |
| `D:/Work/github/avc` | avc C++ SDK（底层能力来源） |
