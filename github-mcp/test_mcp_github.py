#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcp_github.py 的离线回归测试：起一个假的 GitHub API，再用 JSON-RPC 驱动 MCP 服务。

跑法：  python test_mcp_github.py
不联网、不需要真令牌。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SERVER = HERE / "mcp_github.py"


# ------------------------------------------------------------------ 假 GitHub

class State:
    def __init__(self) -> None:
        self.repos: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict | None]] = []
        self.seq = 0

    def next(self, kind: str) -> str:
        self.seq += 1
        return f"{kind}{self.seq}"


STATE = State()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:  # 静音
        pass

    # -- 工具 --------------------------------------------------------
    def _send(self, status: int, payload: Any, extra: dict | None = None) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _record(self, method: str, body: dict | None) -> None:
        STATE.calls.append((method, self.path, body))

    # -- 路由 --------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self._record("GET", None)
        path = self.path.split("?")[0]
        if path == "/user":
            if not self.headers.get("Authorization"):
                self._send(401, {"message": "Requires authentication"})
                return
            self._send(200, {"login": "test-user", "name": "Tester"},
                       {"X-OAuth-Scopes": "repo, read:org"})
            return
        if path == "/rate_limit":
            self._send(200, {"resources": {"core": {"remaining": 4999, "limit": 5000}}})
            return
        if path == "/user/repos":
            self._send(200, list(STATE.repos.values()))
            return
        if path.startswith("/repos/"):
            parts = path.split("/")
            full = "/".join(parts[2:4])
            repo = STATE.repos.get(full)
            if repo is None:
                self._send(404, {"message": "Not Found"})
                return
            if len(parts) == 4:
                self._send(200, repo)
                return
            if parts[4:6] == ["git", "ref"]:
                # 客户端读的是 /git/ref/heads/<branch>（单数）
                if repo.get("empty409"):
                    self._send(409, {"message": "Git Repository is empty."})
                    return
                sha = repo.get("ref")
                if not sha or repo.get("ref_branch", "main") != parts[-1]:
                    self._send(404, {"message": "Not Found"})
                    return
                self._send(200, {"object": {"sha": sha}})
                return
            if parts[4:6] == ["git", "refs"] and len(parts) >= 6:
                sha = repo.get("ref")
                if not sha:
                    self._send(404, {"message": "Not Found"})
                    return
                self._send(200, {"object": {"sha": sha}})
                return
            if parts[4:6] == ["git", "commits"] and len(parts) >= 7:
                self._send(200, {"sha": parts[6], "tree": {"sha": "tree-of-" + parts[6]}})
                return
            self._send(404, {"message": "Not Found"})
            return
        self._send(404, {"message": "Not Found"})

    def do_POST(self) -> None:  # noqa: N802
        body = self._body()
        self._record("POST", body)
        path = self.path.split("?")[0]
        if path == "/user/repos":
            full = f"test-user/{body['name']}"
            repo = {
                "full_name": full, "name": body["name"], "private": body["private"],
                "default_branch": "main", "size": 0, "html_url": f"https://github.com/{full}",
                "stargazers_count": 0, "pushed_at": "2026-10-20T00:00:00Z",
            }
            STATE.repos[full] = repo
            self._send(201, repo)
            return
        parts = path.split("/")
        full = "/".join(parts[2:4])
        repo = STATE.repos.get(full)
        if repo is None:
            self._send(404, {"message": "Not Found"})
            return
        if parts[4:7] == ["git", "blobs"]:
            # 真实 GitHub 对「还没有任何提交」的仓库建 blob 会回 409
            if not repo.get("ref"):
                self._send(409, {"message": "Git Repository is empty."})
                return
            self._send(201, {"sha": STATE.next("blob")})
            return
        if parts[4:7] == ["git", "trees"]:
            self._send(201, {"sha": STATE.next("tree")})
            return
        if parts[4:7] == ["git", "commits"]:
            self._send(201, {"sha": STATE.next("commit")})
            return
        if parts[4:7] == ["git", "refs"]:
            repo["ref"] = body["sha"]
            repo["ref_branch"] = body["ref"].split("/")[-1]
            self._send(201, {"ref": body["ref"], "object": {"sha": body["sha"]}})
            return
        self._send(404, {"message": "Not Found"})

    def do_PUT(self) -> None:  # noqa: N802
        body = self._body()
        self._record("PUT", body)
        path = self.path.split("?")[0]
        parts = path.split("/")
        full = "/".join(parts[2:4])
        repo = STATE.repos.get(full)
        if repo is None:
            self._send(404, {"message": "Not Found"})
            return
        if len(parts) >= 6 and parts[4] == "contents":
            # Contents API 建首个提交：真实 GitHub 会顺手把默认分支的 ref 建出来
            sha = STATE.next("boot")
            repo["ref"] = sha
            repo["ref_branch"] = "main"
            self._send(201, {"commit": {"sha": sha}, "content": {"path": "/".join(parts[6:])}})
            return
        self._send(404, {"message": "Not Found"})

    def do_PATCH(self) -> None:  # noqa: N802
        body = self._body()
        self._record("PATCH", body)
        path = self.path.split("?")[0]
        parts = path.split("/")
        full = "/".join(parts[2:4])
        repo = STATE.repos.get(full)
        if repo is None:
            self._send(404, {"message": "Not Found"})
            return
        if parts[4:6] == ["git", "refs"]:
            repo["ref"] = body["sha"]
            repo["ref_branch"] = parts[-1]
            self._send(200, {"object": {"sha": body["sha"]}})
            return
        self._send(404, {"message": "Not Found"})


# ------------------------------------------------------------------ MCP 驱动

class Mcp:
    def __init__(self, api: str, token: str = "ghp_fake", env_extra: dict | None = None) -> None:
        cmd = [sys.executable, str(SERVER), "--api", api]
        if token:
            cmd += ["--token", token]
        env = dict(os.environ)
        env.pop("GITHUB_TOKEN", None)
        env.pop("GH_TOKEN", None)
        if env_extra:
            env.update(env_extra)
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, env=env,
        )
        self.id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self.id += 1
        msg = {"jsonrpc": "2.0", "id": self.id, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            err = self.proc.stderr.read()
            raise RuntimeError(f"MCP 服务没有响应；stderr:\n{err}")
        return json.loads(line)

    def tool(self, name: str, args: dict) -> tuple[str, bool]:
        reply = self.call("tools/call", {"name": name, "arguments": args})
        result = reply.get("result") or {}
        content = result.get("content") or []
        text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        return text, bool(result.get("isError"))

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.proc.kill()


# ------------------------------------------------------------------ 用例

PASS = 0
FAIL = 0


def check(cond: bool, label: str, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))


def main() -> int:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    api = f"http://127.0.0.1:{port}"
    print(f"假 GitHub API: {api}")
    # 预置一个「刚建好、内容为空」的仓库：真实 GitHub 对它读 ref 会返回 409
    STATE.repos["test-user/fresh-repo"] = {
        "full_name": "test-user/fresh-repo", "name": "fresh-repo", "private": True,
        "default_branch": "main", "size": 0,
        "html_url": "https://github.com/test-user/fresh-repo",
        "stargazers_count": 0, "pushed_at": "2026-10-20T00:00:00Z", "empty409": True,
    }

    m = Mcp(api)
    try:
        print("[1] 握手")
        r = m.call("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                  "clientInfo": {"name": "test", "version": "1"}})
        res = r.get("result") or {}
        check(res.get("protocolVersion") == "2025-11-25", "回显协议版本")
        check((res.get("serverInfo") or {}).get("name") == "mcp-github", "服务名 mcp-github")

        print("[2] tools/list")
        r = m.call("tools/list", {})
        names = [t["name"] for t in (r.get("result") or {}).get("tools", [])]
        check(len(names) == 5, f"5 个工具（实际 {len(names)}）", str(names))
        check("github_push_files" in names, "含 github_push_files")

        print("[3] github_status（有令牌）")
        text, err = m.tool("github_status", {})
        check(not err, "status 不报错", text)
        check("test-user" in text, "识别出账号", text)
        check("repo, read:org" in text, "作用域可见", text)
        check("4999" in text, "配额可见", text)

        print("[4] github_create_repo")
        text, err = m.tool("github_create_repo", {"name": "demo-repo", "private": True})
        check(not err, "建仓库成功", text)
        check("test-user/demo-repo" in text, "返回 full_name", text)
        check("test-user/demo-repo" in STATE.repos, "假服务端已记录仓库")

        print("[5] github_push_files（空仓库 → Contents API 建首提交，其余走 Git Data）")
        STATE.calls.clear()
        text, err = m.tool("github_push_files", {
            "repo": "test-user/demo-repo",
            "message": "initial commit",
            "files": [
                {"path": "README.md", "content": "# demo\n"},
                {"path": "src/main.py", "content": "print('hi')\n"},
            ],
        })
        check(not err, "推送成功", text)
        check("test-user/demo-repo" in text and "main" in text, "回复含仓库与分支", text)
        puts = [c for c in STATE.calls if c[0] == "PUT"]
        check(len(puts) == 1 and puts[0][1].endswith("/contents/README.md"),
              "空仓库先用 Contents API 写首个文件", str([c[1] for c in puts]))
        posts = [c for c in STATE.calls if c[0] == "POST"]
        blobs = [c for c in posts if c[1].endswith("/git/blobs")]
        check(len(blobs) == 1, f"剩下的文件才走 blob（实际 {len(blobs)}）")
        trees = [c for c in posts if c[1].endswith("/git/trees")]
        check(bool(trees) and (trees[0][2] or {}).get("base_tree", "").startswith("tree-of-"),
              "初始化后带 base_tree")
        commits = [c for c in posts if c[1].endswith("/git/commits")]
        check(bool(commits) and len(commits[0][2]["parents"]) == 1, "第二个提交以初始化提交为父")
        check(any(c[0] == "PATCH" for c in STATE.calls), "已存在的分支走 PATCH")
        check(STATE.repos["test-user/demo-repo"].get("ref"), "假仓库已有 ref")

        print("[6] github_push_files（已有提交，增量）")
        STATE.calls.clear()
        text, err = m.tool("github_push_files", {
            "repo": "test-user/demo-repo", "message": "add one file",
            "files": [{"path": "docs/x.txt", "content": "x"}],
        })
        check(not err, "增量推送成功", text)
        trees = [c for c in STATE.calls if c[0] == "POST" and c[1].endswith("/git/trees")]
        check(bool(trees) and (trees[0][2] or {}).get("base_tree", "").startswith("tree-of-"),
              "增量提交带 base_tree", json.dumps((trees[0][2] if trees else {}))[:200])
        patches = [c for c in STATE.calls if c[0] == "PATCH"]
        check(bool(patches), "已存在分支走 PATCH 更新 ref")

        print("[7] 新分支")
        STATE.calls.clear()
        text, err = m.tool("github_push_files", {
            "repo": "test-user/demo-repo", "branch": "feature/x", "message": "feat",
            "files": [{"path": "a.txt", "content": "a"}],
        })
        check(not err, "新分支推送成功", text)
        check(any(c[1].endswith("/git/refs") for c in STATE.calls if c[0] == "POST"), "为新分支建 ref")

        print("[8] 错误路径")
        text, err = m.tool("github_push_files", {"repo": "test-user/nope",
                                                 "files": [{"path": "a", "content": "b"}]})
        check(err and "不存在" in text, "仓库不存在给出提示", text)
        text, err = m.tool("github_push_files", {"repo": "bad-format", "files": []})
        check(err, "repo 格式错误被拒", text)
        text, err = m.tool("github_push_files", {"repo": "test-user/demo-repo", "files": [{"path": ""}]})
        check(err, "缺 path 被拒", text)
        r = m.call("tools/call", {"name": "no_such_tool", "arguments": {}})
        check((r.get("error") or {}).get("code") == -32602, "未知工具返回 -32602")

        print("[9] 无令牌")
        # 用临时 HOME 隔离：否则会读到真实 ~/.dsh/.credentials.yaml 里的 GITHUB_TOKEN
        with tempfile.TemporaryDirectory() as fake_home:
            m2 = Mcp(api, token="", env_extra={"HOME": fake_home, "USERPROFILE": fake_home})
            try:
                text, err = m2.tool("github_status", {})
                check(err and ("令牌无效" in text or "网络不通" in text or "HTTP 401" in text),
                      "无令牌时提示重新授权", text[:200])
            finally:
                m2.close()

        print("[10] 目录/文件读取")
        text, err = m.tool("github_get_file", {"repo": "test-user/demo-repo", "path": "README.md"})
        check(err, "内容接口未实现时给出可读错误", text[:120])

        print("[11] 空仓库读 ref 返回 409 的情形")
        STATE.calls.clear()
        text, err = m.tool("github_push_files", {
            "repo": "test-user/fresh-repo", "message": "first commit",
            "files": [{"path": "README.md", "content": "# fresh\n"}],
        })
        check(not err, "409 空仓库也能首提交成功", text[:200])
        check(any(c[0] == "PUT" for c in STATE.calls), "走 Contents API 初始化")
        check(STATE.repos["test-user/fresh-repo"].get("ref"), "已有 ref")
    finally:
        m.close()
        httpd.shutdown()

    print(f"\n{PASS}/{PASS + FAIL} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
