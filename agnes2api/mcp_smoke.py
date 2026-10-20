# -*- coding: utf-8 -*-
r"""mcp_agnes.py 冒烟 / 回归测试：用原始 JSON-RPC 走 stdio 行协议，不依赖任何 MCP SDK。

用法：
    python mcp_smoke.py                # 只测协议 + 网关状态（不出图，不花钱）
    python mcp_smoke.py --generate     # 额外真出一张图，验证端到端
    python mcp_smoke.py --size 2K      # 出图尺寸档位（1K/2K/3K/4K 或 1024x1024）
    python mcp_smoke.py --exe .\mcp_agnes.exe                        # 测打包版 exe

退出码：0 全部通过；1 有检查项失败。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

PY = sys.executable
SRV = str(Path(__file__).resolve().with_name("mcp_agnes.py"))


class Smoke:
    def __init__(self, cmd: list[str]) -> None:
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self.seq = 0
        self.fails: list[str] = []

    def call(self, method: str, params: dict | None = None, wait: float = 300) -> dict:
        self.seq += 1
        msg = {"jsonrpc": "2.0", "id": self.seq, "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        t0 = time.time()
        while time.time() - t0 < wait:
            line = self.proc.stdout.readline()
            if not line:
                raise SystemExit(f"服务端关闭了 stdout（{method}）；stderr:\n{self.proc.stderr.read()[:800]}")
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            if data.get("id") == self.seq:
                return data
        raise SystemExit(f"等 {method} 超时（{wait}s）")

    def notify(self, method: str) -> None:
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        print(("PASS  " if ok else "FAIL  ") + label + (("  " + detail) if detail else ""))
        if not ok:
            self.fails.append(label)

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        err = self.proc.stderr.read()
        if err.strip():
            print("--- stderr ---")
            print(err.strip()[:800])


def main() -> int:
    ap = argparse.ArgumentParser(description="mcp_agnes 冒烟测试")
    ap.add_argument("--server", default=SRV, help="服务脚本路径（源码模式）")
    ap.add_argument("--python", default=PY, help="Python 解释器（源码模式）")
    ap.add_argument("--exe", default="", help="直接测打包好的 mcp_agnes.exe（优先于 --server）")
    ap.add_argument("--generate", action="store_true", help="额外真出一张图")
    ap.add_argument("--size", default="1024x1024", help="出图尺寸")
    ap.add_argument("--prompt", default="一只戴围巾的柯基犬坐在雪地里，写实摄影")
    args = ap.parse_args()

    cmd = [args.exe] if args.exe else [args.python, args.server]
    sm = Smoke(cmd)
    try:
        init = sm.call("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                      "clientInfo": {"name": "smoke", "version": "0"}})
        res = init.get("result", {})
        sm.check(bool(res.get("protocolVersion")), "initialize 协商版本",
                 str(res.get("protocolVersion")))
        sm.check(res.get("serverInfo", {}).get("name") == "mcp-agnes", "serverInfo.name")
        sm.check("tools" in (res.get("capabilities") or {}), "capabilities.tools")
        sm.notify("notifications/initialized")

        ping = sm.call("ping", {})
        sm.check(ping.get("result") == {} or ping.get("result") is not None, "ping")

        tools = [t["name"] for t in sm.call("tools/list")["result"]["tools"]]
        for name in ("generate_image", "list_images", "gateway_status", "delete_image"):
            sm.check(name in tools, f"tools/list 含 {name}")
        schema = [t for t in sm.call("tools/list")["result"]["tools"] if t["name"] == "generate_image"][0]
        sm.check("prompt" in (schema.get("inputSchema", {}).get("required") or []),
                 "generate_image 的 prompt 是必填")

        unknown = sm.call("tools/call", {"name": "no_such_tool", "arguments": {}})
        sm.check(bool(unknown.get("error")), "未知工具返回 error", str(unknown.get("error", {}).get("code")))

        st = sm.call("tools/call", {"name": "gateway_status", "arguments": {}})
        text = (st["result"]["content"][0]["text"] if st["result"].get("content") else "")
        sm.check("网关版本" in text, "gateway_status 连通本机网关", text.splitlines()[0] if text else "")
        sm.check("鉴权失败" not in text, "admin_token 有效（管理接口 200）",
                 "" if "鉴权失败" not in text else text.splitlines()[-1][:120])

        imgs = sm.call("tools/call", {"name": "list_images", "arguments": {"limit": 3}})
        t2 = (imgs["result"]["content"][0]["text"] if imgs["result"].get("content") else "")
        sm.check(bool(t2.strip()), "list_images 有返回", t2.splitlines()[0][:80] if t2 else "")

        if args.generate:
            gen = sm.call("tools/call", {"name": "generate_image",
                                         "arguments": {"prompt": args.prompt,
                                                       "size": args.size, "show": True}})
            r = gen["result"]
            body = next((c for c in r.get("content", []) if c.get("type") == "text"), {})
            img = next((c for c in r.get("content", []) if c.get("type") == "image"), None)
            sm.check(not r.get("isError") and "已生成" in body.get("text", ""), "generate_image 出图")
            sm.check(bool(img) and img.get("mimeType", "").startswith("image/"),
                     "回传图片内容", f"{img.get('mimeType')} {len(img.get('data', ''))} b64" if img else "")
            print(body.get("text", ""))
    finally:
        sm.close()

    print()
    print("全部通过" if not sm.fails else "失败项：" + ", ".join(sm.fails))
    return 0 if not sm.fails else 1


if __name__ == "__main__":
    sys.exit(main())
