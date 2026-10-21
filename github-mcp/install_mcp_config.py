# -*- coding: utf-8 -*-
r"""把自写的 GitHub MCP 服务写进 DSH 某个 profile 的 cordis.patch.yml。

用法：
  python install_mcp_config.py                # 安装/更新（幂等，重复跑不会重复插）
  python install_mcp_config.py --remove       # 撤掉这一段（回到没有 GitHub MCP 的状态）
  python install_mcp_config.py --profile 名字  # 写进别的 profile（默认 desktop）

换设备要改的只有本文件所在目录（服务脚本就在同目录）。profile 目录默认
~/.dsh/profiles/desktop，可用 --profile / DSH_PROFILE_DIR 覆盖。

要点（2026-10-20 实测）：
  * profile 的 patch 是「覆盖层」，`- id: xxx` 只能改**已存在**的条目；
    全新条目必须用 `- insert: [ {...} ]`，否则会被静默跳过，甚至在文件被回写时整条消失。
  * YAML 里的 Windows 路径必须用**单引号**，双引号里 `\P` 是非法转义。
  * 新增 insert 条目是**热生效**的（约 10 秒内 mcp-client 就起子进程），不必重启 DSH。
"""
from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARK = "# --- GitHub MCP（自写 mcp_github.py）---"
PYTHON = Path(sys.executable)
SCRIPT = HERE / "mcp_github.py"


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


def build_block() -> str:
    return f"""{MARK}
# 用 insert 形式追加：profile patch 是覆盖层，`- id:` 只能改已存在的条目。
# 工具名形如 mcp__github__github_push_files；令牌由脚本自己按顺序找
# （--token → GITHUB_TOKEN/GH_TOKEN → token 文件 → ~/.dsh/.credentials.yaml → gh CLI）。
- insert:
    - id: mcp-github
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: github
        transport: stdio
        command: '{PYTHON}'
        args:
          - '{SCRIPT}'
        env:
          PYTHONUTF8: '1'
          PYTHONIOENCODING: 'utf-8'
        toolCallTimeoutMs: 300000
        failOnStartupError: false
"""


def strip_block(text: str) -> str:
    """去掉已有的 GitHub MCP 段落（从 MARK 到该 insert 块结束）。

    块边界判据 = **缩进回到 0 的非空行**。旧实现把「顶层注释行」也算进块内容，
    于是紧跟其后的另一个 MCP 段落（`# --- xxx MCP ---` + `- insert:`）会被整段吃掉
    —— 2026-10-21 实测：跑一次 Agnes 的 install_mcp_config.py，GitHub MCP 段落消失。
    """
    idx = text.find(MARK)
    if idx < 0:
        return text
    out = text[:idx].splitlines()
    rest = text[idx:].splitlines()[1:]
    n = len(rest)
    i = 0
    # 跳过 MARK 与 insert 之间的说明性注释
    while i < n and not rest[i].startswith(("- insert:", "- id:")):
        i += 1
    if i < n:
        i += 1  # 吃掉 insert 行本身
        while i < n and (rest[i].strip() == "" or rest[i][:1] in (" ", "\t")):
            i += 1
    tail = rest[i:]
    while tail and tail[0].strip() == "":
        tail.pop(0)
    out.extend(tail)
    return "\n".join(out)


def parse_args(argv: list[str]) -> tuple[str, bool]:
    """返回 (profile, remove)。"""
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
        i += 1
    return profile, remove


def main() -> int:
    profile, remove = parse_args(sys.argv[1:])
    target = profile_path(profile)
    if not target.exists():
        print("找不到 profile patch:", target)
        print("  用 --profile 指定 profile 名，或设置环境变量 DSH_PROFILE_DIR")
        return 2
    text = target.read_text(encoding="utf-8")
    block = build_block()

    if remove:
        if MARK not in text:
            print("没有 GitHub MCP 段落，无需改动")
            return 0
        backup = target.with_name(target.name + ".bak-ghmcp-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(target, backup)
        new = strip_block(text).rstrip("\n") + "\n"
        target.write_text(new, encoding="utf-8")
        print("已移除 →", target, "备份:", backup.name)
        return 0

    if not SCRIPT.is_file():
        print("找不到服务脚本:", SCRIPT)
        return 2

    if block in text:
        print("已安装且内容一致，无需改动")
        return 0
    backup = target.with_name(target.name + ".bak-ghmcp-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(target, backup)
    new = strip_block(text).rstrip("\n") + "\n\n" + block
    target.write_text(new, encoding="utf-8")
    print("已写入 →", target)
    print("备份:", backup.name, "行数:", len(new.splitlines()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
