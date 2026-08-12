# CLAUDE.md

claude --dangerously-skip-permissions

> 本项目指导文件。**新会话读本文件 + `任务进度.md` + `设计实现.md` 即可上手。**
> avc_genshin 处于设计/早期实现阶段，本文描述目标架构。

## 1. 项目是什么

让 AI 持续、动态地添加原神自动化任务的 Python 框架：AI 生成 Python 任务脚本，框架加载运行，靠 CV+模拟输入操作游戏。三层分工（边界清晰，勿跨层）：

| 层 | 位置 | 职责 |
|---|---|---|
| **L1 avc**（C++ SDK） | `D:/Work/github/avc` | 通用 CV+IO：截图/输入/模板匹配/OCR/ONNX，SWIG→Python `avc` |
| **L2 avc_genshin**（本仓库） | 本文 | 框架：任务体系/Runtime/守护库/高层 API/导航战斗等领域能力 |
| **L3 AI 任务插件** | `tasks/*.py` | 具体业务（领日常/刷秘境/采集…），**AI 动态生成并添加** |

## 2. 核心原则（务必遵守）

1. **Python 代码即流程**：任务流程用纯 Python（`if/while/for/await`），**不用** JSON 流程定义/状态机类/中央轮询调度器。（"无框架/无状态机/无轮询"指流程层；Runtime 基础设施是要的。）
2. **AI 动态添加任务**：任务是一等公民，写 `tasks/*.py` 即插件，热加载，按名调用、可组合。
3. **基础设施不侵入流程**：Runtime 只管"安全地跑你写的 Python"，不规定流程走向。

> 对照 BetterGI（`D:/Work/github/better-genshin-impact`）：借鉴双回路/上下文/取消/1080p 归一化/ONNX 与路径数据；**摒弃** JSON 编排、`StateMachineBase`、中央轮询触发器。

## 3. 执行引擎（Runtime）要点

- **同步外壳、异步内核**：AI 写同步代码，引擎内部 asyncio，主脚本经工作线程+同步桥调 loop。
- **单线程 loop 天然解决 avc 并发安全**：所有 avc 调用都在 loop 线程，协作式调度无需加锁。
- **守护 = 自己的 `while+sleep` 循环**（控制流在手），非 BGI 那种"调度器每帧回调触发器"。
- **统一 `CancellationToken`**：取消/超时/异常 → 卸载所有守护 + 释放按键。详见 `docs/design/01`。

## 4. 怎么写一个任务（任务契约）

```python
# tasks/daily_quest.py
from framework import task
@task(name="daily_quest", desc="完成每日委托", daemons=["auto_skip"],
      params={"count": {"type": "int", "default": 4}})
def main(ctx, g, count=4):
    g.teleport_to("蒙德城"); ctx.mount("auto_pick"); g.go_to(katherine_pos)  # 边走边按 F
    ctx.unmount("auto_pick"); g.talk("领取每日委托奖励"); return {"claimed": True}
```

- `ctx`=`GameContext`（avc：`sc/ic/tm/ocr`）；`g`=高层 API。任务可调任务：`ctx.run("farm_domain", name="绝缘", times=3)`（纯 Python 调用）。
- 持久任务放 `tasks/`；即时任务现场生成缓存到 `cache/tasks/`，可提升为持久任务。

## 5. avc API 速查（完整见 `设计实现.md §6`）

```python
from avc import Input, Vision, Image
from avc._core import KeyCode, MouseButton
sc = Input.createScreenCapture(); sc.setWindow("原神"); sc.refresh(); buf = sc.getBuffer()  # IImageBuffer(格式见 imageType)
ic = Input.createInputController()
ic.click(x,y); ic.press(KeyCode.f); ic.hotkey(KeyCode.ctrl,KeyCode.c); ic.setMoveDurationMs(200)  # ⚠️无 setHumanize→§8
tm = Vision.createTemplateMatcher(); tm.addTemplatePath("x.png",0.8); tm.match(buf); tm.getMatch(0)
ocr = Vision.createTextRecognizer(); ocr.recognize(buf); ocr.getMatch(0)   # →(text,rect)
Image.crop(buf, x, y, w, h); buf.to_bytes()   # bytes(格式见 imageType)
```

## 6. 高层 API（`g.*`，AI 默认用这层）

```
g.teleport_to(name)  g.go_to(pos)  g.talk(opt)  g.talk_skip()  g.wait_text/wait_template/wait_main_ui
g.has_enemy()  g.find_nearest_enemy()  g.is_loading()  g.click(x,y)  g.press(key,hold=0)
ctx.mount/unmount(name)  ctx.suspend_all()  ctx.run(name, **params)
```

## 7. 项目结构（src layout，详见 `docs/design/03`）

`src/`：`framework/`（核心+公共层）·`abilities/`（navigation/fighter/detector/vision_utils/game_state）·`tasks/`（L3 插件）。
`resources/`（templates/paths/models/ocr/map，大文件 gitignore）；`cache/`·`logs/`(JSONL)·`debug/`(失败存证)；`main.py`（薄壳）·`pyproject.toml`（pip install -e .）。
四类分离：**代码**·**插件**·**资源**·**产物**。

## 8. 约定（踩过的坑，勿重踩）

- **分辨率任意**：非 1080P 自动缩放归一化到 1920×1080（坐标皆基于 1080p），无启动检查。
- **拟人化是框架层**：⚠️ avc Python 绑定**无** `setHumanize`（仅 `setMoveDurationMs`/`setMoveSteps`/`setKeyDelayMs`）。坐标抖动+0.8–1.2× 随机间隔+按住时长随机由 `framework/utils.py`+`GameContext`/`high_level_api` 套用。原神管理员运行，本程序亦须管理员。
- **YOLO=标准 Ultralytics**（BGI=YoloSharp，无自定义解码）：①**别再 sigmoid**（已 bake 进图）②`[1,4+nc,N]` 需转置 ③letterbox 反变换（conf 0.3/IoU 0.45）；类名从 ONNX 元数据 `names` 读（旧 `label.json` 废弃），类名**带空格**如 `"enemy identify"`。见 `src/abilities/detector.py`。
- **能力可观测性（绑定）**：每个 ability 与 `src/tasks/*.py` 在「看/选/结果」决策点发 `ctx.observe.event(kind, ability=, ok=, reason=, ...)`；`ctx.observe` 永可调用（无 runtime 返 no-op，不判空）。`run_summary` 按 `ability` 分组定位坏点；热轮询传 `_quiet=True`/`throttle_key` 节流。新 ability/任务必须遵守，详见 `设计实现.md §2`。
- 仅供技术研究学习；使用自动化工具可能违反游戏服务条款。

## 9. 运行 & 实机验证

```bash
python main.py                       # 交互式
python main.py --intent "完成日常"    # 单次意图
python main.py --task daily_quest    # 跑已注册任务
python main.py --task verify         # 游戏内诊断（实机标定入口）
```
依赖：`avc`(先构建)/`onnxruntime`/`numpy`/`opencv-python`/`anthropic`。

**实机验证要求（每次实机必做）**：
1. 跑任意任务必带可观测性（见 §8）——全程 `ctx.observe` 事件流 + 结束 `run_summary`，可查 `logs/<run_id>.jsonl`；不裸跑。
2. 每 60s 自动截图 + 提交大模型判读「当前在哪个界面 / 卡在哪步 / 下一步做什么」——实机易卡流程中段不自知，卡点即时上报。

## 10. 参考文档

| 文档 | 用途 |
|---|---|
| `任务进度.md` | **当前进度快照**（阶段/交付入口/变更日志，每完成任务更新） |
| `设计实现.md` | **完整设计与实现**（本文件的展开） |
| `docs/design/00–03,08` | 框架总纲/执行引擎/可靠性三件套/项目结构/BGI 对齐核查 |
| `docs/bgi-framework/00-主体框架.md` | BetterGI 源码分析（借鉴对象） |
| `D:/Work/github/better-genshin-impact` | BetterGI 源码（参考） |
| `D:/Work/github/avc` | avc C++ SDK（底层能力来源） |
