#!/usr/bin/env python3
"""
fetch_test_images.py —— 网上公开原神测试图获取脚本

为什么需要本脚本：avc_genshin 的检测器（血条/场景/OCR/YOLO）需要**真实游戏画面**
验证，但现有合成测试只证机制、不证真实像素；实机截图（tests/fixtures/screens/）
要自己截、没游戏就 skip。本脚本从网上收集公开的原神游戏图到 tests/fixtures/web/，
让没游戏时也能跑真实画面测试（tests/test_web_screens.py）。

图源：清单 script/test_images_manifest.json 里每个 item 的 url（GitHub 直链等，
稳定可重复下载）。图不进 git（.gitignore 已忽略 tests/fixtures/web/）。

**不限定分辨率**：本脚本只下载、保留原图任意尺寸；分辨率归一化由**测试加载侧**用
avc 的 ``IImageBuffer.resize(1920,1080)`` 完成（avc/image.py 原生 API）。项目所有
坐标基于 1080p，非 1080p 图加载后 resize 到 1080p 再喂检测器，坐标即对齐。

用法：
  python script/fetch_test_images.py --list                       # 列出全部图及就绪状态
  python script/fetch_test_images.py --check                      # 仅检查缺失，不下载
  python script/fetch_test_images.py --select fight_hilichurls    # 下载指定 id
  python script/fetch_test_images.py --all                        # 下载全部
  python script/fetch_test_images.py --all --dry-run              # 只打印将做什么
  python script/fetch_test_images.py --all --force                # 忽略已存在，重新获取

选项：
  --manifest PATH    清单路径（默认 script/test_images_manifest.json）
  --root PATH        项目根（默认按脚本位置推算）
  --select IDS       逗号分隔的 id 列表
  --all              下载全部
  --check            仅检查就绪状态，不下载
  --list, -l         列出全部图及就绪状态
  --force            忽略已存在，强制重新获取
  --dry-run          只打印将做什么
  --no-color         关闭彩色输出

退出码：0 全部成功/仅查询，1 有失败，2 参数错误。
仅依赖 Python 标准库（urllib）。
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path


# ─────────────────────────── 颜色 ───────────────────────────
class C:
    def __init__(self, enabled):
        self.e = enabled

    def __getattr__(self, name):
        return "" if not self.e else _CODES.get(name, "")


_CODES = {
    "R": "\033[31m", "G": "\033[32m", "Y": "\033[33m",
    "B": "\033[34m", "C": "\033[36m", "M": "\033[35m",
    "BOLD": "\033[1m", "DIM": "\033[2m", "W": "\033[0m",
}


def _e(msg):
    print(msg, file=sys.stderr)


# ─────────────────────────── 路径 / 清单 ───────────────────────────
def find_project_root():
    """script/fetch_test_images.py 的上一级 = 项目根。"""
    return Path(__file__).resolve().parent.parent


def load_manifest(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def dest_dir_of(manifest, root):
    return root / manifest.get("dest_dir", "tests/fixtures/web")


def dest_of(item, manifest, root):
    return dest_dir_of(manifest, root) / item["filename"]


def verify_ok(item, manifest, root):
    return dest_of(item, manifest, root).exists()


def status_text(item, manifest, root, c):
    return f"{c.G}就绪{c.W}" if verify_ok(item, manifest, root) else f"{c.Y}缺失{c.W}"


# ─────────────────────────── 下载 ───────────────────────────
def download_file(url, dest_file, c, retries=3):
    """带进度下载到 dest_file（.part 原子替换）。失败返回 False。进度写 stderr。"""
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_file.with_suffix(dest_file.suffix + ".part")

    def reporthook(read_bytes, total_size):
        if total_size > 0:
            pct = min(100, read_bytes * 100 / total_size)
            bar_len = 30
            filled = int(bar_len * read_bytes / total_size)
            bar = "=" * filled + "-" * (bar_len - filled)
            sys.stderr.write(
                f"\r  {c.DIM}[{bar}]{c.W} {pct:5.1f}% "
                f"({read_bytes / 1048576:.1f}/{total_size / 1048576:.1f}MB)"
            )
        else:
            sys.stderr.write(f"\r  {read_bytes / 1048576:.1f}MB")
        sys.stderr.flush()

    req = urllib.request.Request(url, headers={"User-Agent": "fetch_test_images/1.0"})
    for attempt in range(1, retries + 1):
        try:
            read_bytes = 0
            with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as out:
                total = int(resp.getheader("Content-Length") or 0)
                while True:
                    chunk = resp.read(1024 * 64)
                    if not chunk:
                        break
                    out.write(chunk)
                    read_bytes += len(chunk)
                    reporthook(read_bytes, total)
            sys.stderr.write("\n")
            os.replace(tmp, dest_file)
            return True
        except Exception as e:
            sys.stderr.write("\n")
            _e(f"  {c.R}下载失败（第 {attempt}/{retries} 次）: {e}{c.W}")
            if attempt < retries:
                time.sleep(2 * attempt)
    if tmp.exists():
        tmp.unlink(missing_ok=True)
    return False


# ─────────────────────────── 单项处理 ───────────────────────────
def process_item(item, root, manifest, force, dry, c):
    """返回 dict(id, ok, action, msg)。只下载，保留原图（归一化在测试侧）。"""
    iid = item["id"]
    _e(f"\n{c.BOLD}[{iid}] {item['name']}{c.W}  (场景 {item.get('scene', '?')})")
    dest = dest_of(item, manifest, root)
    if verify_ok(item, manifest, root) and not force:
        _e(f"  -> {c.G}OK{c.W} 已就绪，跳过")
        return {"id": iid, "ok": True, "action": "skip", "msg": "已就绪"}

    if dry:
        _e(f"  -> {c.DIM}DRY 将下载 {item['url']}{c.W}")
        return {"id": iid, "ok": True, "action": "dry", "msg": "dry-run"}

    if not download_file(item["url"], dest, c):
        return {"id": iid, "ok": False, "action": "fetch", "msg": "下载失败"}
    done = verify_ok(item, manifest, root)
    tag = f"{c.G}OK{c.W}" if done else f"{c.R}FAIL{c.W}"
    _e(f"  -> {tag} {dest.relative_to(root)}")
    return {"id": iid, "ok": done, "action": "fetch",
            "msg": "下载完成" if done else "写入失败"}


# ─────────────────────────── 列表 ───────────────────────────
def print_list(items, manifest, root, c):
    _e(f"{c.BOLD}#   场景       id                   状态   名称{c.W}")
    _e("-" * 78)
    for i, it in enumerate(items, 1):
        _e(f"{i:>2}  {it.get('scene', '?'):<10} {it['id']:<20} "
            f"{status_text(it, manifest, root, c)}  {it['name']}")
    _e(f"\n共 {len(items)} 项，就绪 {sum(1 for it in items if verify_ok(it, manifest, root))}。")


# ─────────────────────────── main ───────────────────────────
def main(argv=None):
    p = argparse.ArgumentParser(
        description="avc_genshin 网上原神测试图获取（保留原图，测试侧归一化）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--manifest", default=None, help="清单路径")
    p.add_argument("--root", default=None, help="项目根（默认按脚本位置推算）")
    p.add_argument("--select", default=None, help="逗号分隔的 id 列表")
    p.add_argument("--all", action="store_true", help="下载全部")
    p.add_argument("--check", action="store_true", help="仅检查就绪状态，不下载")
    p.add_argument("--list", "-l", action="store_true", help="列出全部图及就绪状态")
    p.add_argument("--force", action="store_true", help="忽略已存在，强制重新获取")
    p.add_argument("--dry-run", action="store_true", help="只打印将做什么")
    p.add_argument("--no-color", action="store_true", help="关闭彩色输出")
    args = p.parse_args(argv)

    c = C(not args.no_color and sys.stderr.isatty())
    root = Path(args.root).resolve() if args.root else find_project_root()
    manifest_path = (Path(args.manifest) if args.manifest
                     else Path(__file__).resolve().parent / "test_images_manifest.json")
    if not manifest_path.exists():
        _e(f"{c.R}找不到清单: {manifest_path}{c.W}")
        return 2
    manifest = load_manifest(manifest_path)
    items = manifest["items"]

    # 仅查询
    if args.list or args.check:
        if not items:
            _e(f"{c.Y}清单为空。{c.W}")
            return 0
        _e(f"{c.BOLD}项目根: {root}  |  清单: {manifest_path}{c.W}")
        print_list(items, manifest, root, c)
        missing = [it["id"] for it in items if not verify_ok(it, manifest, root)]
        if args.check and missing:
            _e(f"\n{c.Y}缺失 {len(missing)} 项: {', '.join(missing)}{c.W}")
            _e(f"获取: python {Path(sys.argv[0]).name} --select {','.join(missing)}")
        return 0

    # 确定要获取的项
    if args.select:
        wanted = {s.strip() for s in args.select.split(",") if s.strip()}
        chosen = [it for it in items if it["id"] in wanted]
        unknown = wanted - {it["id"] for it in chosen}
        if unknown:
            _e(f"{c.R}未知 id: {', '.join(unknown)}{c.W}")
            return 2
    elif args.all:
        chosen = items
    else:
        _e(f"{c.Y}未指定动作。用 --list 查看, --check 检查缺失, "
            f"--select id / --all 获取。{c.W}")
        return 2

    if not chosen:
        _e(f"{c.Y}没有匹配的项。{c.W}")
        return 0

    _e(f"{c.BOLD}项目根: {root}  |  清单: {manifest_path}{c.W}")
    results = [process_item(it, root, manifest, args.force, args.dry_run, c)
               for it in chosen]
    ok_cnt = sum(1 for r in results if r["ok"])
    _e(f"\n{c.BOLD}汇总: {ok_cnt}/{len(results)} 成功{c.W}")
    return 0 if ok_cnt == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
