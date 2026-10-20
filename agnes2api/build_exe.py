#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""build_exe.py —— 把 agnes2api.py / mcp_agnes.py 重新打成 exe（PyInstaller）。

为什么需要它（2026-10-21 实测教训）：
    之前 `安装目录\agnes2api.exe`（v1.4.0）和 `...\mcp\mcp_agnes.exe`（v1.1.0）
    都是「打完之后源码又改了几轮」的陈旧产物。表现是 MCP 工具报
    `Remote end closed connection without response`、而同样提示词直连网关却正常 ——
    查了半天才发现跑的不是最新的代码。**改完源码就要重新打包，别让 exe 落后于源码。**

用法：
    python build_exe.py                  # 两个都打
    python build_exe.py gateway          # 只打 agnes2api.exe
    python build_exe.py mcp              # 只打 mcp_agnes.exe
    python build_exe.py gateway --install  # 打完顺手替换安装目录下的 exe（先备份旧版）

安装目录（--install 往哪拷）**不写死**，按顺序取：
    --dest <目录> → 环境变量 AGNES2API_DIR → 脚本同级的 ../agnes2api

产物：
    _build\dist\agnes2api\agnes2api.exe      （onedir，带 _internal）
    _build\dist\mcp_agnes\mcp_agnes.exe      （onedir，带 _internal）

依赖：`python -m pip install pyinstaller`（本机 2026-10-21 已装 6.22.3，
装的时候走清华镜像：`-i https://pypi.tuna.tsinghua.edu.cn/simple`）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD = HERE / "_build"
# 安装目录（--install 往哪拷）。不写死本机路径：优先 --dest，其次环境变量
# AGNES2API_DIR，最后退回「脚本同目录的上一级 / agnes2api」。换机器只改环境变量即可。
APP = Path(os.environ.get("AGNES2API_DIR") or (HERE.parent / "agnes2api"))


def run_build(name: str, script: str) -> Path:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir", "--console",
        "--name", name,
        "--distpath", str(BUILD / "dist"),
        "--workpath", str(BUILD / "work"),
        "--specpath", str(BUILD),
        str(HERE / script),
    ]
    print("[build]", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=str(HERE))
    if proc.returncode != 0:
        raise SystemExit(f"[x] PyInstaller 失败，退出码 {proc.returncode}")
    out = BUILD / "dist" / name / f"{name}.exe"
    if not out.is_file():
        raise SystemExit(f"[x] 没找到产物 {out}")
    print(f"[ok] {out}  {out.stat().st_size / 1024 / 1024:.2f} MB", flush=True)
    return out


def install(name: str, target_dir: Path) -> None:
    """把新 build 拷到 target_dir，旧的 exe / _internal 先改名备份。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    src = BUILD / "dist" / name
    target_dir.mkdir(parents=True, exist_ok=True)
    exe = target_dir / f"{name}.exe"
    internal = target_dir / "_internal"
    if exe.is_file():
        exe.replace(target_dir / f"{name}.exe.old-{stamp}")
        print(f"[bak] {exe.name}.old-{stamp}")
    if internal.is_dir():
        internal.replace(target_dir / f"_internal.old-{stamp}")
        print(f"[bak] _internal.old-{stamp}")
    shutil.copy2(src / f"{name}.exe", exe)
    shutil.copytree(src / "_internal", internal)
    print(f"[ok] 已安装到 {target_dir}")


def main() -> int:
    global APP
    ap = argparse.ArgumentParser(description="重新打包 Agnes 网关 / MCP 的 exe")
    ap.add_argument("what", nargs="?", default="all", choices=["all", "gateway", "mcp"])
    ap.add_argument("--install", action="store_true",
                    help="打完后替换安装目录下的 exe（默认读环境变量 AGNES2API_DIR）")
    ap.add_argument("--dest", default="",
                    help="安装目录，覆盖 AGNES2API_DIR / 默认值")
    args = ap.parse_args()
    if args.dest:
        APP = Path(args.dest)
    if args.install and not APP.is_dir():
        raise SystemExit(f"[x] 安装目录不存在：{APP}\n    用 --dest 指定，或设环境变量 AGNES2API_DIR")

    todo = []
    if args.what in ("all", "gateway"):
        todo.append(("agnes2api", "agnes2api.py", APP))
    if args.what in ("all", "mcp"):
        todo.append(("mcp_agnes", "mcp_agnes.py", APP / "mcp"))

    for name, script, dest in todo:
        run_build(name, script)
        if args.install:
            install(name, dest)
    print("\n[bom] 打完记得：网关要重启（先关掉旧进程），MCP 改 py/exe 模式用 install_mcp_config.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
