# resources/ —— 游戏资源（数据，非代码）

| 子目录 | 内容 | git | 来源 |
|---|---|---|---|
| `templates/` | 模板图（1080p 基准，按功能分子目录） | ✅ | 自截 / BetterGI |
| `paths/` | BetterGI 路径 JSON | ✅ | BGI 社区 |
| `models/` | ONNX 模型（world/avatar/...） | ❌ 大文件 | BGI Release |
| `ocr/` | OCR 模型/字典 | ❌ | BGI / avc 内置 |
| `map/` | 全地图数据（300M+） | ❌ | BGI Release |

访问统一走 `framework.resources.res`（**禁止硬编码相对路径**）：

```python
from framework.resources import res
res.template("daily/katherine.png")   # resources/templates/...
res.model("bgi_world.onnx")           # resources/models/...，缺失则回退 BGI_ROOT
```

> 大文件（models/map/ocr）已在 `.gitignore`。两种获取方式（`docs/design/03 §5.2`）：
> - **脚本获取**：`python script/fetch_resources.py --list`（清单 `script/resources_manifest.json`，
>   从 BGI release 7z 按需提取，下载一次缓存到 `cache/bgi_release/`）。
> - **本地复用**：设 `BGI_ROOT` 指向本地 BetterGI **安装/解压目录**（含 `Assets/`），
>   `python script/fetch_resources.py --all --bgi-root <路径>` 直接 copy 免下载。
