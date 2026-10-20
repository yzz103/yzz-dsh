#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""mcp_agnes —— 把本机 agnes2api 网关包装成一个 MCP 服务（stdio 传输）。

零第三方依赖：只用 Python 标准库实现 MCP 的 JSON-RPC 2.0 / stdio 行协议。
配套：D:\APP\agnes2api\agnes2api.exe（OpenAI 兼容网关，默认 127.0.0.1:7871）

暴露的工具（工具名由 MCP 客户端加前缀，例如 mcp__agnes__generate_image）：
  generate_image(prompt, size, model, show)   出一张图，存进网关 gallery
  list_images(limit)                          列出最近生成记录
  gateway_status()                            网关健康状态 / 账号 / 累计用量
  delete_image(id)                            删除一条生成记录（连文件）

用法（一般由 DSH 的 mcp-client 拉起，不用手跑）：
  python mcp_agnes.py
  python mcp_agnes.py --base http://127.0.0.1:7871 --config D:\APP\agnes2api\config.json
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SERVER_NAME = "mcp-agnes"
SERVER_VERSION = "1.2.0"
SUPPORTED_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2024-11-05"]
DEFAULT_BASE = "http://127.0.0.1:7871"
# 只在「本机默认安装位置」兜底；换设备用 --config / AGNES_CONFIG 指定，或干脆只给 --token
DEFAULT_CFG = Path(r"D:\APP\agnes2api\config.json")
DEFAULT_MODEL = "agnes-image-2.5-flash"


def default_config() -> Path:
    """按候选顺序找网关的 config.json：exe/脚本目录 → 上一级 → 当前目录 → 本机默认位置。"""
    here = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
    cands: list[Path] = [
        here / "config.json",
        here.parent / "config.json",
        Path.cwd() / "config.json",
        DEFAULT_CFG,
    ]
    for c in cands:
        try:
            if c.exists():
                return c
        except OSError:
            continue
    return cands[0]


def log(msg: str) -> None:
    """诊断信息一律走 stderr —— stdout 是 MCP 协议通道，写脏一个字节就断线。"""
    print(f"[mcp-agnes] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 网关访问

class Gateway:
    def __init__(self, base: str, config_path: Path | None = None, token: str = "",
                 base_explicit: bool = False) -> None:
        self.base = base.rstrip("/")
        # token 来源优先级：命令行/环境变量 > config.json（跨设备部署时可以完全不读配置文件）
        self.token = token
        self.timeout = 180.0
        if config_path is None:
            return
        try:
            cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
            self.token = self.token or str(cfg.get("admin_token") or "")
            if cfg.get("port") and not base_explicit:
                host = cfg.get("host") or "127.0.0.1"
                self.base = f"http://{host}:{cfg['port']}"
            self.timeout = float(cfg.get("request_timeout") or 180)
        except FileNotFoundError:
            log(f"未找到配置文件 {config_path}" + ("（已用 --token/AGNES_TOKEN，可继续）"
                                                   if self.token else "（管理类工具会不可用）"))
        except Exception as exc:  # noqa: BLE001
            log(f"读配置失败：{exc}")

    # -- 底层

    def _request(self, method: str, path: str, body: dict | None = None,
                 timeout: float | None = None) -> tuple[int, Any]:
        url = self.base + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("X-Admin-Token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                if "json" in ctype:
                    return resp.status, json.loads(raw.decode("utf-8"))
                return resp.status, raw
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw.decode("utf-8"))
            except Exception:  # noqa: BLE001
                return exc.code, {"error": raw.decode("utf-8", "replace")[:500]}

    # -- 业务

    def generate(self, prompt: str, size: str, model: str) -> tuple[int, Any]:
        body = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "n": 1,
            "return_base64": True,
        }
        return self._request("POST", "/v1/images/generations", body)

    def gallery(self, limit: int = 5) -> dict:
        status, data = self._request("GET", f"/admin/api/gallery?limit={limit}")
        if status != 200 or not isinstance(data, dict):
            raise RuntimeError(f"读生成记录失败（HTTP {status}）：{data}")
        return data

    def status(self) -> tuple[int, Any]:
        code, health = self._request("GET", "/health", timeout=10)
        if code != 200:
            return code, health
        scode, state = self._request("GET", "/admin/api/state", timeout=10)
        out: dict[str, Any] = {"health": health, "state": state if scode == 200 else {}}
        if scode != 200:
            # 管理接口要 admin_token；鉴权失败时别装作「账号数 0」蒙过去
            out["state_error"] = f"HTTP {scode}：{state}"
        return code, out

    def delete(self, gid: str) -> tuple[int, Any]:
        return self._request("DELETE", f"/admin/api/gallery/{gid}")


# ---------------------------------------------------------------- 工具定义

TOOLS = [
    {
        "name": "generate_image",
        "description": (
            "用本机 Agnes 网关生成一张图片。返回图片保存路径、尺寸、体积、耗时；"
            "默认同时把图片内容一起回给你看。中文提示词直接写，出图模型对中文理解很好。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "图片描述，中文即可，越具体越好（主体、光线、风格、构图）"},
                "size": {"type": "string", "description": "尺寸：1024x1024 / 2K / 1536x1024 等，默认 1024x1024"},
                "model": {"type": "string", "description": "模型，默认 agnes-image-2.5-flash"},
                "show": {"type": "boolean", "description": "是否把图片内容一起返回（默认 true）"},
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_images",
        "description": "列出网关最近生成过的图片记录（时间、尺寸、提示词、体积、耗时、用的哪个号）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "最多返回几条，默认 10，上限 100"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "gateway_status",
        "description": "查看 Agnes 网关的运行状态：版本、账号数、各号可用情况、累计出图数与错误数。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "delete_image",
        "description": "按记录 id 删除一张生成记录（磁盘文件一起删）。",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "记录 id，从 list_images 里拿"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    },
]


# ---------------------------------------------------------------- 工具实现

def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n} B"


def tool_generate(gw: Gateway, args: dict) -> tuple[list[dict], bool]:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return [{"type": "text", "text": "缺少 prompt"}], True
    size = str(args.get("size") or "1024x1024")
    model = str(args.get("model") or DEFAULT_MODEL)
    show = args.get("show", True)

    t0 = time.time()
    status, data = gw.generate(prompt, size, model)
    elapsed = time.time() - t0
    if status != 200 or not isinstance(data, dict):
        detail = json.dumps(data, ensure_ascii=False)[:600] if isinstance(data, (dict, list)) else str(data)[:600]
        return [{"type": "text", "text": f"出图失败（HTTP {status}，{elapsed:.1f}s）：{detail}"}], True

    item = data.get("data", [{}])[0] if data.get("data") else {}
    b64 = item.get("b64_json") or item.get("b64") or ""
    if isinstance(b64, str) and b64.startswith("data:"):
        b64 = b64.split(",", 1)[-1]
    task_id = data.get("task_id") or item.get("task_id") or ""

    rec: dict | None = None
    try:
        listing = gw.gallery(limit=8)
        for it in listing.get("items", []):
            if task_id and it.get("task_id") == task_id:
                rec = it
                break
        if rec is None and listing.get("items"):
            newest = listing["items"][0]
            if float(newest.get("duration_s") or 0) and abs(time.time() - _parse_ts(newest.get("time"))) < 900:
                rec = newest
    except Exception as exc:  # noqa: BLE001
        log(f"查生成记录失败（不影响出图）：{exc}")

    lines = [f"已生成 1 张图（{elapsed:.1f}s）", f"提示词：{prompt}"]
    if rec:
        lines += [
            f"文件　：{rec.get('file') or rec.get('url') or '(仅链接)'}",
            f"尺寸　：{rec.get('size') or size}",
            f"体积　：{human_size(int(rec.get('bytes') or 0))}",
            f"耗时　：{rec.get('duration_s')}s",
            f"记录id：{rec.get('id')}",
        ]
        root = None
        try:
            root = str(gw.gallery(limit=1).get("dir") or "")
        except Exception:  # noqa: BLE001
            pass
        if root and rec.get("file"):
            lines.append(f"路径　：{Path(root) / str(rec['file'])}")
    else:
        lines.append(f"task_id：{task_id or '(无)'}（网关未回记录，可能没开 gallery）")

    content: list[dict] = [{"type": "text", "text": "\n".join(lines)}]
    if show and b64:
        try:
            raw = base64.b64decode(b64)
        except Exception:  # noqa: BLE001
            raw = b""
        if raw and len(raw) <= 6 * 1024 * 1024:
            mime = "image/png"
            if raw[:3] == b"\xff\xd8\xff":
                mime = "image/jpeg"
            elif raw[:4] == b"RIFF":
                mime = "image/webp"
            content.append({"type": "image", "data": base64.b64encode(raw).decode("ascii"), "mimeType": mime})
            lines.append(f"（图片内容已随本次结果返回，{human_size(len(raw))}）")
        elif raw:
            lines.append("（图片超过 6 MB，只给路径不给内容）")
    return content, False


def _parse_ts(text: str | None) -> float:
    try:
        return time.mktime(time.strptime(str(text), "%Y-%m-%d %H:%M:%S"))
    except Exception:  # noqa: BLE001
        return 0.0


def tool_list(gw: Gateway, args: dict) -> tuple[list[dict], bool]:
    try:
        limit = int(args.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(100, limit))
    try:
        data = gw.gallery(limit=limit)
    except Exception as exc:  # noqa: BLE001
        return [{"type": "text", "text": f"读取失败：{exc}"}], True
    items = data.get("items") or []
    if not items:
        return [{"type": "text", "text": "还没有生成记录。"}], False
    out = [f"共 {data.get('count', len(items))} 条，目录 {data.get('dir')}", ""]
    for it in items:
        out.append(
            f"- {it.get('time')} | {it.get('size')} | {human_size(int(it.get('bytes') or 0))} | "
            f"{it.get('duration_s')}s | {it.get('model')} | {it.get('file') or it.get('url')}\n"
            f"  id={it.get('id')}  prompt={str(it.get('prompt') or '')[:80]}"
        )
    return [{"type": "text", "text": "\n".join(out)}], False


def tool_status(gw: Gateway, args: dict) -> tuple[list[dict], bool]:
    status, data = gw.status()
    if status != 200 or not isinstance(data, dict):
        return [{"type": "text", "text": f"网关不可达（HTTP {status}）：{data}"}], True
    health = data.get("health") or {}
    state = data.get("state") or {}
    totals = (state.get("stats") or {}).get("totals") or {}
    accounts = state.get("accounts") or []
    lines = [
        f"网关版本：{health.get('version')}　地址：{gw.base}",
        f"账号数：{health.get('accounts')}",
    ]
    for a in accounts:
        lines.append(
            f"- {a.get('label') or '(未命名)'}：{'可用' if a.get('enabled') else '已停用'}"
            f"　状态={a.get('state') or a.get('status') or '-'}"
            f"　RPM={a.get('rpm_used', 0)}{'/' + str(a['rpm_limit']) if a.get('rpm_limit') else ''}"
            f"　成功={a.get('ok', 0)} 失败={a.get('fail', 0)}"
        )
    lines.append(
        f"累计：文本 {totals.get('text', 0)} / 图片 {totals.get('image', 0)} / 视频 {totals.get('video', 0)}"
        f"　错误 {totals.get('errors', 0)}"
    )
    if data.get("state_error"):
        lines.append(f"⚠ 管理接口鉴权失败（{data['state_error']}）——检查 admin_token "
                     f"（--token / AGNES_TOKEN，或 config.json 里的 admin_token）")
    return [{"type": "text", "text": "\n".join(lines)}], False


def tool_delete(gw: Gateway, args: dict) -> tuple[list[dict], bool]:
    gid = str(args.get("id") or "").strip()
    if not gid:
        return [{"type": "text", "text": "缺少 id"}], True
    status, data = gw.delete(gid)
    if status != 200:
        return [{"type": "text", "text": f"删除失败（HTTP {status}）：{json.dumps(data, ensure_ascii=False)[:400]}"}], True
    return [{"type": "text", "text": f"已删除 {gid}"}], False


HANDLERS = {
    "generate_image": tool_generate,
    "list_images": tool_list,
    "gateway_status": tool_status,
    "delete_image": tool_delete,
}


# ---------------------------------------------------------------- MCP 协议

def make_result(rid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def make_error(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def handle(msg: dict, gw: Gateway) -> dict | None:
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        want = str(params.get("protocolVersion") or "")
        version = want if want in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        return make_result(rid, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": "图片生成工具：generate_image 直接给中文提示词即可；网关在本机 7871，无需联网配置。",
        })
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return make_result(rid, {})
    if method == "tools/list":
        return make_result(rid, {"tools": TOOLS})
    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if handler is None:
            return make_error(rid, -32602, f"未知工具：{name}")
        log(f"调用 {name} {json.dumps(args, ensure_ascii=False)[:200]}")
        try:
            content, is_error = handler(gw, args if isinstance(args, dict) else {})
        except Exception as exc:  # noqa: BLE001
            log(f"{name} 异常：{exc}")
            return make_result(rid, {"content": [{"type": "text", "text": f"工具执行异常：{exc}"}], "isError": True})
        return make_result(rid, {"content": content, "isError": is_error})
    if method in ("resources/list",):
        return make_result(rid, {"resources": []})
    if method in ("prompts/list",):
        return make_result(rid, {"prompts": []})
    if method is None:
        return None  # 客户端发来的响应，忽略
    return make_error(rid, -32601, f"不支持的方法：{method}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Agnes 生图 MCP 服务（stdio）")
    ap.add_argument("--base", default=os.environ.get("AGNES_BASE") or DEFAULT_BASE,
                    help="网关地址，默认 http://127.0.0.1:7871")
    ap.add_argument("--config",
                    default=os.environ.get("AGNES_CONFIG") or str(default_config()),
                    help="网关 config.json（跨设备部署时可改用 --token，不必读文件）")
    ap.add_argument("--token", default=os.environ.get("AGNES_TOKEN") or "",
                    help="网关 admin_token；不给就读 config.json 里的")
    ap.add_argument("--no-config", action="store_true", help="完全不读 config.json")
    opts = ap.parse_args()

    # Windows 控制台默认 cp936，协议通道必须是 UTF-8（客户端按 UTF-8 解码每一行）
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    token = opts.token or ""
    cfg_path = Path(opts.config)
    if opts.no_config:
        cfg_use: Path | None = None
    elif cfg_path.exists():
        cfg_use = cfg_path
    elif token:
        cfg_use = None  # 只给了 token、没有这个文件：直接用 token，不必抱怨
    else:
        cfg_use = cfg_path  # 没 token 也没文件：让 Gateway 报一次缺失
    base_explicit = bool(os.environ.get("AGNES_BASE")) or ("--base" in sys.argv)
    gw = Gateway(opts.base, cfg_use, token, base_explicit)
    log(f"启动 v{SERVER_VERSION}，网关 {gw.base}，token {'有' if gw.token else '无'}"
        + (f"，配置 {cfg_use}" if cfg_use else "，未读配置文件"))

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log(f"收到非 JSON 行，丢弃：{line[:120]}")
            continue
        reply = handle(msg, gw)
        if reply is None:
            continue
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
