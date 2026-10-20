#!/usr/bin/env python3
r"""push_project.py —— 把一个项目目录推到 GitHub 仓库（`yzz103/yzz-dsh` 的某个顶层文件夹）。

用户约定（2026-10-20 修订，取代原来的「以后的项目都上传到 github 仓库」）：

  1. **新项目不自动发布** —— 用户明确说「提交 / 上传」时才发布，发布时加 `--new`；
  2. **已发布的项目更新后自动提交推送** —— 仓库里已有同名顶层文件夹就算已发布，
     脚本自己判定并直接推，不再二次确认、不用再问用户。

本脚本就是这两条的可执行版本：一条命令完成「判定发布状态 → 体检 → 密钥扫描 → 推送 → 回执」。

用法：
    python push_project.py <项目目录> [选项]
    python push_project.py --status        # 只列远端已发布的项目（判定依据），不推送

    <项目目录>            要推的本地目录（如 ./my-tool 或 2026-10-21_某工具）
    选项：
      --status            列出远端仓库的顶层项目文件夹（哪些算「已发布」）
      --new               首次发布一个新项目（**必须用户明确要求才加**，否则退出码 5）
      --name NAME         仓库内的顶层文件夹名，默认取目录名（去掉日期前缀）
      --repo OWNER/REPO   默认 yzz103/yzz-dsh（可用环境变量 GH_REPO 覆盖）
      --message MSG       提交信息；新项目默认 "feat(<name>): 首次发布"，
                          已发布项目默认 "chore(<name>): 更新（N 个文件）"
      --branch BRANCH     默认仓库默认分支
      --dry-run           只看计划：要推哪些文件、多大、缺什么骨架，不联网不推送
      --allow-missing     骨架文件缺失也照推（默认拦下，退出码 1）
      --allow-local-paths 带本机专属路径（D:/APP 之类）也照推（默认拦，因为别人克隆后用不了）
      --force             允许覆盖已有分支 ref
      --yes               兼容旧用法，现在无作用（已发布项目的更新本来就自动放行）

退出码：0 成功 / 1 参数或体检失败 / 2 密钥扫描命中 / 3 推送失败 / 4 含本机专属路径
        / 5 新项目却没加 --new（未获用户授权发布）

骨架检查（用户 m02767「按通用框架提交」定的标准）：
    README.md、CHANGELOG.md、pyproject.toml、tests/（或同名的 test_*.py）

推之前一定会跑的密钥扫描：sk- / adm- / gho_ / ghp_ / Bearer <长串> / AKIA 等。
命中即中止，并把「文件:行号」打出来（不打内容，免得又把密钥写进日志）。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:  # 输出被管道接走时 Windows 默认 cp936，DSH 按 UTF-8 读会乱码
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001  老版本解释器没有 reconfigure
    pass

import mcp_github as core  # noqa: E402  （同目录，纯标准库）

DEFAULT_REPO = "yzz103/yzz-dsh"

# 不进仓库的东西（与仓库根的 .gitignore 保持一致，脚本自己也兜一层）
SKIP_DIRS = {
    "__pycache__", ".git", ".venv", "venv", "_build", "dist", "build",
    "node_modules", ".idea", ".vscode", "gallery", "output", "_tmp", "_smoke",
}
SKIP_NAMES = {
    "config.json", "stats.json", "serve.log", "serve.err",
    ".credentials.yaml", "Thumbs.db", "desktop.ini",
}
SKIP_SUFFIXES = {".pyc", ".pyo", ".zip", ".spec", ".log", ".exe", ".dll", ".pyd"}

# 骨架（缺了只警告）
SKELETON = ["README.md", "CHANGELOG.md", "pyproject.toml"]

SECRET_PATTERNS = [
    ("Agnes Key", re.compile(r"sk-[A-Za-z0-9_-]{16,}")),
    ("管理台 token", re.compile(r"adm-[a-f0-9]{8,}")),
    ("GitHub OAuth/App 令牌", re.compile(r"gh[oprsu]_[A-Za-z0-9]{20,}")),
    ("AWS Access Key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Bearer 长串", re.compile(r"Bearer\s+[A-Za-z0-9_-]{20,}")),
]

# 默认阻断：本机专属路径换台机器就跑不通（--allow-local-paths 放行）
HOST_PATH = re.compile(r"[A-Za-z]:\\\\?(?:APP|DSH|Program Files)[\\/]")

TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".yml", ".yaml", ".json", ".toml", ".ini", ".cfg",
    ".bat", ".cmd", ".ps1", ".sh", ".js", ".mjs", ".ts", ".tsx", ".jsx",
    ".html", ".css", ".gitignore", ".example", "",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def with_retry(fn, *args, tries: int = 3, **kwargs):
    """本机到 api.github.com 偶发 SSL 断连（实测过两次），网络类错误自动重试。"""
    for attempt in range(1, tries + 1):
        try:
            return fn(*args, **kwargs)
        except core.GitHubError as exc:
            if exc.status != 0 or attempt == tries:
                raise
            log(f"[!] 网络断开（{exc}），{attempt * 2}s 后重试…")
            time.sleep(attempt * 2)


def project_name_from_dir(dirname: str) -> str:
    """`2026-10-21_某工具` → `某工具`；其它情况原样用。"""
    m = re.match(r"^\d{4}-\d{2}-\d{2}[_-](.+)$", dirname)
    return m.group(1) if m else dirname


def is_text(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES or path.name in {".gitignore", "LICENSE", "README"}


def collect(root: Path) -> list[Path]:
    """按跳过规则收集要推的文件（保持稳定顺序）。"""
    picked: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("_bak"))
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if name in SKIP_NAMES or name.startswith("_bak"):
                continue
            if p.suffix.lower() in SKIP_SUFFIXES:
                continue
            if p.stem.endswith("_使用说明") and p.suffix == ".zip":
                continue
            picked.append(p)
    return picked


def scan_secrets(files: list[Path]) -> list[tuple[Path, int, str]]:
    hits: list[tuple[Path, int, str]] = []
    for p in files:
        if not is_text(p):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for no, line in enumerate(text.splitlines(), 1):
            if len(line) > 400:          # 压缩过的一行大文件（如 min.js）跳过，避免误报
                continue
            for label, pat in SECRET_PATTERNS:
                if pat.search(line):
                    hits.append((p, no, label))
                    break
    return hits


def scan_host_paths(files: list[Path]) -> list[tuple[Path, int, str]]:
    hits = []
    for p in files:
        if not is_text(p):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for no, line in enumerate(text.splitlines(), 1):
            m = HOST_PATH.search(line)
            if m:
                hits.append((p, no, m.group(0)))
                break
    return hits


def check_skeleton(root: Path) -> list[str]:
    missing = [n for n in SKELETON if not (root / n).is_file()]
    has_tests = any((root / "tests").glob("test_*.py")) or any(root.glob("test_*.py"))
    if not has_tests:
        missing.append("tests/ 或 test_*.py")
    return missing


def remote_exists(gh, repo: str, path: str) -> bool:
    """远端有该路径且非空 → True；404（路径不存在 / 空仓库）→ False。

    ⚠️ `core.GitHub.contents()` 对不存在的路径是**抛 404**，不是返回 None，
    所以判定「是否已发布」必须在这里吃掉 404，否则新项目会直接崩栈。
    """
    try:
        return bool(with_retry(gh.contents, repo, path))
    except core.GitHubError as exc:
        if exc.status == 404:
            return False
        raise


def show_status(repo: str) -> int:
    """列出远端仓库里的顶层项目文件夹 —— 这些就是「已发布」的项目。"""
    token, source = core.resolve_token()
    if not token:
        log("[x] 没找到 GitHub 令牌（--token / GITHUB_TOKEN / refs.GITHUB_TOKEN / gh auth token）")
        return 3
    gh = core.GitHub(token)
    try:
        entries = with_retry(gh.contents, repo, "") or []
    except core.GitHubError as exc:
        if exc.status == 404:      # 空仓库：一个提交都没有时根目录也是 404
            entries = []
        else:
            log(f"[x] 读仓库失败（HTTP {exc.status}）：{exc}")
            return 3
    dirs = sorted(e["name"] for e in entries if e.get("type") == "dir")
    log(f"仓库 {repo}　令牌来源：{source}")
    if not dirs:
        log("  还没有任何项目文件夹。")
        return 0
    log(f"已发布项目 {len(dirs)} 个（这些项目更新后会自动提交推送）：")
    for d in dirs:
        log(f"  - {d}/")
    log("\n不在这张表里的就是新项目 —— 用户明确说要提交时才发布（加 --new）。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="把一个项目目录推到 GitHub 仓库（默认 yzz103/yzz-dsh）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法：", 1)[1] if __doc__ else None,
    )
    ap.add_argument("project", nargs="?", default="", help="要推的本地目录（--status 时可省略）")
    ap.add_argument("--status", action="store_true",
                    help="只列出远端仓库已发布的顶层项目文件夹（判定「是否已发布」的依据）")
    ap.add_argument("--new", action="store_true",
                    help="首次发布一个新项目；用户明确要求提交时才加，否则退出码 5")
    ap.add_argument("--name", default="", help="仓库内顶层文件夹名（默认取目录名去掉日期前缀）")
    ap.add_argument("--repo", default=os.environ.get("GH_REPO") or DEFAULT_REPO)
    ap.add_argument("--message", default="", help="提交信息")
    ap.add_argument("--branch", default="", help="目标分支（默认仓库默认分支）")
    ap.add_argument("--dry-run", action="store_true", help="只看计划，不联网")
    ap.add_argument("--allow-missing", action="store_true", help="骨架缺失也照推")
    ap.add_argument("--allow-local-paths", action="store_true",
                    help="含本机专属路径（如 D 盘安装目录）也照推")
    ap.add_argument("--force", action="store_true", help="允许强制覆盖分支 ref")
    ap.add_argument("--yes", action="store_true", help="兼容旧用法，现在无作用")
    args = ap.parse_args()

    if args.status:
        return show_status(args.repo)
    if not args.project:
        log("[x] 缺项目目录参数（只想看已发布项目就加 --status）")
        return 1

    root = Path(args.project).expanduser().resolve()
    if not root.is_dir():
        log(f"[x] 不是目录：{root}")
        return 1

    name = (args.name or project_name_from_dir(root.name)).strip().strip("/")
    if not name:
        log("[x] 项目名算不出来，请用 --name 指定")
        return 1

    log(f"本地目录：{root}")
    log(f"目标仓库：{args.repo}　文件夹：{name}/")

    files = collect(root)
    if not files:
        log("[x] 目录里没有可推的文件")
        return 1

    # 1) 密钥扫描（永远先跑）
    hits = scan_secrets(files)
    if hits:
        log(f"\n[x] 密钥扫描命中 {len(hits)} 处，已中止：")
        for p, no, label in hits:
            log(f"    {p.relative_to(root)}:{no}  ← {label}")
        log("    处理完再推（不要把这些文件的内容贴到日志里）。")
        return 2
    log(f"[ok] 密钥扫描：{len(files)} 个文件，零命中")

    # 2) 骨架检查
    missing = check_skeleton(root)
    if missing:
        if args.allow_missing:
            log(f"[!] 骨架缺：{', '.join(missing)}（--allow-missing，照推）")
        else:
            log(f"[x] 骨架不全：{', '.join(missing)}")
            log("    补齐 README / CHANGELOG / pyproject / tests 后再推，或加 --allow-missing。")
            return 1
    else:
        log("[ok] 骨架齐全：README / CHANGELOG / pyproject / tests")

    # 3) 本机专属路径：默认拦住（别人克隆下来跑不通就是废包）
    host = scan_host_paths(files)
    if host:
        log(f"\n[i] 有 {len(host)} 处本机专属路径（换台机器就用不了）：")
        for p, no, frag in host[:8]:
            log(f"    {p.relative_to(root)}:{no}  {frag}")
        if len(host) > 8:
            log(f"    …还有 {len(host) - 8} 处")
        if args.allow_local_paths:
            log("[!] --allow-local-paths：照推")
        else:
            log("[x] 已中止。改成相对路径 / 环境变量 / 命令行参数后再推；")
            log("    确实要发就加 --allow-local-paths（别推只有你自己能跑的包）。")
            return 4

    total = sum(p.stat().st_size for p in files)
    log(f"\n待推 {len(files)} 个文件，共 {core.human_size(total)}：")
    for p in files[:30]:
        log(f"    {core.human_size(p.stat().st_size):>9}  {p.relative_to(root)}")
    if len(files) > 30:
        log(f"    …还有 {len(files) - 30} 个")

    if args.dry_run:
        log("\n[dry-run] 到此为止，没有联网、没有推送。")
        log("          是否已发布要真推时才联网判定；先看已发布清单用 --status。")
        return 0

    # 4) 真推：按 MAX_FILES 分批，路径统一加 <name>/ 前缀
    token, source = core.resolve_token()
    if not token:
        log("[x] 没找到 GitHub 令牌（--token / GITHUB_TOKEN / refs.GITHUB_TOKEN / gh auth token）")
        return 3
    log(f"\n令牌来源：{source}")
    gh = core.GitHub(token)

    try:
        info = with_retry(gh.get_repo, args.repo)
    except core.GitHubError as exc:
        log(f"[x] 读仓库失败（HTTP {exc.status}）：{exc}")
        return 3

    # 4.1) 发布状态判定（用户约定 2026-10-20：新项目不自动发，已发布项目更新自动推）
    exists = remote_exists(gh, args.repo, name)
    if exists:
        log(f"\n[ok] 已发布项目（仓库里已有 {name}/）→ 更新自动提交推送")
    elif not args.new:
        log(f"\n[x] 仓库里没有 {name}/ —— 这是**新项目**，不自动发布。")
        log("    约定：新项目要用户明确说「提交 / 上传」才发；用户确认后再加 --new 重跑。")
        return 5
    else:
        log(f"\n[ok] 新项目首次发布（--new）：{name}/")
    message = args.message or (
        f"feat({name}): 首次发布" if not exists else f"chore({name}): 更新（{len(files)} 个文件）"
    )

    branch = args.branch or info.get("default_branch") or "main"
    chunks = [files[i:i + core.MAX_FILES] for i in range(0, len(files), core.MAX_FILES)]
    last_commit = ""
    for idx, chunk in enumerate(chunks, 1):
        payload = [{
            "path": f"{name}/{p.relative_to(root).as_posix()}",
            "content": str(p),
            "encoding": "path",
        } for p in chunk]
        msg = message if len(chunks) == 1 else f"{message}（第 {idx}/{len(chunks)} 批）"
        try:
            blocks, is_error = with_retry(core.tool_push, gh, {
                "repo": args.repo, "files": payload, "message": msg,
                "branch": args.branch, "force": args.force,
            })
        except core.GitHubError as exc:
            log(f"[x] 推送失败：{exc}")
            return 3
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        if is_error:
            log(f"[x] 推送失败：{text}")
            return 3
        log(text)
        m = re.search(r"提交：([0-9a-f]{6,})", text)
        if m:
            last_commit = m.group(1)

    log(f"\n[ok] 完成。分支 {branch}，最新提交 {last_commit}")
    log(f"     查看：https://github.com/{args.repo}/tree/{branch}/{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
