# 原神自动日常 — 技术方案

## 1. 架构总览

```
+--------------------------------------------------+
|          原神日常自动化 (Python 脚本)               |
|                                                  |
|  用户意图 → AI 生成 Python 脚本 → 直接执行         |
|                                                  |
|  +-----------+  +-----------+                    |
|  | AI 代码生成 |  | 脚本执行器 |                    |
|  +-----------+  +-----------+                    |
+----------------+---------------------------------+
|   avc SDK      |      onnxruntime (Python)       |
|  (截图+操作)    |    (BetterGI ONNX 模型推理)      |
|                |                                |
| IScreenCapture |  bgi_world.onnx  (敌人/血条/采集) |
| IInputControl  |  bgi_mine.onnx   (矿物识别)     |
| ITemplateMatch |  bgi_fish.onnx   (钓鱼检测)     |
| ITextRecognize |  avatar_side.onnx(角色识别)      |
| IImageBuffer   |  PaddleOCR       (文字识别)      |
+----------------+--------------------------------+
```

**核心原则：AI 生成 Python 脚本，脚本直接执行流程。没有框架，没有状态机，没有轮询调度。**

- **avc SDK**：截图（IScreenCapture）、操作（IInputController）、模板匹配（ITemplateMatcher）、OCR（ITextRecognizer）
- **onnxruntime**：加载 BetterGI 的 ONNX 模型做目标检测推理
- **AI（LLM）**：根据用户意图生成 Python 脚本，脚本调用 avc + onnxruntime API 完成任务
- **脚本执行器**：校验 + 执行 AI 生成的脚本，注入 API 对象，提供安全保护

### 1.1 与 BetterGI 的核心区别

| 维度 | BetterGI | 本项目 |
|------|----------|--------|
| 语言 | C# / .NET 8 / WPF | Python 3 / 无 UI |
| 任务调度 | TaskTriggerDispatcher (定时器轮询 + ITaskTrigger) | **无调度器，AI 生成脚本直接执行** |
| 任务编排 | JSON 战斗脚本 + 硬编码状态机 | **AI 生成 Python 代码，无 JSON** |
| 截图 | Fischless.GameCapture | avc IScreenCapture (~5ms) |
| YOLO | Compunet.YoloSharp + OnnxRuntime | onnxruntime Python |
| OCR | PaddleOCR ONNX 版 | avc ITextRecognizer (PaddleOCR v6) |
| 模板匹配 | OpenCvSharp | avc ITemplateMatcher |
| 操作 | Fischless.WindowsInput (PostMessage/SendInput) | avc IInputController (含拟人化) |
| 地图定位 | SIFT 特征匹配 (300M+ 数据) | avc ITemplateMatcher + 全地图模板 |
| 路径导航 | AutoPathing (JSON 路径 + 执行器) | 复用 BetterGI JSON 路径数据，Python 执行器 |
| 战斗 | AutoFight (角色识别+技能CD+脚本引擎) | 简化版：固定连招 + 技能CD检测 |
| UI | WPF + 遮罩窗口 | 无 UI，日志 + 截图调试 |

**关键复用**：直接使用 BetterGI 的 ONNX 模型文件和 JSON 路径数据，不重新训练。

---

## 2. 识别方案

### 2.1 目标检测 — BetterGI ONNX 模型

直接复用 BetterGI 已训练好的 ONNX 模型，无需自己训练。

| 模型文件 | 用途 | 检测类别 |
|---------|------|---------|
| `bgi_world.onnx` | 大世界目标检测 | `health_bar`(血条), `enemy_identify`(敌人), 采集物等 |
| `bgi_fish.onnx` | 钓鱼检测 | 鱼类型、抛竿点 |
| `bgi_mine.onnx` | 矿物识别 | 各种矿石 |
| `bgi_tree.onnx` | 秘境古树 | 秘境内目标 |
| `avatar_side_classify_sim.onnx` | 角色识别 | 角色侧脸分类 |
| `q_classify_sim.onnx` | 技能CD分类 | Q技能冷却状态 |
| `avatar.onnx` | 队伍头像识别 | 角色头像 |

**模型获取**：从 BetterGI GitHub Release 提取 `Assets/Model/` 目录。

**推理代码**：

```python
import onnxruntime as ort
import numpy as np
import cv2

class GenshinDetector:
    def __init__(self, model_path, providers=None):
        if providers is None:
            providers = ['CUDAExecutionProvider', 'CPUExecutionProvider']
        self.session = ort.InferenceSession(model_path, providers=providers)
        self.input_name = self.session.get_inputs()[0].name
        self.input_shape = self.session.get_inputs()[0].shape  # [1, 3, H, W]

    def detect(self, bgra_bytes, width, height):
        """输入 avc IScreenCapture 的 BGRA8 bytes，返回检测结果"""
        img = np.frombuffer(bgra_bytes, dtype=np.uint8).reshape(height, width, 4)
        img = img[:, :, :3][:, :, ::-1]  # BGRA -> RGB
        _, _, model_h, model_w = self.input_shape
        img = cv2.resize(img, (model_w, model_h))
        blob = img.astype(np.float32) / 255.0
        blob = blob.transpose(2, 0, 1)[np.newaxis, ...]
        outputs = self.session.run(None, {self.input_name: blob})
        return self._parse_results(outputs, width, height, model_w, model_h)

    def _parse_results(self, outputs, orig_w, orig_h, model_w, model_h):
        scale_x = orig_w / model_w
        scale_y = orig_h / model_h
        results = {}
        detections = self._decode_yolo_output(outputs[0])
        for det in detections:
            x1, y1, x2, y2, conf, cls_id, cls_name = det
            rect = {'x1': int(x1*scale_x), 'y1': int(y1*scale_y),
                    'x2': int(x2*scale_x), 'y2': int(y2*scale_y), 'confidence': conf}
            if cls_name not in results:
                results[cls_name] = []
            results[cls_name].append(rect)
        return results

    def _decode_yolo_output(self, output):
        # TODO: BetterGI 用 YoloSharp 导出，格式与标准 YOLOv8 略有不同
        # 参考 BetterGI: Core/Recognition/ONNX/YOLO/Predictor.cs
        pass
```

### 2.2 位置定位 — 小地图模板匹配

BetterGI 用 SIFT 特征匹配（非简单模板匹配），精度更高。

**原理**：裁剪小地图 → 预处理（去圆边、旋转补偿）→ 与全地图特征数据库匹配 → 得到玩家坐标。

**BetterGI 地图数据**：300M+，从 Release 单独下载。

```python
from avc import Vision, Image

full_map = Image.loadImage("teyvat_full_map.png")
MINIMAP_RECT = (20, 20, 220, 220)

def get_player_position(buf, tm):
    minimap = buf.crop(*MINIMAP_RECT)
    result = tm.match(minimap, full_map)
    if result.found:
        return result.x, result.y
    return None
```

### 2.3 文字识别 — avc OCR

```python
from avc import Vision
ocr = Vision.createTextRecognizer()

def recognize_text(buf, region=None):
    if region:
        buf = buf.crop(*region)
    return ocr.recognize(buf)  # [{text, confidence, rect}]
```

### 2.4 UI 按钮识别 — 模板匹配

```python
from avc import Vision
tm = Vision.createTemplateMatcher()

def find_ui_element(buf, template, threshold=0.8):
    result = tm.match(buf, template, threshold)
    if result.found:
        return result.x, result.y, result.width, result.height
    return None
```

### 2.5 识别策略汇总

| 识别目标 | 方式 | 工具 |
|---------|------|------|
| 敌人/血条/采集物 | YOLO 检测 | onnxruntime + bgi_world.onnx |
| 矿物 | YOLO 检测 | onnxruntime + bgi_mine.onnx |
| 玩家位置 | 模板匹配 | avc ITemplateMatcher |
| UI 按钮/图标 | 模板匹配 | avc ITemplateMatcher |
| 任务文字/状态 | OCR | avc ITextRecognizer |
| 角色识别 | 分类模型 | onnxruntime + avatar_side.onnx |
| 技能CD状态 | 分类模型 | onnxruntime + q_classify.onnx |

---

## 3. 操作方案

### 3.1 avc IInputController

```python
from avc import Input
ic = Input.createInputController()

ic.moveTo(x, y)
ic.click(x, y)
ic.doubleClick(x, y)
ic.drag(x1, y1, x2, y2)
ic.scroll(0, -3)
ic.press(KeyCode_w)
ic.press(KeyCode_e)
ic.press(KeyCode_q)
ic.hotkey([KeyCode_alt, KeyCode_f1])
ic.typeText("你好")
ic.setMoveDurationMs(200)
ic.setMoveSteps(20)
ic.setKeyDelayMs(50)
```

### 3.2 拟人化（需补 SWIG 绑定，优先级高）

```python
ic.setHumanize(True)       # 贝塞尔轨迹(10-30%偏移) + 自然点击(40-90ms) + 抖动(+/-30%)
ic.setJitterSeed(42)
ic.setClickHoldMs(60)
```

**⚠️ 拟人化必须启用，不是可选的。**

### 3.3 窗口激活（需补 SWIG 绑定）

临时替代：

```python
import ctypes
user32 = ctypes.windll.user32
hwnd = user32.FindWindowW(None, "原神")
if hwnd:
    user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
```

### 3.4 坐标转换

```python
sc = Input.createScreenCapture()
sc.setWindow("原神")
screen_x, screen_y = sc.toScreen(100, 200)
ic.click(screen_x, screen_y)
```

---

## 4. 截图方案

### 4.1 IScreenCapture（推荐）

```python
from avc import Input
sc = Input.createScreenCapture()
sc.setWindow("原神")
sc.refresh()
buf = sc.getBuffer()          # IImageBuffer (BGRA8)
raw_bytes = buf.to_bytes()    # bytes → 喂 onnxruntime
w, h = buf.getWidth(), buf.getHeight()
```

性能：~5ms/帧。

---

## 5. AI 任务编排 — 核心方案

> **本项目核心：不用框架、不用状态机、不用 JSON 编排。AI 直接生成 Python 脚本，脚本上来执行流程。**

### 5.1 为什么用 AI 生成而非传统编排

| 维度 | 传统编排 (JSON/状态机) | AI 生成 Python 脚本 |
|------|----------------------|-------------------|
| 灵活性 | 受限于预定义 action 类型 | 任意 Python 逻辑 |
| 精确度 | 通用逻辑，无法针对场景优化 | LLM 理解语义，生成针对性代码 |
| 开发速度 | 每个任务手写 JSON + 解析代码 | 自然语言 → 直接生成可执行代码 |
| 条件分支 | JSON 难表达复杂条件 | Python 原生 if/else/loop |
| 错误处理 | 统一重试，无法区分场景 | 场景特定的异常处理 |
| 用户自定义 | 需学 JSON 格式 | 自然语言描述即可 |
| 维护 | JSON + 代码双重维护 | 只维护 Python 代码 |

### 5.2 工作流程

```
用户意图 (自然语言)
    │
    ▼
+------------------+
|  AI Task Planner |  ← LLM (Claude/GPT)
|  生成 Python 脚本  |     输入: 意图 + API清单 + 游戏状态
+------------------+
    │
    ▼
+------------------+
|  Code Validator  |  ← 语法检查 + API白名单 + 安全审查
+------------------+
    │
    ▼
+------------------+
|  Script Runner   |  ← exec() 受限环境执行
|  注入API + 超时   |     注入 avc 对象 + 超时保护 + 日志
+------------------+
```

### 5.3 暴露给 AI 的 API 清单

AI 生成代码时只能使用以下 API：

```python
# === 截图 ===
sc.refresh()                          # 刷新截图
buf = sc.getBuffer()                  # 获取图像缓冲
raw = buf.to_bytes()                  # 转 bytes
w, h = buf.getWidth(), buf.getHeight()
buf = buf.crop(x, y, w, h)           # 裁剪
sx, sy = sc.toScreen(x, y)           # 截图坐标→屏幕坐标

# === 操作 ===
ic.moveTo(x, y)
ic.click(x, y)
ic.press(key)                         # KeyCode: w/a/s/d, e, q, f, esc, space, 1/2/3/4, tab, m, j
ic.hold(key, duration_ms)
ic.scroll(dx, dy)
ic.setHumanize(True)

# === 识别 ===
detector.detect(raw_bytes, w, h)      # → {"cls": [{"x1","y1","x2","y2","conf"}]}
ocr.recognize(buf)                    # → [{"text","confidence","rect"}]
tm.match(buf, template, threshold)    # → {found, x, y, width, height, score}
get_player_position(buf, tm)          # → (x, y) or None

# === 路径导航 ===
path_executor.execute(path_data)      # 执行路径 JSON
path_executor.load_path(name)         # 加载路径文件

# === 战斗 ===
fighter.fight_until_clear(timeout)    # 战斗直到清敌
fighter.switch_character(slot)        # 切换角色 1-4

# === 工具 ===
sleep(seconds)                        # 等待（自动加随机抖动 0.8-1.2x）
wait_until(predicate, timeout=30)     # 等待条件满足
find_nearest(detections, x, y)        # 找最近检测目标
click_center(ic, sc, det)             # 点击检测目标中心
```

### 5.4 LLM Prompt 模板

```python
TASK_PLANNER_PROMPT = """你是一个原神游戏自动化脚本生成器。

## 可用 API
{api_list}

## 当前游戏状态
{game_state}

## 用户意图
{user_intent}

## 要求
1. 只使用上面列出的 API，不要 import 其他库
2. 生成完整的 Python 函数，函数名以 task_ 开头
3. 包含异常处理（try/except）
4. 每个步骤有超时保护
5. 操作间 sleep 0.5-2 秒随机
6. 必须调用 ic.setHumanize(True)
7. 坐标基于 1920x1080

直接输出 Python 代码，不要解释。
"""
```

### 5.5 AI 任务编排实现

```python
import anthropic
import ast
import textwrap
import hashlib
import os
import time
import random
import threading

class AITaskPlanner:
    """AI 任务规划器 — 生成 Python 脚本"""

    def __init__(self, api_key=None, model="claude-sonnet-5"):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def plan(self, user_intent: str, game_state: dict) -> str:
        prompt = TASK_PLANNER_PROMPT.format(
            api_list=API_LIST,
            game_state=game_state,
            user_intent=user_intent
        )
        response = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}]
        )
        return self._extract_code(response.content[0].text)

    def _extract_code(self, text: str) -> str:
        if "```python" in text:
            code = text.split("```python")[1].split("```")[0]
        elif "```" in text:
            code = text.split("```")[1].split("```")[0]
        else:
            code = text
        return textwrap.dedent(code).strip()


class CodeValidator:
    """代码校验 — 确保安全"""

    DANGEROUS = {'exec', 'eval', 'compile', '__import__', 'open', 'os', 'sys', 'subprocess'}

    def validate(self, code: str) -> tuple[bool, str]:
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return False, f"语法错误: {e}"
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                return False, "不允许 import"
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in self.DANGEROUS:
                    return False, f"不允许调用 {node.func.id}"
            if isinstance(node, ast.Attribute) and node.attr.startswith('_'):
                return False, f"不允许访问 {node.attr}"
        return True, ""


class TaskCodeCache:
    """缓存已生成的脚本，相同意图直接复用"""

    def __init__(self, cache_dir="cache/tasks"):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    def get(self, intent: str) -> str | None:
        key = hashlib.md5(intent.encode()).hexdigest()
        path = os.path.join(self.cache_dir, f"{key}.py")
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        return None

    def put(self, intent: str, code: str):
        key = hashlib.md5(intent.encode()).hexdigest()
        path = os.path.join(self.cache_dir, f"{key}.py")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"# Intent: {intent}\n# Generated: {time.strftime('%Y-%m-%d %H:%M')}\n\n{code}")


class ScriptRunner:
    """脚本执行器 — 注入 API，安全执行"""

    def __init__(self, sc, ic, detector, ocr, tm, path_executor=None, fighter=None):
        self.sc = sc
        self.ic = ic
        self.detector = detector
        self.ocr = ocr
        self.tm = tm
        self.path_executor = path_executor
        self.fighter = fighter
        self.planner = AITaskPlanner()
        self.validator = CodeValidator()
        self.cache = TaskCodeCache()

    def run_intent(self, user_intent: str):
        """从用户意图到执行的完整流程"""
        # 1. 查缓存
        code = self.cache.get(user_intent)
        if code:
            print(f"[缓存] 命中，直接执行")
        else:
            # 2. AI 生成
            game_state = self._get_game_state()
            code = self.planner.plan(user_intent, game_state)
            # 3. 校验
            is_safe, error = self.validator.validate(code)
            if not is_safe:
                print(f"[安全] 校验失败: {error}")
                return False
            # 4. 缓存
            self.cache.put(user_intent, code)
        # 5. 执行
        return self._exec(code)

    def _exec(self, code: str, timeout: int = 600):
        """在受限环境中执行脚本"""
        import math
        safe_globals = {
            'sc': self.sc, 'ic': self.ic,
            'detector': self.detector, 'ocr': self.ocr, 'tm': self.tm,
            'path_executor': self.path_executor, 'fighter': self.fighter,
            'sleep': lambda s: time.sleep(s * random.uniform(0.8, 1.2)),
            'wait_until': self._wait_until,
            'find_nearest': self._find_nearest,
            'click_center': self._click_center,
            'get_player_position': lambda buf, tm: get_player_position(buf, tm),
            'print': print, 'len': len, 'range': range,
            'int': int, 'float': float, 'str': str,
            'abs': abs, 'min': min, 'max': max, 'round': round,
            'math': math, 'time': time, 'random': random,
            'KeyCode_w': 'w', 'KeyCode_a': 'a', 'KeyCode_s': 's', 'KeyCode_d': 'd',
            'KeyCode_e': 'e', 'KeyCode_q': 'q', 'KeyCode_f': 'f',
            'KeyCode_esc': 'esc', 'KeyCode_space': 'space',
            'KeyCode_tab': 'tab', 'KeyCode_m': 'm', 'KeyCode_j': 'j',
            'KeyCode_1': '1', 'KeyCode_2': '2', 'KeyCode_3': '3', 'KeyCode_4': '4',
        }
        try:
            exec(code, safe_globals)
            for name, obj in safe_globals.items():
                if name.startswith('task_') and callable(obj):
                    obj(self.sc, self.ic, self.detector, self.ocr, self.tm)
            return True
        except Exception as e:
            print(f"[执行] 错误: {e}")
            return False

    def _wait_until(self, predicate, timeout=30):
        start = time.time()
        while time.time() - start < timeout:
            self.sc.refresh()
            buf = self.sc.getBuffer()
            if predicate(buf):
                return True
            time.sleep(0.5)
        return False

    def _find_nearest(self, detections, x, y):
        nearest = None
        min_dist = float('inf')
        for cls, dets in detections.items():
            for d in dets:
                cx = (d['x1'] + d['x2']) / 2
                cy = (d['y1'] + d['y2']) / 2
                dist = ((cx - x)**2 + (cy - y)**2) ** 0.5
                if dist < min_dist:
                    min_dist = dist
                    nearest = {'class': cls, **d}
        return nearest

    def _click_center(self, det):
        cx = (det['x1'] + det['x2']) / 2
        cy = (det['y1'] + det['y2']) / 2
        sx, sy = self.sc.toScreen(int(cx), int(cy))
        self.ic.click(sx, sy)

    def _get_game_state(self):
        self.sc.refresh()
        buf = self.sc.getBuffer()
        raw = buf.to_bytes()
        w, h = buf.getWidth(), buf.getHeight()
        detections = self.detector.detect(raw, w, h)
        texts = self.ocr.recognize(buf)
        pos = get_player_position(buf, self.tm)
        return {
            'resolution': f'{w}x{h}',
            'detections': {k: len(v) for k, v in detections.items()},
            'texts': [t['text'] for t in texts[:10]],
            'player_position': pos,
        }
```

### 5.6 使用示例

```python
runner = ScriptRunner(sc, ic, detector, ocr, tm, path_executor, fighter)

# 用户说：
runner.run_intent("完成每日委托并领取奖励")
runner.run_intent("去蒙德城找凯瑟琳领日常奖励")
runner.run_intent("刷3次绝缘本")
runner.run_intent("去采5朵甜甜花")
runner.run_intent("自动钓鱼，钓满背包")
```

### 5.7 AI 生成的脚本示例

用户说 **"完成每日委托并领取奖励"**，AI 可能生成：

```python
def task_daily_quest(sc, ic, detector, ocr, tm):
    ic.setHumanize(True)
    # 1. 打开任务菜单
    ic.press(KeyCode_j)
    sleep(1.5)
    # 2. 查看每日委托列表
    sc.refresh()
    buf = sc.getBuffer()
    texts = ocr.recognize(buf)
    daily_quests = [t for t in texts if '委托' in t['text']]
    if not daily_quests:
        print("没有未完成的委托")
        return
    # 3. 逐个追踪并完成
    for quest in daily_quests:
        # 点击委托追踪
        sx, sy = sc.toScreen(quest['rect']['x'], quest['rect']['y'])
        ic.click(sx, sy)
        sleep(1.0)
        # 关闭菜单
        ic.press(KeyCode_esc)
        sleep(1.0)
        # 等待传送提示
        wait_until(lambda buf: find_ui_element(buf, "teleport", tm), timeout=10)
        # 传送
        sc.refresh()
        buf = sc.getBuffer()
        tp = find_ui_element(buf, "teleport", tm)
        if tp:
            ic.click(sc.toScreen(tp[0] + tp[2]//2, tp[1] + tp[3]//2))
            sleep(3.0)
        # 等待加载完成
        wait_until(lambda buf: not find_ui_element(buf, "loading", tm), timeout=30)
        sleep(2.0)
        # 战斗/采集
        sc.refresh()
        buf = sc.getBuffer()
        raw = buf.to_bytes()
        w, h = buf.getWidth(), buf.getHeight()
        dets = detector.detect(raw, w, h)
        if 'enemy_identify' in dets or 'health_bar' in dets:
            fighter.fight_until_clear(timeout=120)
        elif 'collect' in dets:
            ic.press(KeyCode_f)
            sleep(2.0)
    # 4. 回凯瑟琳领奖
    ic.press(KeyCode_m)
    sleep(1.5)
    # 点击蒙德城传送点
    ic.click(sc.toScreen(830, 320))
    sleep(1.0)
    ic.press(KeyCode_esc)
    sleep(2.0)
    # 走到凯瑟琳面前
    ic.press(KeyCode_w)
    sleep(2.0)
    ic.press(KeyCode_f)
    sleep(1.0)
    # 对话中点击"领取每日委托奖励"
    for _ in range(5):
        sc.refresh()
        buf = sc.getBuffer()
        texts = ocr.recognize(buf)
        for t in texts:
            if '委托' in t['text'] or '奖励' in t['text']:
                sx, sy = sc.toScreen(t['rect']['x'], t['rect']['y'])
                ic.click(sx, sy)
                sleep(1.0)
                break
        ic.click(sc.toScreen(960, 540))
        sleep(0.5)
    print("每日委托完成！")
```

---

## 6. 路径导航方案

### 6.1 概述

路径导航 = 小地图定位 + 路径数据 + 路径执行器（相机旋转 + WASD 移动）。

BetterGI 参考：
- 路径数据：`GameTask/AutoPathing/Model/` JSON 文件
- 路径执行器：`GameTask/AutoPathing/PathExecutor.cs`
- 相机旋转：`GameTask/AutoPathing/CameraRotateTask.cs`
- 陷阱脱出：`GameTask/AutoPathing/TrapEscaper.cs`
- 小地图定位：`GameTask/Common/Map/MiniMap/`（SIFT 特征匹配）

### 6.2 路径数据格式

复用 BetterGI JSON 路径文件，社区已有大量数据：

```json
{
  "name": "示例采集路径",
  "type": "collection",
  "waypoints": [
    {"position": {"x": 2345.6, "y": -789.0}, "action": "move", "type": "path"},
    {"position": {"x": 2350.0, "y": -795.0}, "action": "combat", "type": "path"},
    {"position": {"x": 2360.0, "y": -800.0}, "action": "collect", "type": "target"}
  ]
}
```

action 类型：`move` / `combat` / `collect` / `mine` / `teleport` / `npc`

### 6.3 路径执行器

```python
class PathExecutor:
    def __init__(self, ic, sc, tm, detector, ocr):
        self.ic = ic
        self.sc = sc
        self.tm = tm
        self.detector = detector
        self.ocr = ocr

    def execute(self, path_data):
        for wp in path_data['waypoints']:
            self._navigate_to(wp['position'])
            self._do_action(wp['action'])

    def _navigate_to(self, target_pos):
        while True:
            self.sc.refresh()
            buf = self.sc.getBuffer()
            current_pos = get_player_position(buf, self.tm)
            if current_pos is None:
                continue
            dx = target_pos['x'] - current_pos[0]
            dy = target_pos['y'] - current_pos[1]
            dist = (dx**2 + dy**2) ** 0.5
            if dist < 5.0:
                return True
            target_angle = math.atan2(dx, dy)
            self._rotate_camera_to(target_angle)
            self.ic.press(KeyCode_w)
            time.sleep(0.5)
            self._check_stuck(current_pos)

    def _rotate_camera_to(self, target_angle):
        # 参考 BetterGI CameraRotateTask
        pass

    def _check_stuck(self, last_pos):
        # 参考 BetterGI TrapEscaper
        pass

    def _do_action(self, action):
        if action == 'collect':
            self.ic.press(KeyCode_f)
            time.sleep(1.0)
        elif action == 'combat':
            pass  # 由 fighter 处理
        elif action == 'mine':
            self.ic.press(KeyCode_f)
            time.sleep(2.0)

    def load_path(self, name):
        import json
        path = os.path.join("paths", f"{name}.json")
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
```

---

## 7. 战斗方案

### 7.1 简化战斗

不做完整战斗 AI，用固定连招 + 技能CD检测：

```python
class SimpleFighter:
    def __init__(self, ic, sc, detector):
        self.ic = ic
        self.sc = sc
        self.detector = detector
        self.combo = [
            ('e', 500), ('q', 1000),
            ('click', 200), ('click', 200), ('click', 200), ('click', 200), ('click', 200),
        ]

    def fight_until_clear(self, timeout=120):
        start = time.time()
        while time.time() - start < timeout:
            self.sc.refresh()
            buf = self.sc.getBuffer()
            raw = buf.to_bytes()
            w, h = buf.getWidth(), buf.getHeight()
            dets = self.detector.detect(raw, w, h)
            if 'enemy_identify' not in dets and 'health_bar' not in dets:
                return True
            self._do_combo()
            time.sleep(0.3)
        return False

    def _do_combo(self):
        for action, delay in self.combo:
            if action == 'click':
                self.ic.click(self.sc.toScreen(960, 540))
            else:
                self.ic.press(action)
            time.sleep(delay / 1000.0)

    def switch_character(self, slot):
        key = [KeyCode_1, KeyCode_2, KeyCode_3, KeyCode_4][slot - 1]
        self.ic.press(key)
        time.sleep(0.5)
```

### 7.2 与 BetterGI AutoFight 的差距

| 能力 | BetterGI | 本项目 |
|------|----------|--------|
| 角色识别 | avatar_side.onnx | 按槽位切换 |
| 技能CD | 完整CD管理 | 仅检测Q是否可用 |
| 战斗脚本 | JSON 引擎 + 条件分支 | 固定连招 |
| 走位 | 动态走位 | 站桩输出 |
| 元素反应 | 脚本编排 | 不支持 |

首期用简化版完成秘境/地脉花，后续再增强。

---

## 8. 性能预估

| 环节 | GPU 模式 | CPU 模式 |
|------|---------|---------|
| IScreenCapture 截图 | ~5ms | ~5ms |
| buf.to_bytes() | ~1ms | ~1ms |
| YOLO 推理 (640x640) | ~15-30ms | ~80-150ms |
| 模板匹配定位 | ~10-20ms | ~10-20ms |
| OCR 文字识别 | ~20-40ms | ~50-100ms |
| 操作执行 | ~5ms | ~5ms |
| **单帧总耗时** | **~55-100ms** | **~165-280ms** |
| **等效帧率** | **10-18 fps** | **3.5-6 fps** |

优化：分帧调度 — 每1秒YOLO，每3秒OCR，每5秒定位。战斗中只跑YOLO，菜单中只跑OCR。

---

## 9. 需要补充的 avc SWIG 绑定

| 优先级 | 方法 | 说明 | 文件 |
|--------|------|------|------|
| 🔴 高 | `setHumanize(bool)` | 启用拟人化 | `BaseInputController.hpp` |
| 🔴 高 | `setJitterSeed(uint32_t)` | 固定随机种子 | 同上 |
| 🔴 高 | `setClickHoldMs(int32_t)` | 点击按住时长 | 同上 |
| 🔴 高 | `activeWindow(const char*)` | 按名称激活窗口 | `ShotOps.hpp` |
| 🟡 中 | `activeWindowByHwnd(void*)` | 按 HWND 激活窗口 | 同上 |
| 🟢 低 | IONNXSession | Python 直接用 onnxruntime | — |

---

## 10. 项目结构

```
D:\Work\genshin_daily\
  main.py                 # 入口：初始化 avc + onnxruntime，接收用户意图，调用 ScriptRunner
  config.py               # 配置 (窗口名、分辨率、模型路径、LLM API key)
  detector.py             # YOLO 检测器 (onnxruntime 封装)
  ai_planner.py           # AI 任务编排 (LLM 代码生成 + 校验 + 缓存 + 执行)
  path_executor.py        # 路径导航 (小地图定位 + 路径执行 + 陷阱脱出)
  fighter.py              # 简化战斗引擎 (固定连招 + 技能CD检测)
  utils.py                # 工具函数 (sleep/wait_until/find_nearest/click_center)
  paths/                  # BetterGI 格式的路径 JSON 文件
    collection/           # 采集路径
    mining/               # 挖矿路径
    boss/                 # Boss 路径
  cache/                  # AI 生成的脚本缓存
    tasks/                # 按 intent hash 缓存 .py 文件
  models/                 # ONNX 模型文件
    bgi_world.onnx
    bgi_mine.onnx
    bgi_fish.onnx
    bgi_tree.onnx
    avatar_side_classify_sim.onnx
    q_classify_sim.onnx
    avatar.onnx
    label.json
  templates/              # 模板匹配图片
    teleport_btn.png
    confirm_btn.png
    daily_quest_icon.png
    loading_icon.png
  map/                    # 地图数据
    teyvat_full_map.png
    map_features/
  logs/                   # 运行日志
  debug/                  # 调试截图
```

---

## 11. 开发计划

| 阶段 | 内容 | 天数 | 优先级 |
|------|------|------|--------|
| **一** | 基础链路：下载模型 + detector.py + avc截图→推理原型 | 2-3 | 🔴 |
| **二** | 操作闭环：补SWIG绑定 + 坐标转换 + 拟人化 | 2-3 | 🔴 |
| **三** | AI 编排：ai_planner.py + 代码生成 + 校验 + 执行 | 3-5 | 🔴 |
| **四** | 简单任务验证：AI 生成"领日常奖励"脚本并执行 | 2-3 | 🔴 |
| **五** | 定位+导航：小地图定位 + path_executor + BetterGI路径数据 | 5-7 | 🟡 |
| **六** | 采集/挖矿：AI 生成采集脚本 + 路径导航 | 3-5 | 🟡 |
| **七** | 简化战斗：fighter.py + 固定连招 + 技能CD | 3-5 | 🟢 |
| **八** | 完整日常：AI 生成一条龙脚本 + 秘境 + 树脂 | 3-5 | 🟢 |
| **九** | 稳定性：长时运行 + 边界case + 拟人化调参 | 3-5 | 🔵 |

### 里程碑

| 里程碑 | 阶段 | 可用功能 | 天数 |
|--------|------|---------|------|
| **M1: 闭环** | 一+二 | 截图→检测→操作 | 4-6 |
| **M2: AI 编排可用** | 三+四 | AI 生成脚本 + 执行简单任务 | 9-14 |
| **M3: 导航+采集** | 五+六 | 路径导航 + 自动采集/挖矿 | 17-26 |
| **M4: 完整日常** | 七+八 | 战斗 + 一条龙 | 23-36 |
| **M5: 稳定版** | 九 | 长时稳定 | 26-41 |

---

## 12. 风险与注意事项

### 12.1 反检测
- 必须启用拟人化：贝塞尔轨迹 + 随机延迟 + 自然点击
- 操作间隔加抖动：0.8-1.2秒随机
- 原神管理员权限运行，操作程序也必须管理员权限

### 12.2 模型兼容性
- BetterGI ONNX 模型用 YoloSharp 导出，输出格式与标准 YOLOv8 略有不同
- 类别标签在 `label.json` 中，需一并提取
- **解码 YOLO 输出最容易踩坑**，参考 BetterGI `Core/Recognition/ONNX/YOLO/Predictor.cs`

### 12.3 分辨率依赖
- 模型和坐标基于 1920x1080，原神必须 1920x1080 窗口模式

### 12.4 游戏更新
- 版本更新可能改变 UI 布局，模板匹配失效需更新
- BetterGI 社区会跟进，可同步更新模型和路径数据

### 12.5 路径导航风险
- 小地图定位精度直接影响导航质量
- 卡住检测和脱出是稳定性关键，参考 BetterGI TrapEscaper
- 地图数据 300M+，需从 BetterGI Release 获取

### 12.6 战斗系统风险
- 简化战斗（站桩连招）在高难度场景可能不够
- 后续增强战斗能力工作量显著增加

### 12.7 AI 编排风险
- LLM 生成的代码可能有逻辑错误，需充分校验
- LLM API 调用有延迟（1-3秒），只用于任务规划阶段，不用于实时操作
- API 费用：每次规划约 1000-3000 tokens，约 $0.01-0.05/次
- **缓解**：代码缓存 + 沙箱执行 + 超时保护 + 网络不可用时降级为内置脚本

### 12.8 法律与合规
- 仅供技术研究和学习使用
- 使用自动化工具可能违反游戏服务条款

---

## 13. BetterGI 源码参考索引

| 功能 | BetterGI 源码路径 | 说明 |
|------|-------------------|------|
| YOLO 推理 | `Core/Recognition/ONNX/BgiYoloPredictor.cs` | YOLO 检测封装 |
| YOLO 输出解码 | `Core/Recognition/ONNX/YOLO/Predictor.cs` | 输出格式解析（必读） |
| 模型注册 | `Core/Recognition/ONNX/BgiOnnxModel.cs` | 所有模型路径 |
| OCR | `Core/Recognition/OCR/` | PaddleOCR ONNX 版 |
| 模板匹配 | `Core/Recognition/OpenCv/` | OpenCvSharp |
| 截图 | `Fischless.GameCapture/` | BitBlt/DwmSharedSurface |
| 操作 | `Fischless.WindowsInput/` | PostMessage/SendInput |
| 路径执行 | `GameTask/AutoPathing/PathExecutor.cs` | 路径导航核心 |
| 相机旋转 | `GameTask/AutoPathing/CameraRotateTask.cs` | 视角旋转 |
| 陷阱脱出 | `GameTask/AutoPathing/TrapEscaper.cs` | 卡住检测与脱出 |
| 小地图定位 | `GameTask/Common/Map/MiniMap/` | SIFT 特征匹配 |
| 相机朝向 | `GameTask/Common/Map/Camera/CameraOrientation.cs` | 朝向检测 |
| 自动战斗 | `GameTask/AutoFight/` | 完整战斗系统 |
| 战斗脚本 | `GameTask/AutoFight/Script/` | JSON 战斗脚本引擎 |
| 自动秘境 | `GameTask/AutoDomain/AutoDomainTask.cs` | 秘境自动化 |
| 自动钓鱼 | `GameTask/AutoFishing/` | 钓鱼检测与操作 |
| 自动拾取 | `GameTask/AutoPick/` | 拾取检测与按F |
| 自动剧情 | `GameTask/AutoSkip/` | 剧情跳过 |
| 一条龙 | `GameTask/Common/Job/` | 组合任务编排 |
