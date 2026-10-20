# -*- coding: utf-8 -*-
r"""把 Agnes 生图 MCP 服务写进 desktop profile 的 cordis.patch.yml（或改成 exe 模式）。

用法：
  python install_mcp_config.py          # 自动：有 exe 就用 exe，否则用 python 跑源码
  python install_mcp_config.py exe      # 强制用 D:\APP\agnes2api\mcp\mcp_agnes.exe
  python install_mcp_config.py py       # 强制用 C:\Program Files\Python312\python.exe + 源码

要点（2026-10-20 实测）：
  * profile 的 patch 是「覆盖层」，`- id: xxx` 只能改**已存在**的条目；
    全新条目必须用 `- insert: [ {...}, ... ]`，否则会被静默跳过（还可能在
    文件被回写时整条消失）。
  * YAML 里的 Windows 路径必须用**单引号**，双引号里 `\P` 是非法转义。
  * 改这里只影响「启动哪个程序」；insert 条目本身新增时才需要重启 DSH。
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

PROFILE = Path.home() / ".dsh/profiles/desktop/cordis.patch.yml"
MARK = "# --- Agnes 生图 MCP"
EXE = Path(r"D:\APP\agnes2api\mcp\mcp_agnes.exe")
PYTHON = Path(r"C:\Program Files\Python312\python.exe")
SCRIPT = Path(r"D:\DSH\_工具\Agnes网关\mcp_agnes.py")

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
        toolCallTimeoutMs: 300000
        failOnStartupError: false
"""


def block_exe() -> str:
    return (HEAD.format(what="mcp_agnes.exe")
            + f"        command: '{EXE.as_posix()}'\n"
            + TAIL)


def block_py() -> str:
    return (HEAD.format(what="mcp_agnes.py")
            + f"        command: '{PYTHON}'\n"
            + "        args:\n"
            + f"          - '{SCRIPT}'\n"
            + TAIL)


def strip_block(text: str) -> str:
    """去掉已有的 Agnes MCP 段落（从 MARK 到该 insert 块结束）。"""
    idx = text.find(MARK)
    if idx < 0:
        return text
    lines = text[:idx].splitlines()
    rest = text[idx:].splitlines()[1:]
    out = list(lines)
    in_block = False
    for line in rest:
        if line.startswith("- insert:") or line.startswith("- id:"):
            in_block = True
            continue
        if in_block:
            if line.strip() == "" or line.startswith((" ", "\t", "#")):
                continue
            out.append(line)          # 块结束后遇到新的顶层条目，保留它
            in_block = False
        else:
            out.append(line)
    return "\n".join(out)


def main() -> int:
    arg = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if arg in ("exe", "py"):
        mode = arg
    else:
        mode = "exe" if EXE.exists() else "py"
    if mode == "exe" and not EXE.exists():
        print(f"找不到 {EXE}，先解压发行包或改用 py 模式")
        return 2
    blk = block_exe() if mode == "exe" else block_py()

    text = PROFILE.read_text(encoding="utf-8")
    if blk.strip() and blk.strip() in text:
        print(f"已是 {mode} 模式，无需改动")
        return 0
    backup = PROFILE.with_name(
        PROFILE.name + ".bak-mcp-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(PROFILE, backup)
    print("备份:", backup)
    new = strip_block(text).rstrip("\n") + "\n\n" + blk
    PROFILE.write_text(new, encoding="utf-8")
    print(f"已改成 {mode} 模式 →", PROFILE,
          "，当前行数:", len(new.splitlines()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
