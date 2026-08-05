#!/usr/bin/env python3
"""
fetch_resources.py —— avc_genshin 大模型与全地图数据获取脚本

为什么需要本脚本：BGI 的 ONNX 模型与全地图 SIFT 特征数据体量大（几十~几百 MB），
不进 git（.gitignore 已忽略 resources/models、resources/map、resources/ocr）。
BGI 也不单独发布这些资源——全部打包在 BetterGI_vX.7z release 包里（约 424MB）。
本脚本下载该包一次（缓存到 cache/bgi_release/），按 resources_manifest.json 从中
**只提取需要的文件**到 resources/，避免把整个 424MB 解压铺开。

三层获取优先级（对每个资源）：
  1. 已就绪（verify_files 存在）→ 跳过
  2. --bgi-root 指向本地 BGI **安装/解压目录**（含 Assets/）→ 直接 copy（免下载）
  3. 否则 → 从缓存的 7z 包提取（首次会触发下载）

用法：
  python script/fetch_resources.py --list                       # 列出全部资源及就绪状态
  python script/fetch_resources.py --check                      # 仅检查缺失，不下载
  python script/fetch_resources.py --phase C                    # 获取阶段 C（战斗）所需
  python script/fetch_resources.py --select avatar_side,q_classify
  python script/fetch_resources.py --all                        # 获取全部
  python script/fetch_resources.py --all --bgi-root D:/BGI      # 从本地 BGI 安装目录复制
  python script/fetch_resources.py --all --dry-run              # 只打印将做什么
  python script/fetch_resources.py --all --force                # 忽略已存在，重新获取

选项：
  --manifest PATH    清单路径（默认 script/resources_manifest.json）
  --root PATH        项目根（默认按脚本位置推算）
  --bgi-root PATH    本地 BGI 安装/解压目录（含 Assets/；有则直接 copy 免下载）
                     默认读环境变量 BGI_ROOT（与 framework/resources.py 一致）
  --phase P          按阶段过滤（A/B/C/D）
  --category CAT     按类别过滤（model/map）
  --no-color         关闭彩色输出
  --dry-run          只打印将做什么，不实际执行
  --force            忽略已存在，强制重新获取

解压后端：优先 py7zr（pip install py7zr；纯 Python，可只提取指定文件，省空间）；
         回退系统 7z（PATH 中的 7z，或 C:\\Program Files\\7-Zip\\7z.exe）；都无则报错。
退出码：0 全部成功/仅查询，1 有失败，2 参数错误。
仅依赖 Python 标准库（+可选 py7zr）。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
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
    """script/fetch_resources.py 的上一级 = 项目根。"""
    return Path(__file__).resolve().parent.parent


def load_manifest(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def item_dest_dir(item, manifest, root):
    """item 目标目录 = root / categories[category]。"""
    return root / manifest["categories"][item["category"]]


def verify_ok(item, root):
    """verify_files 全存在则就绪。"""
    return all((root / v).exists() for v in item.get("verify_files", []))


def status_text(item, root, c):
    if item.get("in_release") is False:
        return f"{c.DIM}未收录{c.W}"
    return f"{c.G}就绪{c.W}" if verify_ok(item, root) else f"{c.Y}缺失{c.W}"


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

    req = urllib.request.Request(url, headers={"User-Agent": "fetch_resources/1.0"})
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


# ─────────────────────────── 7z 后端 ───────────────────────────
def pick_backend(c, root=None):
    """返回 ('7z', exe_path) | ('py7zr', None) | None。
    优先系统 7z:原生支持 BCJ2 等 py7zr 不支持的方法(BGI 的 7z 包用了 BCJ2 滤波器,
    py7zr 会抛 UnsupportedCompressionMethodError)。
    其次项目内便携 7zr.exe/7za.exe(cache/ 或 script/tools/, 免装系统软件);
    最后 py7zr 回退(无 7z 时, 但可能遇 BCJ2 失败)。"""
    for name in ("7z", "7z.exe", "7za", "7zr"):
        p = shutil.which(name)
        if p:
            return ("7z", p)
    for cand in (r"C:\Program Files\7-Zip\7z.exe",
                 r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if os.path.isfile(cand):
            return ("7z", cand)
    r = Path(root) if root else find_project_root()
    for cand in (r / "cache" / "7zr.exe", r / "cache" / "7za.exe",
                 r / "script" / "tools" / "7z.exe"):
        if cand.is_file():
            return ("7z", str(cand))
    try:
        import py7zr  # noqa: F401
        return ("py7zr", None)
    except ImportError:
        pass
    return None


def list_archive_names(archive, backend, c):
    """返回包内所有条目相对路径列表（/ 分隔）。"""
    kind, _ = backend
    if kind == "py7zr":
        import py7zr
        with py7zr.SevenZipFile(archive, mode="r") as z:
            return z.getnames()
    # 系统 7z: l -slt 解析 "Path ="
    exe = backend[1]
    r = subprocess.run([exe, "l", "-slt", str(archive)],
                       capture_output=True, text=True)
    names = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("Path ="):
            v = line[len("Path ="):].strip()
            if v:
                names.append(v.replace("\\", "/"))
    return names


def resolve_targets(names, bgi_path, is_dir):
    """从包内条目名解析出 bgi_path 对应的实际条目（处理顶层前缀）。
    返回 (targets: list[str], located: str)。
    located 对单文件=产出条目名；对目录=目录在包内完整路径（带前缀）。"""
    bp = bgi_path.replace("\\", "/").rstrip("/")
    if is_dir:
        matched = []
        for n in names:
            nn = n.replace("\\", "/").rstrip("/")
            if nn == bp or nn.endswith("/" + bp) or \
               nn.startswith(bp + "/") or ("/" + bp + "/") in nn:
                if not nn.endswith("/"):  # 跳过纯目录条目
                    matched.append(nn)
        if not matched:
            return [], None
        # 目录在包内完整路径 = 首个匹配里 bp 之前（含 bp）的部分
        marker = "/" + bp + "/"
        first = matched[0]
        i = first.find(marker)
        located = (first[:i + len(marker) - 1]) if i >= 0 else bp
        return matched, located
    else:
        for n in names:
            nn = n.replace("\\", "/").rstrip("/")
            if nn == bp or nn.endswith("/" + bp):
                return [nn], nn
        return [], None


def extract_entries(archive, item, backend, tmp, c):
    """提取 item.bgi_path 到 tmp（保留包内结构）。返回 bool。"""
    kind, _ = backend
    bp = item["bgi_path"].replace("\\", "/").rstrip("/")
    is_dir = item.get("filename") is None
    if kind == "py7zr":
        import py7zr
        names = list_archive_names(archive, backend, c)
        targets, _located = resolve_targets(names, bp, is_dir)
        if not targets:
            return False
        with py7zr.SevenZipFile(archive, mode="r") as z:
            z.extract(path=str(tmp), targets=targets)
        return True
    # 系统 7z: 通配提取（同时匹配有/无顶层前缀两种）
    exe = backend[1]
    suffix = "/*" if is_dir else ""
    patterns = [bp + suffix, "*/" + bp + suffix]
    cmd = [exe, "x", str(archive), f"-o{tmp}", "-y", "-bso0", "-bse2"] + patterns
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            _e(f"  {c.R}7z 错误: {r.stderr.strip()}{c.W}")
        return r.returncode == 0
    except Exception as e:
        _e(f"  {c.R}7z 执行异常: {e}{c.W}")
        return False


def locate_extracted(tmp, item):
    """提取后在 tmp 内定位产出（统一逻辑，py7zr/7z 通用）。返回 Path 或 None。"""
    bp = item["bgi_path"].replace("\\", "/").rstrip("/")
    if item.get("filename") is None:  # 目录：找 endswith bp 的目录
        for p in tmp.rglob("*"):
            if p.is_dir():
                rp = str(p.relative_to(tmp)).replace("\\", "/").rstrip("/")
                if rp == bp or rp.endswith("/" + bp):
                    return p
        return None
    # 单文件：按 filename 找
    fn = item["filename"]
    for p in tmp.rglob(fn):
        if p.is_file():
            return p
    return None


# ─────────────────────────── 单项处理 ───────────────────────────
def copy_from_bgi(item, bgi_root, manifest, root, c):
    """从本地 BGI 安装目录 copy。成功返回 True。"""
    src = Path(bgi_root) / item["bgi_path"]
    if not src.exists():
        return False, f"BGI_ROOT 下未找到 {item['bgi_path']}"
    dest_dir = item_dest_dir(item, manifest, root)
    if item.get("filename") is None:  # 目录
        target = dest_dir / item["bgi_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, target, dirs_exist_ok=True)
    else:
        target = dest_dir / item["filename"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    return True, "从 BGI_ROOT 复制"


def ensure_archive(manifest, root, c):
    """确保 BGI 7z 已下载到缓存。返回 Path 或 None。"""
    rel = manifest["bgi_release"]
    cache = root / rel["cache_dir"]
    archive = cache / rel["archive"]
    if archive.exists() and archive.stat().st_size > 1024 * 1024:
        return archive
    cache.mkdir(parents=True, exist_ok=True)
    _e(f"{c.C}下载 BGI release 包（~{rel['size_mb']}MB，仅一次，缓存到 {cache}）...{c.W}")
    if not download_file(rel["url"], archive, c):
        return None
    return archive


def process_item(item, root, manifest, bgi_root, force, dry, c):
    """返回 dict(id, ok, action, msg)。"""
    iid = item["id"]
    _e(f"\n{c.BOLD}[{iid}] {item['name']}{c.W}  "
       f"(阶段{item.get('phase', '?')} · {item['category']})", )
    if item.get("in_release") is False:
        _e(f"  -> {c.DIM}SKIP release 包未收录此文件（见 note）{c.W}")
        return {"id": iid, "ok": True, "action": "skip", "msg": "未收录"}
    if verify_ok(item, root) and not force:
        _e(f"  -> {c.G}OK{c.W} 已就绪，跳过")
        return {"id": iid, "ok": True, "action": "skip", "msg": "已就绪"}

    if dry:
        if bgi_root and (Path(bgi_root) / item["bgi_path"]).exists():
            msg = f"将复制 BGI_ROOT/{item['bgi_path']}"
        else:
            msg = f"将从 7z 提取 {item['bgi_path']}"
        _e(f"  -> {c.DIM}DRY {msg}{c.W}")
        return {"id": iid, "ok": True, "action": "dry", "msg": msg}

    # 1. 本地 BGI_ROOT 复制
    if bgi_root:
        ok, msg = copy_from_bgi(item, bgi_root, manifest, root, c)
        if ok:
            done = verify_ok(item, root)
            tag = f"{c.G}OK{c.W}" if done else f"{c.Y}WARN{c.W}"
            _e(f"  -> {tag} {msg}" + ("" if done else "（但 verify 未通过）"))
            return {"id": iid, "ok": done, "action": "copy", "msg": msg}

    # 2. 从 7z 提取
    archive = ensure_archive(manifest, root, c)
    if archive is None:
        _e(f"  -> {c.R}FAIL 无 7z 包（下载失败）{c.W}")
        return {"id": iid, "ok": False, "action": "fetch", "msg": "下载失败"}
    backend = pick_backend(c, root)
    if backend is None:
        _e(f"  -> {c.R}FAIL 无解压后端：pip install py7zr，或安装 7z 到 PATH{c.W}")
        return {"id": iid, "ok": False, "action": "fetch", "msg": "无解压后端"}

    tmp = Path(tempfile.mkdtemp(prefix="fetch7z_", dir=str(root / "cache")))
    try:
        _e(f"  提取 {item['bgi_path']} ...")
        if not extract_entries(archive, item, backend, tmp, c):
            return {"id": iid, "ok": False, "action": "fetch", "msg": "提取失败"}
        located = locate_extracted(tmp, item)
        if located is None:
            _e(f"  {c.R}提取后未在包内定位到 {item['bgi_path']}{c.W}")
            return {"id": iid, "ok": False, "action": "fetch", "msg": "包内未找到目标"}
        dest_dir = item_dest_dir(item, manifest, root)
        if item.get("filename") is None:
            final = dest_dir / item["bgi_path"]
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(located, final, dirs_exist_ok=True)
        else:
            final = dest_dir / item["filename"]
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(located, final)
        done = verify_ok(item, root)
        tag = f"{c.G}OK{c.W}" if done else f"{c.Y}WARN{c.W}"
        _e(f"  -> {tag} 提取到 {final.relative_to(root)}")
        return {"id": iid, "ok": done, "action": "fetch",
                "msg": "提取完成" if done else "提取后 verify 未通过"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─────────────────────────── 过滤 / 列表 ───────────────────────────
def filter_items(items, phase=None, category=None):
    out = items
    if phase:
        out = [it for it in out if it.get("phase") == phase]
    if category:
        out = [it for it in out if it.get("category") == category]
    return out


def print_list(items, root, c):
    _e(f"{c.BOLD}#   阶段  类别    id         状态   名称{c.W}")
    _e("-" * 78)
    for i, it in enumerate(items, 1):
        _e(f"{i:>2}  {it.get('phase', '?'):<4} {it['category']:<8} "
            f"{it['id']:<10} {status_text(it, root, c)}  {it['name']}")
    _e(f"\n共 {len(items)} 项，就绪 {sum(1 for it in items if verify_ok(it, root))}。")


# ─────────────────────────── main ───────────────────────────
def main(argv=None):
    p = argparse.ArgumentParser(
        description="avc_genshin 大模型与全地图数据获取（BGI release 包）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--manifest", default=None, help="清单路径")
    p.add_argument("--root", default=None, help="项目根（默认按脚本位置推算）")
    p.add_argument("--bgi-root", default=None,
                   help="本地 BGI 安装/解压目录（含 Assets/；默认读 $BGI_ROOT）")
    p.add_argument("--phase", default=None, help="按阶段过滤 A/B/C/D")
    p.add_argument("--category", default=None, choices=["model", "map"], help="按类别过滤")
    p.add_argument("--select", default=None, help="逗号分隔的 id 列表")
    p.add_argument("--all", action="store_true", help="获取全部（按 phase/category 过滤后）")
    p.add_argument("--check", action="store_true", help="仅检查就绪状态，不下载")
    p.add_argument("--list", "-l", action="store_true", help="列出全部资源及就绪状态")
    p.add_argument("--force", action="store_true", help="忽略已存在，强制重新获取")
    p.add_argument("--dry-run", action="store_true", help="只打印将做什么")
    p.add_argument("--no-color", action="store_true", help="关闭彩色输出")
    args = p.parse_args(argv)

    c = C(not args.no_color and sys.stderr.isatty())
    root = Path(args.root).resolve() if args.root else find_project_root()
    manifest_path = (Path(args.manifest) if args.manifest
                     else Path(__file__).resolve().parent / "resources_manifest.json")
    if not manifest_path.exists():
        _e(f"{c.R}找不到清单: {manifest_path}{c.W}")
        return 2
    manifest = load_manifest(manifest_path)
    items = filter_items(manifest["items"], args.phase, args.category)
    bgi_root = args.bgi_root or os.getenv("BGI_ROOT", "").strip() or None
    if bgi_root:
        bgi_root = bgi_root.replace("\\", "/")

    # 仅查询
    if args.list or args.check:
        if not items:
            _e(f"{c.Y}没有匹配的项。{c.W}")
            return 0
        _e(f"{c.BOLD}项目根: {root}  |  清单: {manifest_path}{c.W}")
        if bgi_root:
            _e(f"{c.C}BGI_ROOT: {bgi_root}{c.W}")
        print_list(items, root, c)
        missing = [it["id"] for it in items
                   if not verify_ok(it, root) and it.get("in_release") is not False]
        if args.check and missing:
            _e(f"\n{c.Y}缺失 {len(missing)} 项: {', '.join(missing)}{c.W}")
            _e(f"获取: python {Path(sys.argv[0]).name} --select {','.join(missing)}")
        return 0

    # 确定要获取的项
    if args.select:
        wanted = {s.strip() for s in args.select.split(",") if s.strip()}
        chosen = [it for it in manifest["items"] if it["id"] in wanted]
        unknown = wanted - {it["id"] for it in chosen}
        if unknown:
            _e(f"{c.R}未知 id: {', '.join(unknown)}{c.W}")
            return 2
    elif args.all or args.phase or args.category:
        # --phase / --category 隐含“对这些项执行”（items 已过滤）
        chosen = items
    else:
        _e(f"{c.Y}未指定动作。用 --list 查看, --check 检查缺失, "
            f"--phase C / --select id / --all 获取。{c.W}")
        return 2

    if not chosen:
        _e(f"{c.Y}没有匹配的项。{c.W}")
        return 0

    _e(f"{c.BOLD}项目根: {root}  |  清单: {manifest_path}{c.W}")
    if bgi_root:
        br = Path(bgi_root)
        has_assets = (br / "Assets").is_dir()
        tag = f"{c.G}有效（含 Assets/）{c.W}" if has_assets else f"{c.Y}无 Assets/（将走下载）{c.W}"
        _e(f"BGI_ROOT: {bgi_root}  {tag}")
    if not args.dry_run:
        be = pick_backend(c, root)
        if be is None and not bgi_root:
            _e(f"{c.R}无解压后端且无 BGI_ROOT：pip install py7zr，或安装 7z 到 PATH{c.W}")
            return 1
        if be:
            _e(f"{c.DIM}解压后端: {be[0]}{c.W}")

    results = [process_item(it, root, manifest, bgi_root, args.force, args.dry_run, c)
               for it in chosen]
    ok_cnt = sum(1 for r in results if r["ok"])
    _e(f"\n{c.BOLD}汇总: {ok_cnt}/{len(results)} 成功{c.W}")
    return 0 if ok_cnt == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
