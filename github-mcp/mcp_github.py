#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub MCP 服务（stdio，纯标准库，零第三方依赖）。

设计目标：让 DSH 里的模型能直接把文件推到 GitHub 仓库。
不走 GitHub 的远程 MCP 端点（那个只认静态 header、而且令牌类型受限），
这里直接用 GitHub REST API 实现最小可用工具集，令牌可以是 PAT 也可以是
gh CLI 的 OAuth 令牌（gho_ 开头，REST API 同样接受）。

令牌查找顺序（先命中先用）：
    1. --token
    2. 环境变量 GITHUB_TOKEN
    3. 环境变量 GH_TOKEN
    4. --token-file 指定的文件（整行即令牌）
    5. ~/.dsh/.credentials.yaml 的 refs.GITHUB_TOKEN
    6. 本机 gh CLI（gh auth token）

协议：JSON-RPC 2.0，一行一条，UTF-8 编码，走 stdin/stdout；日志一律走 stderr。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SERVER_NAME = "mcp-github"
SERVER_VERSION = "1.0.7"
SUPPORTED_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2024-11-05"]
DEFAULT_API = "https://api.github.com"
API_VERSION = "2022-11-28"
MAX_BLOB = 20 * 1024 * 1024  # 单文件上限，GitHub 允许更大，这里保守
MAX_FILES = 100

HOME = Path(os.environ.get("USERPROFILE") or Path.home())
CREDENTIALS = HOME / ".dsh" / ".credentials.yaml"


def log(msg: str) -> None:
    print(f"[{SERVER_NAME}] {msg}", file=sys.stderr, flush=True)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} MB"


# ------------------------------------------------------------------ 令牌

def token_from_credentials(path: Path = CREDENTIALS) -> str:
    """从 DSH 凭据中心的 refs 段取 GITHUB_TOKEN（不引第三方 yaml）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    in_refs = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if re.match(r"^refs:\s*$", line):
            in_refs = True
            continue
        if in_refs and re.match(r"^\S", line):  # 回到顶格：refs 段结束
            break
        if not in_refs:
            continue
        m = re.match(r"^\s+GITHUB_TOKEN:\s*['\"]?([^'\"\s]+)['\"]?\s*$", line)
        if m:
            return m.group(1)
    return ""


def token_from_gh() -> str:
    """本机装了 gh 且已登录时，直接借它的令牌。"""
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    tok = (out.stdout or "").strip()
    return tok if out.returncode == 0 and tok else ""


def resolve_token(cli_token: str = "", token_file: str = "") -> tuple[str, str]:
    if cli_token:
        return cli_token, "--token"
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        val = os.environ.get(name, "").strip()
        if val:
            return val, f"环境变量 {name}"
    if token_file:
        try:
            val = Path(token_file).read_text(encoding="utf-8").strip().splitlines()[0].strip()
            if val:
                return val, f"文件 {token_file}"
        except (OSError, IndexError):
            pass
    val = token_from_credentials()
    if val:
        return val, f"{CREDENTIALS}（refs.GITHUB_TOKEN）"
    val = token_from_gh()
    if val:
        return val, "本机 gh CLI"
    return "", "未找到"


# ------------------------------------------------------------------ HTTP

class GitHubError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}：{message}")
        self.status = status
        self.message = message


def encode_content(content: str, encoding: str = "utf-8") -> bytes:
    """把 files[].content 按 encoding 变成字节（utf-8 / base64 / path=读本地文件）。"""
    enc = (encoding or "utf-8").lower()
    if enc == "base64":
        raw = base64.b64decode(content)
    elif enc == "path":
        raw = Path(content).read_bytes()
    else:
        raw = str(content).encode("utf-8")
    if len(raw) > MAX_BLOB:
        raise GitHubError(0, f"文件超过 {MAX_BLOB // 1024 // 1024} MB 上限")
    return raw


class GitHub:
    def __init__(self, token: str, api: str = DEFAULT_API, timeout: float = 30.0):
        self.token = token
        self.api = api.rstrip("/")
        self.timeout = timeout

    # -- 底层 --------------------------------------------------------
    def _request(self, method: str, path: str, body: Any = None,
                 tries: int = 4) -> tuple[int, Any, dict]:
        """发一次 API 请求。网络层错误（断连 / SSL EOF / 超时）自动重试。

        本机到 `api.github.com` 实测偶发 `SSL: UNEXPECTED_EOF_WHILE_READING`
        （2026-10-20 一次推送连撞三次），GITHUB 又是「一次推送十几个请求」，
        所以重试放在单个请求这一层，而不是整次推送重来。
        """
        url = path if path.startswith("http") else self.api + path
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "dsh-mcp-github/" + SERVER_VERSION,
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status = 0
        raw = b""
        hdrs: dict = {}
        for attempt in range(1, tries + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    hdrs = dict(resp.headers)
                    status = resp.status
                break
            except urllib.error.HTTPError as exc:
                raw = exc.read() or b""
                hdrs = dict(exc.headers or {})
                status = exc.code
                break
            except (urllib.error.URLError, OSError) as exc:
                if attempt == tries:
                    reason = getattr(exc, "reason", exc)
                    raise GitHubError(0, f"网络不通（已重试 {tries} 次）：{reason}") from exc
                time.sleep(min(2 ** (attempt - 1), 4))
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = raw.decode("utf-8", "replace")
        return status, parsed, hdrs

    def call(self, method: str, path: str, body: Any = None) -> Any:
        status, data, _ = self._request(method, path, body)
        if status >= 400:
            msg = data.get("message") if isinstance(data, dict) else str(data)[:300]
            raise GitHubError(status, str(msg))
        return data

    # -- 语义封装 ----------------------------------------------------
    def user(self) -> dict:
        return self.call("GET", "/user")

    def scopes(self) -> str:
        status, _, hdrs = self._request("GET", "/user")
        return hdrs.get("X-OAuth-Scopes", "") or ""

    def rate(self) -> dict:
        return self.call("GET", "/rate_limit")

    def get_repo(self, full: str) -> dict:
        return self.call("GET", f"/repos/{full}")

    def create_repo(self, name: str, private: bool, description: str = "") -> dict:
        body = {"name": name, "private": bool(private)}
        if description:
            body["description"] = description
        # 组织仓库要换端点，这里按个人账号走；name 里带斜杠则视为 org/name
        if "/" in name:
            org, repo_name = name.split("/", 1)
            body["name"] = repo_name
            return self.call("POST", f"/orgs/{org}/repos", body)
        return self.call("POST", "/user/repos", body)

    def ref_sha(self, full: str, branch: str) -> str | None:
        status, data, _ = self._request("GET", f"/repos/{full}/git/ref/heads/{urllib.parse.quote(branch)}")
        # 404 = 分支不存在；409 = 仓库还空着（Git Repository is empty），两者都当「没有 ref」
        if status in (404, 409):
            return None
        if status >= 400:
            raise GitHubError(status, str((data or {}).get("message", "")))
        return (data or {}).get("object", {}).get("sha")

    def commit_tree_sha(self, full: str, commit_sha: str) -> str:
        commit = self.call("GET", f"/repos/{full}/git/commits/{commit_sha}")
        return commit["tree"]["sha"]

    def blob(self, full: str, content: str, encoding: str = "utf-8") -> str:
        raw = encode_content(content, encoding)
        payload = {"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"}
        blob = self.call("POST", f"/repos/{full}/git/blobs", payload)
        return blob["sha"]

    def put_file(self, full: str, path: str, content_b64: str, message: str, branch: str = "") -> dict:
        """用 Contents API 写单个文件（空仓库建首个提交只能用这条路）。"""
        body: dict[str, Any] = {"message": message, "content": content_b64}
        if branch:
            body["branch"] = branch
        return self.call("PUT", f"/repos/{full}/contents/{urllib.parse.quote(path)}", body)

    def tree(self, full: str, entries: list[dict], base_tree: str | None = None) -> str:
        body: dict[str, Any] = {"tree": entries}
        if base_tree:
            body["base_tree"] = base_tree
        return self.call("POST", f"/repos/{full}/git/trees", body)["sha"]

    def commit(self, full: str, message: str, tree_sha: str, parents: list[str]) -> str:
        body = {"message": message, "tree": tree_sha, "parents": parents}
        return self.call("POST", f"/repos/{full}/git/commits", body)["sha"]

    def set_ref(self, full: str, branch: str, sha: str, exists: bool, force: bool = False) -> Any:
        if exists:
            body = {"sha": sha, "force": bool(force)}
            return self.call("PATCH", f"/repos/{full}/git/refs/heads/{urllib.parse.quote(branch)}", body)
        body = {"ref": f"refs/heads/{branch}", "sha": sha}
        return self.call("POST", f"/repos/{full}/git/refs", body)

    def contents(self, full: str, path: str, ref: str = "") -> dict:
        suffix = f"?ref={urllib.parse.quote(ref)}" if ref else ""
        return self.call("GET", f"/repos/{full}/contents/{urllib.parse.quote(path)}{suffix}")

    def list_repos(self, limit: int = 10) -> list[dict]:
        return self.call("GET", f"/user/repos?per_page={max(1, min(limit, 100))}&sort=updated")


# ------------------------------------------------------------------ 工具实现

def _text(s: str, error: bool = False) -> tuple[list[dict], bool]:
    return [{"type": "text", "text": s}], error


def tool_status(gh: GitHub, args: dict) -> tuple[list[dict], bool]:
    src = args.get("_token_source") or "未知"
    try:
        me = gh.user()
    except GitHubError as exc:
        hint = ""
        if exc.status in (401, 403):
            hint = "\n令牌无效或已过期：重新 `gh auth login --web`，或把新 PAT 写进 ~/.dsh/.credentials.yaml 的 refs.GITHUB_TOKEN。"
        return _text(f"令牌来源：{src}\n调用 GitHub 失败 → {exc}{hint}", True)
    lines = [
        f"令牌来源：{src}",
        f"登录账号：{me.get('login')}（{me.get('name') or '未设置姓名'}）",
        f"令牌作用域：{gh.scopes() or '（未返回，PAT 细粒度令牌通常不暴露）'}",
    ]
    try:
        rl = gh.rate()
        core = (rl.get("resources") or {}).get("core") or {}
        lines.append(f"API 配额：{core.get('remaining')}/{core.get('limit')}（每 60 分钟）")
    except GitHubError:
        pass
    return _text("\n".join(lines))


def tool_push(gh: GitHub, args: dict) -> tuple[list[dict], bool]:
    repo = str(args.get("repo") or "").strip()
    files = args.get("files") or []
    message = str(args.get("message") or "通过 DSH 推送").strip()
    branch_arg = str(args.get("branch") or "").strip()
    if not repo or "/" not in repo:
        return _text("参数 repo 要写成 owner/仓库名", True)
    if not isinstance(files, list) or not files:
        return _text("参数 files 至少给一项：{path, content, encoding?}", True)
    if len(files) > MAX_FILES:
        return _text(f"一次最多推 {MAX_FILES} 个文件，当前 {len(files)} 个", True)

    try:
        info = gh.get_repo(repo)
    except GitHubError as exc:
        if exc.status == 404:
            return _text(f"仓库 {repo} 不存在（或令牌无权访问）。可以先调 github_create_repo 建一个。", True)
        raise
    default_branch = info.get("default_branch") or "main"
    branch = branch_arg or default_branch
    empty = not info.get("size")
    ref = gh.ref_sha(repo, branch)

    # 空仓库：POST /git/blobs 会直接 409「Git Repository is empty」，
    # 只能先用 Contents API 写一个文件把仓库「激活」出首个提交，剩下的再走 Git Data API。
    boot_commit = ""
    if ref is None and files:
        first = files[0]
        if not isinstance(first, dict):
            return _text("files 里每一项都要是对象：{path, content, encoding?}", True)
        first_path = str(first.get("path") or "").lstrip("/")
        if not first_path:
            return _text("files 里有一项缺少 path", True)
        if first.get("content") is None:
            return _text(f"{first_path} 缺少 content", True)
        raw = encode_content(str(first.get("content")), str(first.get("encoding") or "utf-8"))
        try:
            put = gh.put_file(repo, first_path, base64.b64encode(raw).decode("ascii"),
                              f"{message}（初始化仓库）", default_branch)
        except GitHubError as exc:
            return _text(f"空仓库初始化失败：{exc}", True)
        boot_commit = str((put.get("commit") or {}).get("sha") or "")
        ref = gh.ref_sha(repo, default_branch)
        files = files[1:]
        if branch != default_branch and ref:
            gh.set_ref(repo, branch, ref, exists=False)
            ref = gh.ref_sha(repo, branch)
        if not files:
            total0 = len(raw)
            return _text(
                f"已推送到 {repo}（分支 {default_branch}）\n"
                f"提交：{boot_commit[:12]}　文件 1 个　{total0} 字节\n"
                f"链接：https://github.com/{repo}/commit/{boot_commit}"
            )

    base_tree = gh.commit_tree_sha(repo, ref) if ref else None

    entries = []
    sizes: list[int] = [len(raw)] if boot_commit else []
    for item in files:
        if not isinstance(item, dict):
            return _text("files 里每一项都要是对象：{path, content, encoding?}", True)
        path = str(item.get("path") or "").lstrip("/")
        if not path:
            return _text("files 里有一项缺少 path", True)
        content = item.get("content")
        if content is None:
            return _text(f"{path} 缺少 content", True)
        enc = str(item.get("encoding") or "utf-8").lower()
        raw = encode_content(str(content), enc)
        sha = gh.blob(repo, base64.b64encode(raw).decode("ascii"), "base64")
        mode = str(item.get("mode") or "100644")
        entries.append({"path": path, "mode": mode, "type": "blob", "sha": sha})
        sizes.append(len(raw))

    tree_sha = gh.tree(repo, entries, base_tree)
    commit_sha = gh.commit(repo, message, tree_sha, [ref] if ref else [])
    gh.set_ref(repo, branch, commit_sha, exists=bool(ref), force=bool(args.get("force")))

    total = sum(sizes)
    lines = [
        f"已推送到 {repo}（分支 {branch}）",
        f"提交：{commit_sha[:12]}　文件 {len(entries) + (1 if boot_commit else 0)} 个　{human_size(total)}",
        f"链接：https://github.com/{repo}/commit/{commit_sha}",
    ]
    if boot_commit:
        lines.append(f"空仓库已自动初始化（首个提交 {boot_commit[:12]}），本次为第二个提交。")
    elif empty and not ref:
        lines.append("提示：这是仓库的第一个提交。")
    return _text("\n".join(lines))


def tool_create_repo(gh: GitHub, args: dict) -> tuple[list[dict], bool]:
    name = str(args.get("name") or "").strip()
    if not name:
        return _text("参数 name 必填（可以是 仓库名 或 org/仓库名）", True)
    private = bool(args.get("private", True))
    desc = str(args.get("description") or "")
    try:
        repo = gh.create_repo(name, private, desc)
    except GitHubError as exc:
        if exc.status == 422:
            return _text(f"创建失败：{exc}（同名仓库可能已存在，或名字不符合规则）", True)
        return _text(f"创建失败：{exc}", True)
    return _text(
        f"已创建{'私有' if private else '公开'}仓库 {repo.get('full_name')}\n"
        f"地址：{repo.get('html_url')}\n默认分支：{repo.get('default_branch') or '（空仓库，首个提交后确定）'}"
    )


def tool_get_file(gh: GitHub, args: dict) -> tuple[list[dict], bool]:
    repo = str(args.get("repo") or "").strip()
    # path 允许空串：表示仓库根目录，返回顶层条目列表
    path = str(args.get("path") or "").strip()
    if not repo:
        return _text("参数 repo（owner/name）必填；path 留空表示仓库根目录", True)
    try:
        data = gh.contents(repo, path, str(args.get("ref") or ""))
    except GitHubError as exc:
        return _text(f"读取失败：{exc}", True)
    if isinstance(data, list):
        names = "\n".join(f"- {x.get('type')} {x.get('path')}" for x in data[:200])
        return _text(f"{repo} 下 {path or '/'} 是目录，共 {len(data)} 项：\n{names}")
    raw = base64.b64decode(data.get("content") or "").decode("utf-8", "replace")
    if len(raw) > 20000:
        raw = raw[:20000] + "\n…（已截断）"
    return _text(f"{repo}:{path}（{data.get('size')} 字节，sha {str(data.get('sha'))[:8]}）\n\n{raw}")


def tool_list_repos(gh: GitHub, args: dict) -> tuple[list[dict], bool]:
    try:
        repos = gh.list_repos(int(args.get("limit") or 10))
    except GitHubError as exc:
        return _text(f"列出仓库失败：{exc}", True)
    if not repos:
        return _text("这个账号下没看到仓库")
    lines = [
        f"{'私有' if r.get('private') else '公开'}  {r.get('full_name')}  "
        f"默认分支 {r.get('default_branch') or '-'}  ★{r.get('stargazers_count')}  "
        f"更新 {str(r.get('pushed_at'))[:10]}"
        for r in repos
    ]
    return _text("\n".join(lines))


HANDLERS = {
    "github_status": tool_status,
    "github_push_files": tool_push,
    "github_create_repo": tool_create_repo,
    "github_get_file": tool_get_file,
    "github_list_repos": tool_list_repos,
}


# ------------------------------------------------------------------ 工具声明

TOOLS = [
    {
        "name": "github_status",
        "description": "查看当前使用的 GitHub 令牌来源、登录账号、令牌作用域与 API 剩余配额。排查 401/403 先调这个。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "github_push_files",
        "description": "把一批文件提交并推送到 GitHub 仓库的指定分支（分支不存在会新建）。走 Git Data API，一次提交可包含多个文件，仓库为空时也能建出首个提交。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/仓库名，例如 zz/my-project"},
                "files": {
                    "type": "array",
                    "description": "要提交的文件列表",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "仓库内相对路径，如 src/main.py"},
                            "content": {"type": "string", "description": "文件内容；encoding=path 时填本地绝对路径"},
                            "encoding": {"type": "string", "enum": ["utf-8", "base64", "path"], "description": "默认 utf-8；二进制用 base64；path 表示 content 是本地文件路径"},
                            "mode": {"type": "string", "description": "文件权限，默认 100644，可执行文件用 100755"},
                        },
                        "required": ["path", "content"],
                    },
                },
                "message": {"type": "string", "description": "提交信息"},
                "branch": {"type": "string", "description": "目标分支，留空用仓库默认分支；填新分支名会自动创建"},
                "force": {"type": "boolean", "description": "是否强制覆盖已有分支（默认 false）"},
            },
            "required": ["repo", "files"],
        },
    },
    {
        "name": "github_create_repo",
        "description": "创建一个 GitHub 仓库。默认私有。参数 name 可以只给仓库名（建在自己账号下），也可以 org/仓库名（建在组织下）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "仓库名或 org/仓库名"},
                "private": {"type": "boolean", "description": "是否私有，默认 true"},
                "description": {"type": "string", "description": "仓库简介"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "github_get_file",
        "description": "读取仓库里某个文件的内容；path 给目录时列出该目录下的条目。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/仓库名"},
                "path": {"type": "string", "description": "仓库内路径；空字符串表示根目录"},
                "ref": {"type": "string", "description": "分支、标签或提交 sha，默认默认分支"},
            },
            "required": ["repo", "path"],
        },
    },
    {
        "name": "github_list_repos",
        "description": "列出当前令牌可见的仓库，按最近推送排序。",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "返回数量，默认 10，上限 100"}},
            "additionalProperties": False,
        },
    },
]


# ------------------------------------------------------------------ MCP 协议

def make_result(rid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def make_error(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def handle(msg: dict, gh: GitHub, token_source: str) -> dict | None:
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
            "instructions": (
                "GitHub 工具集：github_status 查令牌与配额；"
                "github_push_files 批量提交文件到仓库（最常用）；"
                "github_create_repo 建仓库；github_get_file 读文件；github_list_repos 列仓库。"
            ),
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
        if not isinstance(args, dict):
            args = {}
        log(f"调用 {name} {json.dumps(args, ensure_ascii=False)[:200]}")
        try:
            content, is_error = handler(gh, {**args, "_token_source": token_source})
        except GitHubError as exc:
            content, is_error = _text(f"GitHub API 报错：{exc}", True)
        except Exception as exc:  # noqa: BLE001
            log(f"{name} 异常：{exc}")
            content, is_error = _text(f"工具执行异常：{exc}", True)
        return make_result(rid, {"content": content, "isError": is_error})
    if method == "resources/list":
        return make_result(rid, {"resources": []})
    if method == "prompts/list":
        return make_result(rid, {"prompts": []})
    if method is None:
        return None
    return make_error(rid, -32601, f"不支持的方法：{method}")


def main() -> int:
    ap = argparse.ArgumentParser(description="GitHub MCP 服务（stdio）")
    ap.add_argument("--token", default="", help="GitHub 令牌；不给就按内置顺序查找")
    ap.add_argument("--token-file", default="", help="从文件读令牌（整行）")
    ap.add_argument("--api", default=os.environ.get("GITHUB_API") or DEFAULT_API,
                    help="API 根地址，默认 https://api.github.com（自建 Enterprise 可改）")
    ap.add_argument("--timeout", type=float, default=30.0, help="单次请求超时秒数")
    opts = ap.parse_args()

    # Windows 控制台默认 cp936，协议通道必须是 UTF-8
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    token, source = resolve_token(opts.token, opts.token_file)
    gh = GitHub(token, opts.api, opts.timeout)
    log(f"启动 v{SERVER_VERSION}，令牌来源：{source}")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log(f"收到非 JSON 行，丢弃：{line[:120]}")
            continue
        reply = handle(msg, gh, source)
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
