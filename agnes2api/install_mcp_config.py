# -*- coding: utf-8 -*-
r"""把 Agnes 生图 MCP 服务写进 DSH 某个 profile 的 cordis.patch.yml（或改成 exe 模式）。

用法：
  python install_mcp_config.py                # 自动：找得到 exe 就用 exe，否则用当前解释器跑源码
  python install_mcp_config.py exe            # 强制用 mcp_agnes.exe（同目录或 ../mcp/ 下）
  python install_mcp_config.py py             # 强制用「当前 Python + 本目录源码」
  python install_mcp_config.py --profile 名字  # 写进别的 profile（默认 desktop）
  python install_mcp_config.py --remove       # 撤掉这一段

换设备要改的只有两处：本文件所在目录（源码就在同目录）与 exe 位置
（放在同目录，或用环境变量 AGNES_MCP_EXE 指定）。profile 目录默认
~/.dsh/profiles/desktop，可用 --profile / DSH_PROFILE_DIR 覆盖。

要点（2026-10-20 实测）：
  * profile 的 patch 是「覆盖层」，`- id: xxx` 只能改**已存在**的条目；
    全新条目必须用 `- insert: [ {...}, ... ]`，否则会被静默跳过（还可能在
    文件被回写时整条消失）。
  * YAML 里的 Windows 路径必须用**单引号**，双引号里 `\P` 是非法转义。
  * 新增 insert 条目是**热生效**的（约 10 秒内 mcp-client 就会起子进程），不必重启 DSH。
"""
from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARK = "# --- Agnes 生图 MCP"
SCRIPT = HERE / "mcp_agnes.py"
PYTHON = Path(sys.executable)


def find_exe() -> Path:
    """找 mcp_agnes.exe：环境变量 AGNES_MCP_EXE → 同目录 → ../mcp/。"""
    env = os.environ.get("AGNES_MCP_EXE")
    cands = [Path(env)] if env else []
    cands += [HERE / "mcp_agnes.exe", HERE.parent / "mcp" / "mcp_agnes.exe"]
    for c in cands:
        if c.is_file():
            return c
    return cands[0]


def profile_path(profile: str) -> Path:
    """定位 cordis.patch.yml。

    ⚠ DSH 自己注入的 `DSH_PROFILE_DIR` 指向**某个 profile 目录本身**
    （如 `C:\\Users\\zz\\.dsh\\profiles\\desktop`），不是 profiles 的上级目录；
    早先按「上级目录 + profile 名」拼，会变成 …/desktop/desktop/cordis.patch.yml
    （2026-10-21 实测报「找不到 profile patch」）。
    """
    root = os.environ.get("DSH_PROFILE_DIR")
    if root:
        p = Path(root)
        if p.name == profile or (p / "cordis.patch.yml").exists():
            return p / "cordis.patch.yml"
        return p / profile / "cordis.patch.yml"
    return Path.home() / ".dsh" / "profiles" / profile / "cordis.patch.yml"


HEAD = """# --- Agnes 生图 MCP（{what}）---
# 用 insert 形式追加：profile patch 是覆盖层，`- id:` 只能改已存在的条目。
# 工具名形如 mcp__agnes__generate_image；服务端走本机 7871 网关。
- insert:
    - id: mcp-agnes
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: agnes
        transport: stdio
"""

TAIL = """        env:
          PYTHONUTF8: '1'
          PYTHONIOENCODING: 'utf-8'
{extra_env}        toolCallTimeoutMs: 900000
        failOnStartupError: false
"""


def block_exe(exe: Path) -> str:
    return (HEAD.format(what="mcp_agnes.exe")
            + f"        command: '{exe.as_posix()}'\n"
            + TAIL.format(extra_env=""))


def block_py() -> str:
    """py 模式下脚本在 _工具 里，拿不到网关目录的 config.json，
    所以额外把 AGNES_CONFIG 指过去（可用同名环境变量覆盖）。"""
    cfg = os.environ.get("AGNES_CONFIG") or "D:/APP/agnes2api/config.json"
    return (HEAD.format(what="mcp_agnes.py")
            + f"        command: '{PYTHON}'\n"
            + "        args:\n"
            + f"          - '{SCRIPT}'\n"
            + TAIL.format(extra_env=f"          AGNES_CONFIG: '{cfg}'\n"))


def strip_block(text: str) -> str:
    """去掉已有的 Agnes MCP 段落（从 MARK 到该 insert 块结束）。

    块边界判据 = **缩进回到 0 的非空行**。旧实现把「顶层注释行」也算进块内容，
    于是紧跟其后的另一个 MCP 段落会被整段吃掉（2026-10-21 实测：跑一次本脚本，
    cordis.patch.yml 里的 GitHub MCP 段落整段消失）。
    """
    idx = text.find(MARK)
    if idx < 0:
        return text
    out = text[:idx].splitlines()
    rest = text[idx:].splitlines()[1:]
    n = len(rest)
    i = 0
    while i < n and not rest[i].startswith(("- insert:", "- id:")):
        i += 1
    if i < n:
        i += 1
        while i < n and (rest[i].strip() == "" or rest[i][:1] in (" ", "\t")):
            i += 1
    tail = rest[i:]
    while tail and tail[0].strip() == "":
        tail.pop(0)
    out.extend(tail)
    return "\n".join(out)


def parse_args(argv: list[str]) -> tuple[str, str, bool]:
    """返回 (mode, profile, remove)。mode 取 'exe' / 'py' / ''（自动）。"""
    mode = ""
    profile = os.environ.get("DSH_PROFILE") or "desktop"
    remove = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--profile", "-p") and i + 1 < len(argv):
            profile = argv[i + 1]
            i += 2
            continue
        if a == "--remove":
            remove = True
        elif a.lower() in ("exe", "py"):
            mode = a.lower()
        i += 1
    return mode, profile, remove


def main() -> int:
    mode, profile, remove = parse_args(sys.argv[1:])
    target = profile_path(profile)
    if not target.exists():
        print("找不到 profile patch:", target)
        print("  用 --profile 指定 profile 名，或设置环境变量 DSH_PROFILE_DIR")
        return 2

    text = target.read_text(encoding="utf-8")

    if remove:
        if MARK not in text:
            print("没有 Agnes MCP 段落，无需改动")
            return 0
        backup = target.with_name(target.name + ".bak-mcp-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(target, backup)
        new = strip_block(text).rstrip("\n") + "\n"
        target.write_text(new, encoding="utf-8")
        print("已移除 →", target, "备份:", backup.name)
        return 0

    exe = find_exe()
    if not mode:
        mode = "exe" if exe.is_file() else "py"
    if mode == "exe" and not exe.is_file():
        print(f"找不到 {exe}，先解压发行包 / 用 AGNES_MCP_EXE 指定，或改用 py 模式")
        return 2
    if mode == "py" and not SCRIPT.is_file():
        print(f"找不到 {SCRIPT}，py 模式需要 mcp_agnes.py 与本脚本同目录")
        return 2

    blk = block_exe(exe) if mode == "exe" else block_py()
    if blk.strip() and blk.strip() in text:
        print(f"已是 {mode} 模式，无需改动")
        return 0

    backup = target.with_name(target.name + ".bak-mcp-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(target, backup)
    print("备份:", backup)
    new = strip_block(text).rstrip("\n") + "\n\n" + blk
    target.write_text(new, encoding="utf-8")
    what = exe.as_posix() if mode == "exe" else f"{PYTHON} {SCRIPT}"
    print(f"已改成 {mode} 模式（{what}）→", target,
          "，当前行数:", len(new.splitlines()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
