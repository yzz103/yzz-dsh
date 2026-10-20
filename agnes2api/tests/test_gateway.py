#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agnes2api 离线自测：用进程内假上游替身，不碰真网络、不需要真 key。

跑法（bash）：
  "/c/Program Files/Python312/python.exe" test_gateway.py

覆盖：
  1  /health
  2  /v1/models 透传（只做格式校验的 key 也能列）
  3  图片请求的 response_format 被自动挪进 extra_body
  4  401 换号重试（坏号在池里，请求仍成功）
  5  429 冷却 + 全池不可用 → 本地 429 rate_limit_local
  6  RPM 上限拦截
  7  管理 API 鉴权 / 加号 / 停用 / 删号 / probe
  8  文本 SSE 流透传
  9  4xx 不换号，直接回给客户端
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 同时兼容两种布局：源码树里测试与被测脚本同目录；发布仓库里测试在 tests/ 下
ROOT = HERE.parent if HERE.name == "tests" else HERE
sys.path.insert(0, str(ROOT))

import agnes2api as gw  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   [{detail}]" if detail else ""))


# --------------------------------------------------------------------------- #
# 假上游
# --------------------------------------------------------------------------- #

SEEN: dict = {"bodies": [], "keys": []}
VALID_KEYS = {"sk-good-1", "sk-good-2", "sk-not-a-real-key"}
FLAKY_429 = {"sk-flaky-429"}
BAD = {"sk-bad-401"}
# 1x1 透明 PNG，用来验「base64 落盘 + 管理台取图」
TINY_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
                "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class FakeUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, status, obj, ctype="application/json", raw=None):
        body = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _key(self) -> str:
        auth = self.headers.get("Authorization", "")
        return auth[7:] if auth.startswith("Bearer ") else ""

    def do_GET(self):
        key = self._key()
        SEEN["keys"].append(key)
        p = self.path.split("?")[0]
        if p == "/v1/models":
            if key in BAD:
                return self._send(401, {"error": {"message": "Invalid token"}})
            return self._send(200, {"object": "list", "data": [
                {"id": "agnes-image-2.0-flash"}, {"id": "agnes-image-2.5-flash"},
                {"id": "agnes-2.5-flash"}]})
        if p.startswith("/v1/videos/"):
            return self._send(200, {"id": "v-1", "status": "succeeded"})
        self._send(404, {"error": {"message": "no route"}})

    def do_POST(self):
        key = self._key()
        SEEN["keys"].append(key)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        SEEN["bodies"].append(body)
        p = self.path.split("?")[0]

        if key in BAD:
            return self._send(401, {"error": {"message": "Invalid token"}})
        if key in FLAKY_429:
            return self._send(429, {"error": {"message": "rate limited"}})

        if p == "/v1/images/generations":
            doc = json.loads(body or b"{}")
            if doc.get("model") == "agnes2api-probe-invalid-model":
                return self._send(400, {"error": {"message": "model not found"}})
            if doc.get("model") == "agnes-image-b64-test":
                return self._send(200, {"created": int(time.time()),
                                        "task_id": "task_b64_test",
                                        "data": [{"b64_json": TINY_PNG_B64}]})
            if "model" not in doc or "prompt" not in doc:
                return self._send(400, {"error": {"message": "model/prompt required"}})
            return self._send(200, {"created": int(time.time()), "data": [
                {"url": f"https://fake.example/{doc.get('size','1K')}.png"}]})

        if p == "/v1/chat/completions":
            doc = json.loads(body or b"{}")
            if doc.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for i in range(3):
                    payload = f"data: {{\"choices\":[{{\"delta\":{{\"content\":\"t{i}\"}}}}]}}\n\n"
                    self.wfile.write(payload.encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return
            return self._send(200, {"choices": [{"message": {"content": "hi"}}]})

        if p in ("/v1/videos", "/v1/video/generations"):
            return self._send(200, {"id": "v-1", "status": "queued"})

        self._send(404, {"error": {"message": "no route"}})


def start_fake() -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"


# --------------------------------------------------------------------------- #
# 客户端小工具
# --------------------------------------------------------------------------- #

def post(url, obj, token=None):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          **({"X-Admin-Token": token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def get(url, token=None):
    req = urllib.request.Request(url, headers={"X-Admin-Token": token} if token else {})
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def delete(url, token=None):
    req = urllib.request.Request(url, method="DELETE",
                                 headers={"X-Admin-Token": token} if token else {})
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def raw_get(url):
    """按原始字节取（管理台的图片接口传二进制）。"""
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def main() -> int:
    fake, upstream = start_fake()
    print(f"假上游: {upstream}")

    workdir = Path(tempfile.mkdtemp(prefix="agnes2api_test_"))
    cfg_path = workdir / "config.json"
    cfg_path.write_text(json.dumps({
        "host": "127.0.0.1", "port": 0, "upstream": upstream,
        "admin_token": "test-token",
        "accounts": [
            {"label": "好号", "key": "sk-good-1"},
            {"label": "坏号", "key": "sk-bad-401"},
        ],
        "limits": {"text_rpm": 10, "image_rpm": {"1K": 10, "2K": 5, "3K": 1, "4K": 1},
                   "video_rpm": 120},
        # 这组用例的上游是本地假服务，关掉自动识别，免得加号时去探真实的国际站/国内站
        "auto_base": False,
    }, ensure_ascii=False), encoding="utf-8")

    g = gw.Gateway(cfg_path, workdir / "stats.json")
    httpd = gw.build_server(g, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    print(f"网关  : {base}\n")

    # 1 health
    st, body = get(base + "/health")
    check("1 /health 通", st == 200 and json.loads(body)["ok"] is True, f"{st}")

    # 2 models
    st, body = get(base + "/v1/models")
    ids = [m["id"] for m in json.loads(body)["data"]] if st == 200 else []
    check("2 /v1/models 透传", st == 200 and "agnes-image-2.0-flash" in ids, f"{st} {ids}")

    # 3 response_format 挪进 extra_body
    SEEN["bodies"].clear()
    st, body = post(base + "/v1/images/generations", {
        "model": "agnes-image-2.0-flash", "prompt": "一只橘猫", "size": "1024x1024",
        "response_format": "url", "n": 1})
    sent = json.loads(SEEN["bodies"][-1]) if SEEN["bodies"] else {}
    moved = sent.get("extra_body", {}).get("response_format") == "url" \
        and "response_format" not in sent
    check("3 response_format 自动下沉 extra_body", st == 200 and moved,
          f"{st} sent={json.dumps(sent, ensure_ascii=False)[:160]}")

    # 4 401 换号：把坏号排到最前（last_used 更早）
    g.pool.accounts[1].last_used = 0        # 坏号
    g.pool.accounts[0].last_used = time.time()
    g.pool.accounts[1].cooldown_until = 0.0
    SEEN["keys"].clear()
    st, body = post(base + "/v1/images/generations", {
        "model": "agnes-image-2.0-flash", "prompt": "测试换号", "size": "1024x1024"})
    keys = SEEN["keys"][-2:] if len(SEEN["keys"]) >= 2 else SEEN["keys"]
    check("4 坏号 401 → 自动换好号", st == 200 and keys[:1] == ["sk-bad-401"], f"{st} keys={keys}")
    if len(keys) >= 2:
        check("4b 坏号进入 1h 冷却",
              g.pool.accounts[1].cooldown_until - time.time() > 3000,
              f"{round(g.pool.accounts[1].cooldown_until - time.time())}s")

    # 5 429：只留一个持续限流的号
    g.pool.remove(g.pool.accounts[1].id)          # 删坏号
    good_id = g.pool.accounts[0].id
    g.pool.set_enabled(good_id, False)             # 停好号
    g.pool.add("限流号", "sk-flaky-429")
    st, body = post(base + "/v1/images/generations", {
        "model": "agnes-image-2.0-flash", "prompt": "会被限流", "size": "1024x1024"})
    doc = json.loads(body) if body else {}
    is_local_429 = st == 429 and doc.get("error", {}).get("type") == "rate_limit_local"
    check("5 全池不可用 → 本地 429 rate_limit_local", is_local_429, f"{st} {body[:120]}")
    flaky = [a for a in g.pool.accounts if a.label == "限流号"][0]
    check("5b 429 号进入 60s 冷却", flaky.cooldown_until - time.time() > 30,
          f"{round(flaky.cooldown_until - time.time())}s")

    # 6 RPM 上限：整个池换成 1 个好号，3K 档 RPM=1
    g.pool.remove(flaky.id)
    g.pool.set_enabled(good_id, True)
    g.pool.accounts[0].hits.clear()
    st1, _ = post(base + "/v1/images/generations", {
        "model": "agnes-image-2.0-flash", "prompt": "3K 第一次", "size": "3000x3000"})
    st2, b2 = post(base + "/v1/images/generations", {
        "model": "agnes-image-2.0-flash", "prompt": "3K 第二次", "size": "3000x3000"})
    check("6 3K 档 RPM=1 被本地拦截", st1 == 200 and st2 == 429,
          f"{st1}/{st2} {b2[:80]}")

    # 7 管理 API
    st, _ = get(base + "/admin/api/state")
    check("7a 无 token 被拒", st == 401, f"{st}")
    st, body = get(base + "/admin/api/state", token="test-token")
    check("7b 有 token 可读", st == 200 and "accounts" in json.loads(body), f"{st}")
    st, body = post(base + "/admin/api/accounts",
                    {"label": "新号", "key": "sk-good-2"}, token="test-token")
    new_id = json.loads(body)["account"]["id"] if st == 200 else ""
    check("7c 加账号", st == 200 and new_id.startswith("acc-"), f"{st} {new_id}")
    st, body = post(base + f"/admin/api/accounts/{new_id}/enable",
                    {"enabled": False}, token="test-token")
    check("7d 停用账号", st == 200 and json.loads(body)["ok"] is True, f"{st}")
    st, body = post(base + "/admin/api/probe", {}, token="test-token")
    probes = {a["label"]: a["probe"] for a in json.loads(body)["accounts"]} if st == 200 else {}
    check("7e 探活能认出好 key（不再用 /models 误判）",
          st == 200 and "有效" in probes.get("好号", ""), f"{st} {probes}")
    st, body = post(base + "/admin/api/accounts",
                    {"label": "坏key", "key": "sk-bad-401"}, token="test-token")
    bad_id = json.loads(body)["account"]["id"] if st == 200 else ""
    st, body = post(base + "/admin/api/probe", {}, token="test-token")
    probes2 = {a["label"]: a["probe"] for a in json.loads(body)["accounts"]} if st == 200 else {}
    check("7e2 探活能认出坏 key（/images 400 vs 401 判别）",
          "无效" in probes2.get("坏key", ""), f"{probes2.get('坏key', '')}")
    delete(base + f"/admin/api/accounts/{bad_id}", token="test-token")
    st, _ = delete(base + f"/admin/api/accounts/{new_id}", token="test-token")
    check("7f 删账号", st == 200, f"{st}")

    # 8 SSE 流
    req = urllib.request.Request(base + "/v1/chat/completions",
                                 data=json.dumps({"model": "agnes-2.5-flash",
                                                  "messages": [{"role": "user", "content": "hi"}],
                                                  "stream": True}).encode(),
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        stream_body = r.read().decode()
    check("8 文本 SSE 流透传", "data: [DONE]" in stream_body and stream_body.count("data:") >= 3,
          f"{len(stream_body)}B")

    # 9 4xx 不换号
    SEEN["keys"].clear()
    st, body = post(base + "/v1/images/generations", {"size": "1024x1024"})  # 缺 model
    check("9 上游 400 原样回传且不换号", st == 400 and len(SEEN["keys"]) == 1,
          f"{st} 调用次数={len(SEEN['keys'])}")

    # ---------------- 10~12：限流切号相关（各起一个独立网关） ---------------- #

    def new_gateway(tag, accounts, limits):
        d = Path(tempfile.mkdtemp(prefix=f"agnes2api_t_{tag}_"))
        cfg = d / "config.json"
        cfg.write_text(json.dumps({
            "host": "127.0.0.1", "port": 0, "upstream": upstream,
            "admin_token": "t", "accounts": accounts, "limits": limits,
        }, ensure_ascii=False), encoding="utf-8")
        gg = gw.Gateway(cfg, d / "stats.json")
        hh = gw.build_server(gg, "127.0.0.1", 0)
        threading.Thread(target=hh.serve_forever, daemon=True).start()
        return gg, hh, f"http://127.0.0.1:{hh.server_address[1]}"

    # 10 上游 429 → 同一个请求里换下一个号，客户端无感
    g10, h10, b10 = new_gateway("switch", [
        {"label": "限流号", "key": "sk-flaky-429", "enabled": True},
        {"label": "好号", "key": "sk-good-1", "enabled": True},
    ], {"text_rpm": 10, "image_rpm": {"1K": 10, "2K": 5, "3K": 1, "4K": 1},
        "video_rpm": 120, "rpm_window": 60, "wait_max_seconds": 0})
    SEEN["keys"].clear()
    st, body = post(b10 + "/v1/images/generations", {
        "model": "agnes-image-2.0-flash", "prompt": "限流后换号", "size": "1024x1024"})
    keys = SEEN["keys"][:2]
    flaky = [a for a in g10.pool.accounts if a.label == "限流号"][0]
    check("10 上游 429 → 本请求内切下一个号，客户端拿到 200",
          st == 200 and keys == ["sk-flaky-429", "sk-good-1"], f"{st} keys={keys}")
    check("10b 被限流的号凉 60s，后面不再被选",
          flaky.cooldown_until - time.time() > 50,
          f"{round(flaky.cooldown_until - time.time())}s")
    h10.shutdown()

    # 11 单号 rpm 覆盖：付费号写 "rpm": 3，不受 1K 档默认 1 的限制
    g11, h11, b11 = new_gateway("override", [
        {"label": "付费号", "key": "sk-good-1", "enabled": True, "rpm": 3},
    ], {"text_rpm": 10, "image_rpm": {"1K": 1, "2K": 1, "3K": 1, "4K": 1},
        "video_rpm": 120, "rpm_window": 60, "wait_max_seconds": 0})
    codes = [post(b11 + "/v1/images/generations",
                  {"model": "agnes-image-2.0-flash", "prompt": f"第{i}张", "size": "1024x1024"})[0]
             for i in range(4)]
    check("11 账号级 rpm 覆盖生效（1K 默认 1，付费号放到 3）",
          codes[:3] == [200, 200, 200] and codes[3] == 429, f"codes={codes}")
    check("11b 覆盖值写回 config.json 不丢",
          g11.pool.accounts[0].rpm_override == 3 and "rpm" in g11.pool.accounts[0].to_dict(),
          f"rpm={g11.pool.accounts[0].rpm_override}")
    h11.shutdown()

    # 12 只差一点点额度时短暂等待，而不是直接 429
    g12, h12, b12 = new_gateway("wait", [
        {"label": "唯一号", "key": "sk-good-1", "enabled": True},
    ], {"text_rpm": 10, "image_rpm": {"1K": 1, "2K": 1, "3K": 1, "4K": 1},
        "video_rpm": 120, "rpm_window": 1, "wait_max_seconds": 2})
    st_a, _ = post(b12 + "/v1/images/generations",
                   {"model": "agnes-image-2.0-flash", "prompt": "第一次", "size": "1024x1024"})
    t0 = time.time()
    st_b, b_b = post(b12 + "/v1/images/generations",
                     {"model": "agnes-image-2.0-flash", "prompt": "第二次", "size": "1024x1024"})
    waited = time.time() - t0
    check("12 窗口即将滑出时等一下就放行（不再直接 429）",
          st_a == 200 and st_b == 200 and waited >= 0.6,
          f"{st_a}/{st_b} 等待 {waited:.2f}s")
    h12.shutdown()

    # 13 首次运行：config.json 不存在时要自动生成（exe 冒烟抓到的 bug）
    d13 = Path(tempfile.mkdtemp(prefix="agnes2api_cfg_"))
    fresh = d13 / "config.json"
    try:
        cfgobj = gw.Config(fresh)
        check("13 首次运行自动生成 config.json（不抛 AttributeError）",
              fresh.exists() and bool(cfgobj.admin_token)
              and cfgobj.limits.get("rpm_window") == 60,
              f"exists={fresh.exists()} token={bool(cfgobj.admin_token)}")
    except Exception as exc:
        check("13 首次运行自动生成 config.json（不抛 AttributeError）", False,
              f"{type(exc).__name__}: {exc}")
    shutil.rmtree(d13, ignore_errors=True)

    # 14 没有任何可用账号时，/v1/models 回退到已知模型清单而不是空列表
    g14, h14, b14 = new_gateway("nomodels", [], {"text_rpm": 10,
                                                "image_rpm": {"1K": 10, "2K": 5, "3K": 1, "4K": 1},
                                                "video_rpm": 120, "rpm_window": 60,
                                                "wait_max_seconds": 0})
    st14, body14 = get(b14 + "/v1/models")
    try:
        ids = [m["id"] for m in json.loads(body14)["data"]]
    except Exception:
        ids = []
    check("14 无账号时 /v1/models 回退常量清单",
          st14 == 200 and len(ids) == len(gw.KNOWN_MODELS), f"{st14} n={len(ids)}")
    h14.shutdown()

    # ---------------- 15~18：生成记录（管理台回看图片） ---------------- #
    st, body = post(base + "/v1/images/generations", {
        "model": "agnes-image-2.0-flash", "prompt": "记录测试-外链", "size": "1024x1024"})
    st2, gbody = get(base + "/admin/api/gallery", token="test-token")
    gdoc = json.loads(gbody) if st2 == 200 else {}
    items = gdoc.get("items") or []
    top = items[0] if items else {}
    check("15 出图后管理台能看到生成记录（url 分支也记元数据）",
          st == 200 and gdoc.get("count", 0) >= 1 and top.get("prompt") == "记录测试-外链"
          and top.get("url", "").startswith("https://fake.example/"),
          f"{st} count={gdoc.get('count')} top={top.get('prompt')} file={top.get('file')!r}")

    st, body = post(base + "/v1/images/generations", {
        "model": "agnes-image-b64-test", "prompt": "记录测试-base64", "size": "1024x1024"})
    st2, gbody = get(base + "/admin/api/gallery", token="test-token")
    items = json.loads(gbody).get("items") or [] if st2 == 200 else []
    top = items[0] if items else {}
    fpath = Path(g.gallery.root) / top.get("file", "")
    b64_ok = bool(top.get("file")) and fpath.is_file() and fpath.stat().st_size > 0
    st3, blob, ctype = raw_get(base + f"/admin/api/gallery/file/{top.get('file','')}"
                               f"?token=test-token")
    check("16 base64 出图落成真文件、管理台能按 token 取回",
          st == 200 and b64_ok and st3 == 200 and blob.startswith(b"\x89PNG")
          and ctype.startswith("image/png"),
          f"file={top.get('file')} exists={b64_ok} http={st3} {ctype} {len(blob)}B")

    st4, _ = delete(base + f"/admin/api/gallery/{top.get('id','')}", token="test-token")
    check("17 删除单条记录会连图片文件一起删",
          st4 == 200 and not fpath.exists(), f"{st4} exists={fpath.exists()}")

    st, body = post(base + "/admin/api/gallery/clear", {}, token="test-token")
    check("18 清空全部记录", st == 200 and g.gallery.count() == 0,
          f"{st} count={g.gallery.count()}")

    # ---------------- 19~20：按账号指定上游（国内版 / 国际版） ---------------- #

    class OtherUpstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.split("?")[0] == "/v1/models":
                b = json.dumps({"object": "list",
                                "data": [{"id": "from-cn-upstream"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    other = ThreadingHTTPServer(("127.0.0.1", 0), OtherUpstream)
    other.daemon_threads = True
    threading.Thread(target=other.serve_forever, daemon=True).start()
    other_base = f"http://127.0.0.1:{other.server_address[1]}/v1"

    g19, h19, b19 = new_gateway("base", [
        {"label": "国内号", "key": "sk-cn-1", "enabled": True, "base": other_base},
        {"label": "国际号", "key": "sk-intl-1", "enabled": True},
    ], {"text_rpm": 10, "image_rpm": {"1K": 10, "2K": 5, "3K": 1, "4K": 1},
        "video_rpm": 120, "rpm_window": 60, "wait_max_seconds": 0})
    st19, body19 = get(b19 + "/v1/models")
    ids19 = []
    try:
        ids19 = [m["id"] for m in json.loads(body19)["data"]]
    except Exception:
        pass
    check("19 账号自带 base 时请求真的打到那个上游",
          st19 == 200 and ids19 == ["from-cn-upstream"], f"{st19} {ids19}")
    check("19b 账号 base 会写进 config.json，重启后还在",
          g19.pool.accounts[0].to_dict().get("base") == other_base,
          f"{g19.pool.accounts[0].to_dict().get('base')}")
    h19.shutdown()
    other.shutdown()

    check("20 base 简称归一：cn/国内→api.agnes-ai.cn，com→apihub.agnes-ai.com，空→None",
          gw.normalize_base("cn") == "https://api.agnes-ai.cn/v1"
          and gw.normalize_base("国内版") == "https://api.agnes-ai.cn/v1"
          and gw.normalize_base("com") == gw.DEFAULT_UPSTREAM
          and gw.normalize_base("") is None
          and gw.normalize_base(" https://x.cn/v1/ ") == "https://x.cn/v1",
          f"{gw.normalize_base('cn')} / {gw.normalize_base('')}")

    # ---------------- 21~22：只填 Key，自动识别上游 ---------------- #

    class CnUpstream(BaseHTTPRequestHandler):
        """只认 sk-cn-only 的「国内站」假上游：其它 key 一律 401。"""
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, doc=None):
            b = json.dumps(doc).encode() if doc is not None else b""
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            auth = self.headers.get("Authorization") or ""
            if auth.replace("Bearer ", "") != "sk-cn-only":
                return self._send(401, {"error": {"message": "无效的令牌"}})
            self._send(404, {"error": {"message": "当前分组不支持模型"}})

    cn = ThreadingHTTPServer(("127.0.0.1", 0), CnUpstream)
    cn.daemon_threads = True
    threading.Thread(target=cn.serve_forever, daemon=True).start()
    cn_base = f"http://127.0.0.1:{cn.server_address[1]}/v1"
    BAD.add("sk-cn-only")

    g21, h21, b21 = new_gateway("auto", [], {
        "text_rpm": 10, "image_rpm": {"1K": 10, "2K": 5, "3K": 1, "4K": 1},
        "video_rpm": 120, "rpm_window": 60, "wait_max_seconds": 0})
    g21.base_candidates = [upstream, cn_base]     # 先国际站（401）→ 再国内站（认）
    st21, body21 = post(b21 + "/admin/api/accounts",
                        {"label": "只填 Key 的号", "key": "sk-cn-only"}, token="t")
    made = g21.pool.accounts[0] if g21.pool.accounts else None
    check("21 只填 key：自动识别出该走国内站并落库",
          st21 == 200 and made is not None and made.base == cn_base,
          f"{st21} base={getattr(made, 'base', None)}")

    st21b, body21b = post(b21 + "/admin/api/accounts",
                          {"label": "坏号", "key": "sk-bad-401"}, token="t")
    check("21b 两站都不认的 key → 400 且不加进池子",
          st21b == 400 and len(g21.pool.accounts) == 1, f"{st21b}")

    # 22 探活时发现站不对 → 自动纠正并写盘
    g21.pool.accounts[0].base = upstream
    g21.pool.persist()
    st22, body22 = post(b21 + "/admin/api/probe", {}, token="t")
    acc22 = (json.loads(body22).get("accounts") or [{}])[0] if st22 == 200 else {}
    saved = json.loads(Path(g21.cfg.path).read_text(encoding="utf-8"))
    check("22 探活发现上游选错 → 自动换到认这把 key 的站并保存",
          st22 == 200 and acc22.get("fixed_base") == cn_base
          and (saved.get("accounts") or [{}])[0].get("base") == cn_base,
          f"{st22} {acc22.get('fixed_base')} saved={(saved.get('accounts') or [{}])[0].get('base')}")
    h21.shutdown()
    cn.shutdown()
    BAD.discard("sk-cn-only")

    # 汇总
    ok = sum(1 for _, o, _ in RESULTS if o)
    total = len(RESULTS)
    print(f"\n{'='*56}\n{ok}/{total} 通过")
    for name, passed, detail in RESULTS:
        if not passed:
            print(f"  ✗ {name}  {detail}")
    httpd.shutdown()
    fake.shutdown()
    shutil.rmtree(workdir, ignore_errors=True)
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
