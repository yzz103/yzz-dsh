#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agnes2api —— 本地 Agnes AI 网关（OpenAI 兼容），零第三方依赖。

作用：
  1. 把 Agnes AI 的 API 收拢到本地一个地址（默认 http://127.0.0.1:7871/v1），
     任何 OpenAI 兼容的客户端/脚本改个 base_url 就能用。
  2. 管理多个 Agnes 账号（API Key）：轮换、失效禁用、限流冷却、用量统计。
     每个号可以单独指定上游 base（国际版 / 国内版），见 config 的 accounts[].base。
  3. 自带一个只用标准库的管理页 /admin，看账号状态、加删 key、探活。

子命令：
  serve                     启动网关（默认）
  accounts list             列出账号（key 打码）
  accounts add LABEL KEY [--rpm N] [--base URL]
                            添加账号；--base 给这个号指定上游（如国内版）
  accounts rm ID            删除账号
  accounts enable ID|disable ID
  probe                     用每个账号发一次【故意非法】的出图请求验 Key
                            （不消耗额度、不会真出图；不能用 /models 验，它不校验 token）
  status                    打印当前配置与统计

常用参数：
  --config PATH             配置文件，默认与脚本同目录 config.json
  --host / --port           覆盖监听地址
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.4.3"
DEFAULT_UPSTREAM = "https://apihub.agnes-ai.com/v1"
# 国内版（实测 2026-10-20）：apihub.agnes-ai.cn 只是国际站的镜像；
# 真·国内站是 api.agnes-ai.cn，国内账号的 key 只在它上面认（中文报错「无效的令牌 / 当前分组不支持模型」）。
DEFAULT_UPSTREAM_CN = "https://api.agnes-ai.cn/v1"
DEFAULT_PORT = 7871
# 无可用账号时 /v1/models 的兜底清单（来自官方 /v1/models 实测，2026-10-20）
KNOWN_MODELS = [
    "agnes-image-2.0-flash",
    "agnes-image-2.1-flash",
    "agnes-image-2.5-flash",
    "agnes-2.5-flash",
    "agnes-2.5-pro-alpha",
    "agnes-2.5-pro-beta",
    "agnes-3.0-flash",
    "agnes-3.0-pro",
    "agnes-video-2.5",
    "agnes-video-2.5-flash",
]

# 免费账号实际 RPM（来自 Agnes 官方 Token Plan FAQ）
DEFAULT_LIMITS = {
    "text_rpm": 10,
    "image_rpm": {"1K": 10, "2K": 5, "3K": 1, "4K": 1},
    "video_rpm": 120,
    "rpm_window": 60,        # RPM 统计窗口（秒）
    "wait_max_seconds": 3,   # 全池都卡在本地限流时，最多等这么久再放弃
}

COOLDOWN = {
    "invalid": 3600,   # 401/403：key 失效，先凉一小时
    "rate": 60,        # 429：被限流
    "transient": 5,    # 5xx / 网络错误
}

# 生图记录（管理台的「生成记录」）
DEFAULT_GALLERY = {
    "enabled": True,     # 关掉就只转发、不落盘
    "keep": 200,         # 最多留最近多少张，超了删最旧的
    "save_b64": True,    # 上游回 base64 时解码存成图片文件
    "fetch_url": True,   # 上游只回 url 时顺手下载一份（失败就只记 url）
    "max_bytes": 40 * 1024 * 1024,
}

TEXT_HINT = ("chat", "completion", "messages", "text")
IMAGE_HINT = ("image", "img", "vision-gen", "seedream")
VIDEO_HINT = ("video", "seedance")


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def now() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or now()).strftime("%Y-%m-%d %H:%M:%S")


def atomic_write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return key[:3] + "***"
    return key[:6] + "..." + key[-4:]


def normalize_base(text) -> "str | None":
    """把用户填的上游简称换成完整 URL。

    - 空            → None（用全局 upstream）
    - "cn" / "国内"  → 国内版 api.agnes-ai.cn
    - "com" / "国际" → 国际版 apihub.agnes-ai.com
    - 其它          → 当 URL 用（去掉结尾斜杠）
    """
    t = str(text or "").strip()
    if not t:
        return None
    low = t.lower()
    if low == "cn" or "国内" in t:
        return DEFAULT_UPSTREAM_CN
    if low == "com" or "国际" in t:
        return DEFAULT_UPSTREAM
    return t.rstrip("/")


def guess_tier(size: str | None) -> str:
    """Agnes 的图片档位：1K/2K/3K/4K。size 可能是 '1024x1024' 或 '2K'。"""
    if not size:
        return "1K"
    s = str(size).strip().upper()
    m = re.fullmatch(r"([1-4])K", s)
    if m:
        return f"{m.group(1)}K"
    m = re.fullmatch(r"(\d+)\s*[xX*]\s*(\d+)", s)
    if m:
        side = max(int(m.group(1)), int(m.group(2)))
        if side <= 1280:
            return "1K"
        if side <= 2304:
            return "2K"
        if side <= 3328:
            return "3K"
        return "4K"
    return "1K"


def reconfigure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def emit(line: str) -> None:
    """原样输出一行（请求流水用，自带列格式，不再加时间戳前缀）。"""
    print(line, flush=True)


def human_size(n: int) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "-"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

class Config:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise SystemExit(f"配置文件解析失败 {self.path}: {exc}")
        else:
            data = {}
        data.setdefault("host", "127.0.0.1")
        data.setdefault("port", DEFAULT_PORT)
        data.setdefault("upstream", DEFAULT_UPSTREAM)
        data.setdefault("accounts", [])
        # 老版本的 config.json 里没有新字段，这里按默认值补齐（而不是整块覆盖）
        merged = dict(DEFAULT_LIMITS)
        merged.update(data.get("limits") or {})
        merged["image_rpm"] = {
            **DEFAULT_LIMITS["image_rpm"],
            **((data.get("limits") or {}).get("image_rpm") or {}),
        }
        data["limits"] = merged
        data.setdefault("require_client_auth", False)
        data.setdefault("client_keys", [])
        data.setdefault("request_timeout", 180)
        # 加号时不填上游 → 自动探一遍国际站 / 国内站，哪个认这把 Key 就用哪个
        data.setdefault("auto_base", True)
        gal = dict(DEFAULT_GALLERY)
        gal.update(data.get("gallery") or {})
        data["gallery"] = gal
        if not data.get("admin_token"):
            data["admin_token"] = "adm-" + os.urandom(12).hex()
        if not self.path.exists():
            # 注意：这里不能调 self.save()，此刻 self.data 还没赋值
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.path, data)
        return data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, self.data)

    # 便捷属性 ------------------------------------------------------------
    @property
    def upstream(self) -> str:
        return self.data["upstream"].rstrip("/")

    @property
    def limits(self) -> dict:
        return self.data["limits"]

    @property
    def accounts(self) -> list:
        return self.data["accounts"]

    @property
    def admin_token(self) -> str:
        return self.data["admin_token"]

    @property
    def gallery(self) -> dict:
        return self.data["gallery"]

    @property
    def auto_base(self) -> bool:
        return bool(self.data.get("auto_base", True))


# --------------------------------------------------------------------------- #
# 账号池
# --------------------------------------------------------------------------- #

class Account:
    def __init__(self, raw: dict):
        self.id = raw.get("id") or ("acc-" + os.urandom(4).hex())
        self.label = raw.get("label") or self.id
        self.key = raw.get("key", "")
        # 这个号单独走哪个上游（空 = 用全局 cfg.upstream）。
        # 国际版 https://apihub.agnes-ai.com/v1，国内版 https://api.agnes-ai.cn/v1。
        self.base = normalize_base(raw.get("base"))
        self.enabled = bool(raw.get("enabled", True))
        self.created = raw.get("created", now())
        # 这个号自己每分钟能打多少（None = 用全局免费档位默认值）。
        # 付费号 / 公司号额度高，就在 config 里写 "rpm": 60，免得被本地拦截。
        self.rpm_override = raw.get("rpm")
        # 运行期状态
        self.window = 60.0           # RPM 统计窗口，由 AccountPool 注入
        self.cooldown_until = 0.0
        self.last_used = 0.0
        self.ok = 0
        self.fail = 0
        self.last_error = ""
        self.hits: deque = deque()   # 发起时间戳，用于 RPM 统计

    def to_dict(self) -> dict:
        d = {"id": self.id, "label": self.label, "key": self.key,
             "enabled": self.enabled, "created": self.created}
        if self.rpm_override:
            d["rpm"] = self.rpm_override
        if self.base:
            d["base"] = self.base
        return d

    def public(self, rpm_limit: int | None = None) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "key": mask_key(self.key),
            "base": self.base or "",
            "enabled": self.enabled,
            "cooldown": max(0.0, round(self.cooldown_until - now(), 1)),
            "ok": self.ok,
            "fail": self.fail,
            "last_error": self.last_error,
            "last_used": iso(self.last_used) if self.last_used else "",
            "rpm_used": self.rpm_used(),
            "rpm_limit": rpm_limit if rpm_limit is not None else self.rpm_override,
            "rpm_override": self.rpm_override,
        }

    def rpm_used(self, window: float | None = None) -> int:
        cutoff = now() - (window if window is not None else self.window)
        while self.hits and self.hits[0] < cutoff:
            self.hits.popleft()
        return len(self.hits)


class AccountPool:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.reload()

    def reload(self) -> None:
        with self.lock:
            self.accounts = [Account(a) for a in self.cfg.accounts]
            for a in self.accounts:
                a.window = self.window

    @property
    def window(self) -> float:
        try:
            return max(1.0, float((self.cfg.limits or {}).get("rpm_window", 60)))
        except (TypeError, ValueError):
            return 60.0

    def persist(self) -> None:
        with self.lock:
            self.cfg.data["accounts"] = [a.to_dict() for a in self.accounts]
            self.cfg.save()

    # 限额 ---------------------------------------------------------------
    def rpm_limit(self, kind: str, tier: str | None, acc: "Account | None" = None) -> int:
        """单号每分钟上限。acc 自带 rpm 覆盖时优先用它（付费号可以放宽）。"""
        lim = self.cfg.limits or {}
        if acc is not None and acc.rpm_override:
            try:
                override = int(acc.rpm_override)
                if override > 0:
                    return override
            except (TypeError, ValueError):
                pass
        if kind == "text":
            return int(lim.get("text_rpm", 10)) or 10
        if kind == "image":
            table = lim.get("image_rpm") or {}
            return int(table.get(tier or "1K", 10)) or 10
        return int(lim.get("video_rpm", 120)) or 120

    # 选号 ---------------------------------------------------------------
    def acquire(self, kind: str, tier: str | None, exclude: set[str]):
        """按「最久没用优先」选一个还有额度的号。

        返回 (account, retry_after)：
          - (acc, None)   选中了
          - (None, 秒数) 暂时没号，秒数是「最早可能腾出额度」的等待时间
        """
        with self.lock:
            t = now()
            usable, waiting = [], []
            for acc in self.accounts:
                if not acc.enabled or not acc.key or acc.id in exclude:
                    continue
                if acc.cooldown_until > t:
                    waiting.append(acc.cooldown_until - t)
                    continue
                limit = self.rpm_limit(kind, tier, acc)
                used = acc.rpm_used()
                if used >= limit:
                    # 等窗口里最早那次调用滑出窗口即可
                    if acc.hits:
                        oldest = next(iter(acc.hits))
                        waiting.append(max(0.05, oldest + acc.window - t))
                    else:
                        waiting.append(acc.window)
                    continue
                usable.append(acc)
            if not usable:
                return None, (min(waiting) if waiting else 1.0)
            # 优先用最久没用的
            usable.sort(key=lambda a: a.last_used)
            acc = usable[0]
            acc.hits.append(t)
            acc.last_used = t
            return acc, None

    def release(self, acc: Account, status: int, message: str = "") -> None:
        with self.lock:
            if 200 <= status < 300:
                acc.ok += 1
                acc.cooldown_until = 0.0
                acc.last_error = ""
                return
            acc.fail += 1
            acc.last_error = f"{status} {message[:120]}".strip()
            if status in (401, 403):
                acc.cooldown_until = now() + COOLDOWN["invalid"]
            elif status == 429:
                acc.cooldown_until = now() + COOLDOWN["rate"]
            elif status >= 500 or status == 0:
                acc.cooldown_until = now() + COOLDOWN["transient"]

    def add(self, label: str, key: str, rpm: int | None = None,
            base: str | None = None) -> dict:
        with self.lock:
            raw = {"label": label, "key": key}
            if rpm:
                raw["rpm"] = int(rpm)
            if base:
                raw["base"] = str(base).strip()
            acc = Account(raw)
            acc.window = self.window
            self.accounts.append(acc)
            self.persist()
            return acc.public()

    def remove(self, acc_id: str) -> bool:
        with self.lock:
            before = len(self.accounts)
            self.accounts = [a for a in self.accounts if a.id != acc_id]
            changed = len(self.accounts) != before
            if changed:
                self.persist()
            return changed

    def set_enabled(self, acc_id: str, enabled: bool) -> bool:
        with self.lock:
            for a in self.accounts:
                if a.id == acc_id:
                    a.enabled = enabled
                    if enabled:
                        a.cooldown_until = 0.0
                        a.fail = 0
                    self.persist()
                    return True
            return False

    def set_key(self, acc_id: str, key: str) -> bool:
        with self.lock:
            for a in self.accounts:
                if a.id == acc_id:
                    a.key = key
                    a.cooldown_until = 0.0
                    a.fail = 0
                    self.persist()
                    return True
            return False

    def snapshot(self) -> list:
        with self.lock:
            return [a.public() for a in self.accounts]


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #

class Stats:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.data = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}
        self.data.setdefault("since", iso())
        self.data.setdefault("totals", {"text": 0, "image": 0, "video": 0, "errors": 0})
        self.data.setdefault("images_by_tier", {"1K": 0, "2K": 0, "3K": 0, "4K": 0})

    def record(self, kind: str, ok: bool, tier: str | None = None) -> None:
        with self.lock:
            t = self.data["totals"]
            t[kind] = t.get(kind, 0) + 1
            if not ok:
                t["errors"] = t.get("errors", 0) + 1
            if ok and kind == "image" and tier:
                b = self.data["images_by_tier"]
                b[tier] = b.get(tier, 0) + 1
            atomic_write_json(self.path, self.data)

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.data))


# --------------------------------------------------------------------------- #
# 生图记录（管理台里能回看已经生成过的图片）
# --------------------------------------------------------------------------- #

class Gallery:
    """把每次成功出图的图片落到 `<配置目录>/gallery/`，元数据进 index.jsonl。"""

    def __init__(self, root: Path, cfg: dict | None = None):
        self.root = Path(root)
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.keep = max(1, int(cfg.get("keep", 200) or 200))
        self.save_b64 = bool(cfg.get("save_b64", True))
        self.fetch_url = bool(cfg.get("fetch_url", True))
        self.max_bytes = int(cfg.get("max_bytes", 40 * 1024 * 1024) or 40 * 1024 * 1024)
        self.index = self.root / "index.jsonl"
        self.lock = threading.Lock()

    # --- 读 ------------------------------------------------------------- #
    def records(self) -> list[dict]:
        if not self.index.exists():
            return []
        out: list[dict] = []
        try:
            for line in self.index.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        except Exception:
            return []
        return out

    def listing(self, limit: int = 200) -> list[dict]:
        return list(reversed(self.records()))[:max(1, limit)]

    def count(self) -> int:
        return len(self.records())

    def file_path(self, name: str) -> Path | None:
        if not name or any(ch in name for ch in ("/", "\\", "..")) or not name.strip():
            return None
        p = self.root / name
        try:
            return p if p.is_file() else None
        except OSError:
            return None

    # --- 写 ------------------------------------------------------------- #
    def _rewrite(self, recs: list[dict]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.index.with_suffix(".tmp")
        body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs)
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(self.index)

    def add(self, rec: dict) -> dict:
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.index.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._prune_locked()
        return rec

    def delete(self, gid: str) -> bool:
        with self.lock:
            recs = self.records()
            target = [r for r in recs if r.get("id") == gid]
            if not target:
                return False
            for r in target:
                self._unlink(r.get("file"))
            self._rewrite([r for r in recs if r.get("id") != gid])
        return True

    def clear(self) -> int:
        with self.lock:
            recs = self.records()
            for r in recs:
                self._unlink(r.get("file"))
            self._rewrite([])
        return len(recs)

    def _unlink(self, name: str | None) -> None:
        if not name:
            return
        p = self.root / name
        try:
            if p.is_file():
                p.unlink()
        except OSError:
            pass

    def _prune_locked(self) -> None:
        recs = self.records()
        if len(recs) <= self.keep:
            return
        drop = recs[:len(recs) - self.keep]
        for r in drop:
            self._unlink(r.get("file"))
        self._rewrite(recs[len(recs) - self.keep:])

    # --- 落盘 ----------------------------------------------------------- #
    def save_bytes(self, data: bytes, ext: str, gid: str) -> str | None:
        self.root.mkdir(parents=True, exist_ok=True)
        name = f"{gid}{ext}"
        (self.root / name).write_bytes(data)
        return name

    def fetch_remote(self, url: str) -> bytes | None:
        """上游只回 URL 时顺便存一份图，失败返回 None（记录照样留 url）。

        超时压到 15s：存档是「顺手」的，不能让一个下不动的图片 URL 把客户端请求
        拖到 60s。挖不到就只留元数据，管理台按 url 外链。
        """
        if not url.lower().startswith(("http://", "https://")):
            return None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "agnes2api/" + VERSION})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read(self.max_bytes + 1)
            return data if len(data) <= self.max_bytes else None
        except Exception:
            return None


def sniff_ext(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ".png"


# --------------------------------------------------------------------------- #
# 网关
# --------------------------------------------------------------------------- #

class Gateway:
    def __init__(self, config_path: Path, stats_path: Path | None = None):
        self.cfg = Config(config_path)
        self.stats = Stats(stats_path or (self.cfg.path.parent / "stats.json"))
        self.pool = AccountPool(self.cfg)
        self.gallery = Gallery(self.cfg.path.parent / "gallery", self.cfg.gallery)
        self._models_cache: tuple[float, list] = (0.0, [])
        # 自动识别上游时依次试的候选（测试里可替换成假上游）
        self.base_candidates: list = [DEFAULT_UPSTREAM, DEFAULT_UPSTREAM_CN]
        self._seq = 0
        self._seq_lock = threading.Lock()

    def next_no(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    # 上游 ---------------------------------------------------------------
    def upstream_request(self, path: str, body: bytes | None, key: str,
                         method: str = "POST", stream: bool = False,
                         base: str | None = None):
        # 客户端路径形如 /v1/images/generations，而 upstream 本身已含 /v1，
        # 直接拼接会变成 /v1/v1/... —— 这里把重复前缀削掉。
        root = (base or self.cfg.upstream or DEFAULT_UPSTREAM).rstrip("/")
        if root.endswith("/v1") and path.startswith("/v1/"):
            path = path[3:]
        url = root + path
        headers = {
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        timeout = int(self.cfg.data.get("request_timeout", 180))
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            return resp.status, resp.headers.get("Content-Type", "application/json"), resp
        except urllib.error.HTTPError as exc:
            try:
                payload = exc.read()
            except Exception:
                payload = b""
            return exc.code, exc.headers.get("Content-Type", "application/json"), payload
        except Exception as exc:
            return 0, "text/plain", f"{type(exc).__name__}: {exc}".encode()

    def models(self, force: bool = False) -> list:
        ts, cached = self._models_cache
        if cached and not force and now() - ts < 300:
            return cached
        models = []
        for acc in self.pool.accounts:
            if not acc.enabled or not acc.key:
                continue
            status, _, resp = self.upstream_request("/models", None, acc.key, "GET",
                                                    base=acc.base)
            if status == 200:
                try:
                    models = json.loads(resp.read()
                                        if hasattr(resp, "read") else resp)["data"]
                    break
                except Exception:
                    continue
        if models:
            self._models_cache = (now(), models)
            return models
        if cached:
            return cached
        # 没有任何可用账号时回退到已知模型清单，避免客户端 /v1/models 拿到空列表
        return [{"id": m, "object": "model", "owned_by": "agnes"} for m in KNOWN_MODELS]


# 探活用的故意非法请求：模型名不存在 → 上游必在参数校验阶段拒掉，不会真出图
PROBE_BODY = {"model": "agnes2api-probe-invalid-model", "prompt": "ping"}


def probe_key(gw, key: str, base: str | None = None) -> tuple[str, str]:
    """验一把 Key：返回 (状态, 说明)，状态取 ok / invalid / network。

    注意**不能用 /v1/models 验 Key** —— 实测该接口不校验 token，
    任何字符串都能拿到 200 和完整模型列表（假 key 也一样）。
    """
    body = json.dumps(PROBE_BODY).encode()
    status, _, resp = gw.upstream_request("/images/generations", body, key, "POST",
                                          base=base)
    raw = resp.read() if hasattr(resp, "read") else resp
    text = raw[:120].decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)[:120]
    if status == 0:
        return "network", "网络不通"
    if status in (401, 403):
        return "invalid", f"Key 无效（HTTP {status}）"
    if 200 <= status < 300:
        return "ok", "Key 有效（⚠ 上游接受了探测请求，可能真出了一张图）"
    return "ok", f"Key 有效（HTTP {status}：{text[:70]}）"


# 自动判断「这把 Key 属于哪个站」时依次尝试的候选上游
BASE_CANDIDATES = [DEFAULT_UPSTREAM, DEFAULT_UPSTREAM_CN]


def detect_base(gw, key: str, candidates: list | None = None,
                prefer: str | None = None) -> tuple[str | None, str, str]:
    """自动识别这把 Key 该走哪个上游，返回 (base, kind, 说明)。

    判定依据就是探活本身：某个域回 **401/403** = 这个域的鉴权不认这把 Key，
    换下一个候选；回别的任何状态（404 model_not_found / 400 / 422 / 429 …）
    都说明「鉴权已通过、只是请求内容不对」→ 就是这个域。

    全程用故意非法的模型名，不出图、不消耗额度。
    """
    order: list[str] = []
    cands = candidates if candidates is not None else getattr(
        gw, "base_candidates", BASE_CANDIDATES)
    for b in ([prefer] if prefer else []) + list(cands):
        nb = normalize_base(b)
        if nb and nb not in order:
            order.append(nb)
    notes = []
    for b in order:
        kind, text = probe_key(gw, key, b)
        if kind == "ok":
            return b, "ok", "；".join(notes + [f"{b} ✓"])
        notes.append(f"{b} ✗ {text}")
    return None, "unknown", "；".join(notes) or "没有候选上游"


# --------------------------------------------------------------------------- #
# HTTP 处理
# --------------------------------------------------------------------------- #

ADMIN_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>agnes2api 管理台</title>
<style>
:root{color-scheme:dark}
body{margin:0;background:#0b1220;color:#e6edf7;font:14px/1.6 "Segoe UI",system-ui,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:19px;margin:0 0 4px}
.sub{color:#8b9ab1;font-size:12px;margin-bottom:22px}
.card{background:rgba(18,28,46,.9);border:1px solid #1e2c44;border-radius:12px;padding:16px 18px;margin-bottom:16px}
.card h2{font-size:14px;margin:0 0 12px;color:#9fb3cd;font-weight:600}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #1b2740}
th{color:#8b9ab1;font-weight:500;font-size:12px}
code{background:#0f1a2c;padding:1px 5px;border-radius:4px;font-size:12px;color:#8fd0ff}
.tag{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11px}
.on{background:#12351f;color:#5fd08a}.off{background:#3a1a1a;color:#ff8f8f}
.cool{background:#3a2f14;color:#ffc46b}
button{background:#1b2a44;color:#d6e4f7;border:1px solid #2b3f61;border-radius:7px;padding:4px 10px;font-size:12px;cursor:pointer}
button:hover{background:#22355a}
button.del{border-color:#5a2530;color:#ff9b9b}
input{background:#0d1728;color:#e6edf7;border:1px solid #26374f;border-radius:7px;padding:6px 9px;font-size:13px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.mut{color:#77869c;font-size:12px}
.kv{display:grid;grid-template-columns:150px 1fr;gap:4px 12px;font-size:13px}
.err{color:#ff8f8f}
.gal{display:grid;grid-template-columns:repeat(auto-fill,minmax(148px,1fr));gap:10px}
.gcard{background:#0e1828;border:1px solid #1d2b44;border-radius:10px;overflow:hidden}
.gcard img{width:100%;height:148px;object-fit:cover;display:block;background:#0a121f;cursor:zoom-in}
.gcard .gph{height:148px;display:flex;align-items:center;justify-content:center;font-size:12px;color:#77869c}
.gmeta{padding:6px 8px;font-size:11px;color:#8b9ab1;line-height:1.5;word-break:break-all}
.gmeta b{color:#cfe0f5;font-weight:500}
.toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%) translateY(20px);background:rgba(10,26,44,.96);color:#d8e6f6;border:1px solid #2c4a6e;border-radius:10px;padding:10px 16px;font-size:13px;max-width:70vw;opacity:0;pointer-events:none;transition:.25s;z-index:99}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
</style></head><body><div class="wrap">
<h1>agnes2api <span class="mut" id="ver"></span></h1>
<div class="sub">本地 Agnes AI 网关 · <span id="hostline"></span></div>

<div class="card"><h2>网关信息</h2><div class="kv" id="info"></div></div>

<div class="card"><h2>账号池</h2>
<table><thead><tr><th>标签</th><th>Key</th><th>状态</th><th>RPM(60s)</th><th>成功/失败</th><th>最近使用</th><th>操作</th></tr></thead>
<tbody id="accs"></tbody></table>
<div class="row" style="margin-top:14px">
  <input id="label" placeholder="标签(可空)" style="width:110px">
  <input id="key" placeholder="sk-...（只显示一次，别外传）" style="width:380px">
  <input id="rpm" placeholder="每分钟上限(空=默认)" style="width:150px">
  <input id="base" placeholder="上游(空=自动识别)" style="width:150px">
  <button onclick="addAcc()">添加账号</button>
  <button onclick="probe()">全部探活</button>
</div>
<div class="mut" style="margin-top:8px">**只填 Key 就能加**：留空标签 / 上游时，网关会拿这把 Key 先打一次国际站、再打一次国内站，哪个认就用哪个（探活用故意非法的模型名，不出图、不消耗额度）。
「每分钟上限」留空按免费档位默认（1K 图 10 次/分钟）；付费号填数字放宽。
「全部探活」会重新校验每个号；某个号在它当前的站上过不了鉴权时，会自动换到另一个站并保存。</div>
</div>

<div class="card"><h2>累计用量</h2><div class="kv" id="stats"></div></div>

<div class="card"><h2>生成记录 <span class="mut" id="galcnt"></span></h2>
<div class="row" style="margin-bottom:12px">
  <input id="galfilter" placeholder="按提示词 / 模型筛选" style="width:280px" oninput="renderGallery()">
  <button onclick="refreshGallery()">刷新</button>
  <button class="del" onclick="clearGallery()">清空全部</button>
</div>
<div id="gallery" class="gal"></div>
</div>

<script>
const TOKEN = new URLSearchParams(location.search).get('token') || '';
async function api(path, opt={}){
  opt.headers = Object.assign({'X-Admin-Token':TOKEN,'Content-Type':'application/json'}, opt.headers||{});
  const r = await fetch(path, opt);
  if(!r.ok) throw new Error(await r.text());
  return r.json();
}
function esc(s){return String(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
async function refresh(){
  const d = await api('/admin/api/state');
  document.getElementById('ver').textContent = 'v' + d.version;
  document.getElementById('hostline').textContent = d.base_url;
  const info = d.info;
  document.getElementById('info').innerHTML = Object.entries(info)
    .map(([k,v])=>`<div class="mut">${esc(k)}</div><div>${esc(v)}</div>`).join('');
  document.getElementById('accs').innerHTML = d.accounts.map(a=>{
    const st = !a.enabled ? '<span class="tag off">已停用</span>'
      : (a.cooldown>0 ? `<span class="tag cool">冷却 ${a.cooldown}s</span>` : '<span class="tag on">可用</span>');
    const up = a.base ? `<div class="mut" style="font-size:11px">@${esc(String(a.base).replace(/^https?:\/\//,'').replace(/\/v1$/,''))}</div>` : '';
    return `<tr><td>${esc(a.label)}${up}</td><td><code>${esc(a.key)}</code></td><td>${st}</td>
      <td>${a.rpm_used}${a.rpm_limit ? ' / ' + a.rpm_limit : ''}</td><td>${a.ok} / ${a.fail}</td><td class="mut">${esc(a.last_used)||'-'}</td>
      <td><button onclick="toggle('${a.id}',${!a.enabled})">${a.enabled?'停用':'启用'}</button>
          <button class="del" onclick="rm('${a.id}')">删除</button></td></tr>`;
  }).join('') || '<tr><td colspan="7" class="mut">还没有账号，先在上面加一个</td></tr>';
  const s = d.stats;
  document.getElementById('stats').innerHTML =
    `<div class="mut">文本请求</div><div>${s.totals.text}　图片 ${s.totals.image}　视频 ${s.totals.video}　错误 ${s.totals.errors}</div>
     <div class="mut">按档位出图</div><div>1K ${s.images_by_tier['1K']}　2K ${s.images_by_tier['2K']}　3K ${s.images_by_tier['3K']}　4K ${s.images_by_tier['4K']}</div>
     <div class="mut">统计自</div><div>${esc(s.since)}</div>`;
}
async function addAcc(){
  const label = document.getElementById('label').value.trim() || '未命名';
  const key = document.getElementById('key').value.trim();
  const rpm = document.getElementById('rpm').value.trim();
  const baseRaw = document.getElementById('base').value.trim();
  if(!key) return alert('先填 key');
  const body = {label, key};
  if(rpm) body.rpm = Number(rpm);
  if(baseRaw) body.base = normBase(baseRaw);
  try{
    const d = await api('/admin/api/accounts',{method:'POST',body:JSON.stringify(body)});
    const a = d.account || {};
    document.getElementById('key').value=''; document.getElementById('rpm').value='';
    document.getElementById('base').value='';
    toast(detectedText(a, d.detected));
  }catch(e){
    alert('添加失败：' + e.message);
  }
  refresh();
}
function detectedText(a, note){
  const up = a.base ? String(a.base).replace(/^https?:\/\//,'').replace(/\/v1$/,'') : '全局默认';
  const name = a.base && /agnes-ai\.cn/.test(a.base) ? '国内站' : '国际站';
  return `已加上 ${a.label || ''}（${name} ${up}）` + (note ? ' · ' + note : '');
}
function toast(msg){
  let el = document.getElementById('toast');
  if(!el){ el = document.createElement('div'); el.id='toast'; el.className='toast'; document.body.appendChild(el); }
  el.textContent = msg; el.classList.add('show');
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(()=>el.classList.remove('show'), 6000);
}
function normBase(s){
  const t = String(s||'').trim();
  if(!t) return '';
  if(/^cn$/i.test(t) || /国内/.test(t)) return 'https://api.agnes-ai.cn/v1';
  if(/^com$/i.test(t) || /国际/.test(t)) return 'https://apihub.agnes-ai.com/v1';
  return t;
}
async function toggle(id, en){ await api(`/admin/api/accounts/${id}/enable`,{method:'POST',body:JSON.stringify({enabled:en})}); refresh(); }
async function rm(id){ if(!confirm('确认删除这个账号？')) return; await api(`/admin/api/accounts/${id}`,{method:'DELETE'}); refresh(); }
async function probe(){
  const d = await api('/admin/api/probe',{method:'POST'});
  const fixed = d.accounts.filter(a=>a.fixed_base);
  alert(d.accounts.map(a=>`${a.label} ${a.key}: ${a.probe}`).join('\n'));
  if(fixed.length) toast(`已自动纠正 ${fixed.length} 个号的上游`);
  refresh();
}
let GAL = [];
async function refreshGallery(){
  const d = await api('/admin/api/gallery?limit=200');
  GAL = d.items || [];
  document.getElementById('galcnt').textContent =
    `共 ${d.count} 张 · 最多留 ${d.keep} 张 · 存在 ${d.dir}` + (d.enabled ? '' : '（记录已关闭）');
  renderGallery();
}
function renderGallery(){
  const q = (document.getElementById('galfilter').value || '').toLowerCase();
  const list = GAL.filter(r => !q || (r.prompt||'').toLowerCase().includes(q)
                                  || (r.model||'').toLowerCase().includes(q));
  document.getElementById('gallery').innerHTML = list.map(r=>{
    const src = r.file ? `/admin/api/gallery/file/${encodeURIComponent(r.file)}?token=${encodeURIComponent(TOKEN)}` : '';
    const full = r.url || src;
    const thumb = src ? `<img src="${src}" loading="lazy" onclick="window.open('${full}','_blank')">`
                      : `<div class="gph">只有外链</div>`;
    const mb = ((r.bytes||0)/1048576).toFixed(2);
    const ext = r.url ? `<button onclick="window.open('${r.url}','_blank')">外链</button>` : '';
    return `<div class="gcard">${thumb}<div class="gmeta">
      <b>${esc((r.time||'').slice(11,19))}</b> · ${esc(r.size || r.model || '')}<br>
      ${esc((r.prompt||'').slice(0,120) || '(无提示词)')}<br>
      ${mb} MB · ${esc(String(r.duration_s||''))}s · ${esc(r.account||'')}
      <div style="margin-top:4px"><button onclick="delImg('${r.id}')">删除</button>${ext}</div>
      </div></div>`;
  }).join('') || '<div class="mut">还没有记录，出一张图就会出现在这里</div>';
}
async function delImg(id){
  if(!confirm('删掉这张记录？磁盘上的图片也会删除')) return;
  await api(`/admin/api/gallery/${id}`,{method:'DELETE'}); refreshGallery();
}
async function clearGallery(){
  if(!confirm('清空全部生成记录？磁盘上的图片也会删掉')) return;
  await api('/admin/api/gallery/clear',{method:'POST'}); refreshGallery();
}
refresh(); refreshGallery(); setInterval(refresh, 3000); setInterval(refreshGallery, 8000);
</script></div></body></html>
"""


def make_handler(gw: Gateway):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "agnes2api/" + VERSION

        # --- 基础 ------------------------------------------------------- #
        def log_message(self, fmt, *args):  # 静音默认访问日志
            return

        def _send(self, status, payload: bytes, ctype: str = "application/json",
                  extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(payload)
            except Exception:
                pass

        def _json(self, status, obj, extra=None):
            self._send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json", extra)

        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _admin_ok(self) -> bool:
            token = self.headers.get("X-Admin-Token")
            if not token:
                q = self.path.split("?", 1)[1] if "?" in self.path else ""
                for part in q.split("&"):
                    if part.startswith("token="):
                        token = part[6:]
            return bool(token) and token == gw.cfg.admin_token

        def _client_ok(self) -> bool:
            if not gw.cfg.data.get("require_client_auth"):
                return True
            auth = self.headers.get("Authorization", "")
            key = auth[7:] if auth.startswith("Bearer ") else ""
            return key in (gw.cfg.data.get("client_keys") or [])

        # --- GET -------------------------------------------------------- #
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/health":
                return self._json(200, {"ok": True, "version": VERSION,
                                        "accounts": len(gw.pool.accounts)})
            if path in ("/admin", "/admin/"):
                return self._send(200, ADMIN_HTML.encode(), "text/html; charset=utf-8")
            if path == "/admin/api/state":
                if not self._admin_ok():
                    return self._json(401, {"error": "admin token 不对"})
                return self._json(200, self._admin_state())
            if path == "/admin/api/gallery":
                if not self._admin_ok():
                    return self._json(401, {"error": "admin token 不对"})
                limit = 200
                if "?" in self.path:
                    for part in self.path.split("?", 1)[1].split("&"):
                        if part.startswith("limit="):
                            try:
                                limit = max(1, min(1000, int(part[6:])))
                            except ValueError:
                                pass
                return self._json(200, {
                    "enabled": gw.gallery.enabled,
                    "keep": gw.gallery.keep,
                    "dir": str(gw.gallery.root),
                    "count": gw.gallery.count(),
                    "items": gw.gallery.listing(limit),
                })
            if path.startswith("/admin/api/gallery/file/"):
                if not self._admin_ok():
                    return self._json(401, {"error": "admin token 不对"})
                fp = gw.gallery.file_path(path[len("/admin/api/gallery/file/"):])
                if not fp:
                    return self._json(404, {"error": "没有这张图"})
                ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                         ".webp": "image/webp", ".gif": "image/gif"}.get(
                            fp.suffix.lower(), "application/octet-stream")
                return self._send(200, fp.read_bytes(), ctype, {"Cache-Control": "private, max-age=3600"})
            if path == "/v1/models":
                if not self._client_ok():
                    return self._json(401, {"error": "客户端鉴权失败"})
                return self._json(200, {"object": "list", "data": gw.models()})
            if path.startswith("/v1/videos/"):
                return self._proxy("GET", path, b"", "video", None)
            return self._json(404, {"error": f"没有这个路径: {path}"})

        # --- POST ------------------------------------------------------- #
        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path.startswith("/admin/api/"):
                if not self._admin_ok():
                    return self._json(401, {"error": "admin token 不对"})
                return self._admin(path, self._body())
            if not self._client_ok():
                return self._json(401, {"error": "客户端鉴权失败"})
            if path == "/v1/chat/completions":
                body = self._body()
                return self._proxy("POST", path, body, "text", None)
            if path in ("/v1/images/generations", "/v1/images/edits"):
                body = self._normalize_image_body(self._body())
                tier = guess_tier(self._json_field(body, "size"))
                return self._proxy("POST", path, body, "image", tier)
            if path in ("/v1/videos", "/v1/video/generations"):
                return self._proxy("POST", path, self._body(), "video", None)
            return self._json(404, {"error": f"没有这个路径: {path}"})

        def do_DELETE(self):
            path = self.path.split("?", 1)[0]
            if not path.startswith("/admin/api/"):
                return self._json(404, {"error": "not found"})
            if not self._admin_ok():
                return self._json(401, {"error": "admin token 不对"})
            return self._admin(path, b"", method="DELETE")

        # --- 管理 API --------------------------------------------------- #
        def _admin_state(self) -> dict:
            base = f"http://{gw.cfg.data['host']}:{gw.cfg.data['port']}/v1"
            models = [m.get("id", "") for m in gw._models_cache[1]] if gw._models_cache[1] else []
            return {
                "version": VERSION,
                "base_url": base,
                "accounts": gw.pool.snapshot(),
                "stats": gw.stats.snapshot(),
                "models": models,
                "gallery": {
                    "enabled": gw.gallery.enabled,
                    "keep": gw.gallery.keep,
                    "count": gw.gallery.count(),
                    "dir": str(gw.gallery.root),
                },
                "info": {
                    "上游": gw.cfg.upstream,
                    "监听": f"{gw.cfg.data['host']}:{gw.cfg.data['port']}",
                    "账号数": f"{len(gw.pool.accounts)}（启用 "
                              f"{sum(1 for a in gw.pool.accounts if a.enabled)}）",
                    "文本 RPM 上限": gw.cfg.limits.get("text_rpm"),
                    "图片 RPM 上限": ", ".join(
                        f"{k}={v}" for k, v in (gw.cfg.limits.get("image_rpm") or {}).items()),
                    "客户端鉴权": "开启" if gw.cfg.data.get("require_client_auth") else "关闭（仅本机）",
                    "配置文件": str(gw.cfg.path),
                },
            }

        def _admin(self, path: str, body: bytes, method: str = "POST"):
            try:
                payload = json.loads(body.decode() or "{}")
            except Exception:
                payload = {}
            if path == "/admin/api/accounts" and method == "POST":
                label = (payload.get("label") or "").strip() or "未命名"
                key = (payload.get("key") or "").strip()
                if not key:
                    return self._json(400, {"error": "缺少 key"})
                try:
                    rpm = int(payload["rpm"]) if payload.get("rpm") else None
                except (TypeError, ValueError):
                    rpm = None
                base = (payload.get("base") or "").strip()
                note = ""
                # 只填 key 的默认路径：自动探一遍，判断这把 key 属于国际站还是国内站
                if not base and gw.cfg.auto_base:
                    base, kind, note = detect_base(gw, key,
                                                   gw.base_candidates)
                    if kind != "ok":
                        return self._json(400, {
                            "error": "这把 Key 在国际站和国内站都过不了鉴权：" + note})
                    log(f"自动识别 {label} 的上游 → {base}")
                acc = gw.pool.add(label, key, rpm, base)
                log(f"添加账号 {label} ({acc['key']})"
                    + (f" 上游={acc['base']}" if acc.get("base") else ""))
                return self._json(200, {"ok": True, "account": acc,
                                        "detected": note})
            m = re.fullmatch(r"/admin/api/accounts/([\w\-]+)/enable", path)
            if m and method == "POST":
                ok = gw.pool.set_enabled(m.group(1), bool(payload.get("enabled", True)))
                return self._json(200 if ok else 404, {"ok": ok})
            m = re.fullmatch(r"/admin/api/accounts/([\w\-]+)", path)
            if m and method == "DELETE":
                ok = gw.pool.remove(m.group(1))
                return self._json(200 if ok else 404, {"ok": ok})
            if path == "/admin/api/probe" and method == "POST":
                return self._json(200, {"accounts": self._probe()})
            if path == "/admin/api/gallery/clear" and method == "POST":
                n = gw.gallery.clear()
                log(f"清空生成记录 {n} 条")
                return self._json(200, {"ok": True, "removed": n})
            m = re.fullmatch(r"/admin/api/gallery/([\w\-]+)", path)
            if m and method == "DELETE":
                ok = gw.gallery.delete(m.group(1))
                return self._json(200 if ok else 404, {"ok": ok})
            return self._json(404, {"error": f"没有这个管理接口: {path}"})

        def _probe(self) -> list:
            out = []
            for acc in gw.pool.accounts:
                kind, text = probe_key(gw, acc.key, acc.base)
                fixed = ""
                # 这个号在它当前的站上过不了鉴权 → 自动试另一个站，认就改过去
                if kind != "ok" and gw.cfg.auto_base and acc.key:
                    base, kind2, note = detect_base(gw, acc.key,
                                                   gw.base_candidates,
                                                   prefer=None)
                    if kind2 == "ok" and base != (acc.base or ""):
                        acc.base = base
                        gw.pool.persist()
                        kind = "ok"
                        text = f"已自动纠正上游 → {base}（{note}）"
                        fixed = base
                        log(f"自动纠正 {acc.id} 的上游 → {base}")
                if kind == "ok":
                    acc.cooldown_until = 0.0
                    acc.last_error = ""
                elif kind == "invalid":
                    acc.last_error = text
                out.append({**acc.public(), "probe": text, "fixed_base": fixed})
            return out

        # --- 代理 ------------------------------------------------------- #
        @staticmethod
        def _json_field(body: bytes, field: str):
            try:
                return json.loads(body.decode()).get(field)
            except Exception:
                return None

        @staticmethod
        def _normalize_image_body(body: bytes) -> bytes:
            """Agnes 要求 response_format 放在 extra_body 里，这里替客户端挪一下。"""
            try:
                doc = json.loads(body.decode())
            except Exception:
                return body
            if not isinstance(doc, dict):
                return body
            top = doc.pop("response_format", None)
            eb = doc.get("extra_body")
            if top and isinstance(eb, dict) and "response_format" not in eb:
                eb["response_format"] = top
            elif top and eb is None:
                doc["extra_body"] = {"response_format": top}
            if doc.get("return_base64") and isinstance(doc.get("extra_body"), dict) \
                    and "response_format" not in doc["extra_body"]:
                doc["extra_body"]["response_format"] = "b64_json"
            return json.dumps(doc, ensure_ascii=False).encode()

        def _log_req(self, kind: str, model: str, status, label: str, t0: float,
                     note: str = "") -> None:
            """一行一条请求流水（模仿 wb2api 的表格风格）。"""
            dur = max(0.0, now() - t0)
            cols = [
                f"#{gw.next_no():>4}",
                datetime.now().strftime("%H:%M:%S"),
                (model or "-")[:28].ljust(28),
                kind.ljust(5),
                str(status).rjust(3),
                (label or "-")[:12].ljust(12),
                f"{dur:6.1f}s",
            ]
            if note:
                cols.append(note)
            emit(" | ".join(cols))

        def _record_image(self, req_body: bytes, raw: bytes, acc, tier, elapsed: float) -> dict | None:
            """出图成功时把图存进 gallery/，管理台的「生成记录」就是读这里。"""
            gal = gw.gallery
            if not gal.enabled:
                return None
            try:
                doc = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                return None
            items = doc.get("data") or []
            if not items or not isinstance(items[0], dict):
                return None
            item = items[0]
            try:
                req = json.loads(req_body.decode()) if req_body else {}
            except Exception:
                req = {}
            if not isinstance(req, dict):
                req = {}

            gid = datetime.now().strftime("%Y%m%d-%H%M%S-") + f"{random.randint(0, 0xFFFF):04x}"
            data: bytes | None = None
            ext, source = ".png", ""
            b64 = item.get("b64_json") or item.get("b64") or ""
            if gal.save_b64 and isinstance(b64, str) and b64:
                try:
                    payload = b64.split(",", 1)[1] if b64.startswith("data:") else b64
                    data = base64.b64decode(payload, validate=False)
                    ext = sniff_ext(data)
                    source = "b64"
                except Exception:
                    data = None
            remote = str(item.get("url") or "")
            if data is None and gal.fetch_url and remote:
                fetched = gal.fetch_remote(remote)
                if fetched:
                    data, ext, source = fetched, sniff_ext(fetched), "url"

            name = ""
            if data is not None:
                if len(data) > gal.max_bytes:
                    data = None
                else:
                    name = gal.save_bytes(data, ext, gid) or ""

            rec = {
                "id": gid,
                "time": iso(),
                "prompt": str(req.get("prompt") or "")[:1000],
                "model": str(req.get("model") or doc.get("model") or ""),
                "size": str(req.get("size") or req.get("resolution") or ""),
                "tier": tier or "",
                "account": acc.label,
                "file": name,
                "bytes": len(data) if data else 0,
                "source": source,
                "url": remote,
                "task_id": str(doc.get("task_id") or doc.get("id") or ""),
                "duration_s": round(elapsed, 1),
            }
            gal.add(rec)
            log(f"已记录生成图片 {gid}{ext if name else '（只记元数据）'}")
            return rec

        def _proxy(self, method: str, path: str, body: bytes, kind: str, tier):
            t0 = now()
            stream = False
            if kind == "text":
                try:
                    stream = bool(json.loads(body.decode()).get("stream"))
                except Exception:
                    stream = False

            model = ""
            if method != "GET":
                model = str(self._json_field(body, "model") or "")
            if not model:
                model = path.rstrip("/").rsplit("/", 1)[-1]

            tried: set[str] = set()
            waited = 0
            try:
                max_wait = float((gw.cfg.limits or {}).get("wait_max_seconds", 3) or 0)
            except (TypeError, ValueError):
                max_wait = 0.0
            last = (0, "application/json",
                    b'{"error":{"message":"no available account"}}')
            for _ in range(max(1, len([a for a in gw.pool.accounts if a.enabled and a.key]))):
                acc, wait = gw.pool.acquire(kind, tier, tried)
                if acc is None and wait and 0 < wait <= max_wait and waited < 1:
                    # 只差一点点额度就腾出来：短暂等待后改用这个号，而不是直接丢 429
                    waited = 1
                    time.sleep(min(float(wait), max_wait) + 0.05)
                    acc, wait = gw.pool.acquire(kind, tier, tried)
                if acc is None:
                    gw.stats.record(kind, False, tier)
                    self._log_req(kind, model, 429, "-", t0,
                                  f"无可用账号，约 {round(wait or 1, 1)}s 后重试")
                    return self._json(429, {
                        "error": {
                            "message": f"没有可用账号（都在冷却或已达本地 RPM 上限），"
                                       f"约 {round(wait or 1, 1)} 秒后重试",
                            "type": "rate_limit_local",
                        }
                    }, {"Retry-After": str(int(wait or 1) + 1)})
                tried.add(acc.id)
                status, ctype, resp = gw.upstream_request(path, body if method != "GET" else None,
                                                         acc.key, method, stream=stream,
                                                         base=acc.base)
                if status == 200 and stream:
                    gw.pool.release(acc, 200)
                    gw.stats.record(kind, True, tier)
                    self.send_response(200)
                    self.send_header("Content-Type", ctype or "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    try:
                        while True:
                            chunk = resp.read(1024)
                            if not chunk:
                                break
                            self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                            self.wfile.flush()
                        self.wfile.write(b"0\r\n\r\n")
                    except Exception:
                        pass
                    self._log_req(kind, model, 200, acc.label, t0, "stream")
                    return
                raw = resp.read() if hasattr(resp, "read") else (resp or b"")
                gw.pool.release(acc, status, raw[:200].decode("utf-8", "replace"))
                last = (status, ctype, raw)
                if status == 200:
                    gw.stats.record(kind, True, tier)
                    note = human_size(len(raw))
                    if kind == "image":
                        rec = None
                        try:
                            rec = self._record_image(body, raw, acc, tier, now() - t0)
                        except Exception as exc:
                            log(f"生图记录失败（不影响出图）: {exc}")
                        if rec:
                            note = " | ".join(x for x in (
                                rec.get("size") or "", human_size(rec.get("bytes") or 0),
                                rec.get("file") or "(仅元数据)") if x)
                    else:
                        try:
                            usage = json.loads(raw.decode("utf-8", "replace")).get("usage") or {}
                            if usage.get("total_tokens"):
                                note = f"tok={usage['total_tokens']} | {note}"
                        except Exception:
                            pass
                    self._log_req(kind, model, 200, acc.label, t0, note)
                    return self._send(200, raw, ctype)
                if status in (400, 404, 422):
                    gw.stats.record(kind, False, tier)
                    etext = raw[:120].decode("utf-8", "replace").replace("\n", " ")
                    self._log_req(kind, model, status, acc.label, t0, etext)
                    return self._send(status, raw, ctype or "application/json")
                log(f"{path} 账号 {acc.label} 返回 {status}，换下一个")
            # 所有账号都试过且都没成功：统一回本地 429，带上最后一次上游错误
            gw.stats.record(kind, False, tier)
            status, _ctype, raw = last
            detail = (raw or b"")[:160].decode("utf-8", "replace")
            self._log_req(kind, model, 429, "-", t0,
                          f"全池失败（最后一次 HTTP {status}）")
            return self._json(429, {
                "error": {
                    "message": f"账号池里每个号都试过了，仍失败（最后一次 HTTP {status}）"
                               + (f"：{detail}" if detail else ""),
                    "type": "rate_limit_local",
                }
            }, {"Retry-After": "5"})

    return Handler


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #

_SINGLE_INSTANCE_GUARD: object | None = None


def acquire_single_instance(port: int) -> object | None:
    """单实例守卫：同一个端口只允许一个网关进程（Windows 用命名互斥体）。

    返回：
      - 句柄/对象    拿到锁（进程存活期间一直持有，退出时由系统或 GC 回收）
      - None         已经有实例在监听这个端口，调用方应该提示并退出
      - object()     平台不支持 / 创建失败 —— fail-open，宁可多开一次也别把网关搞挂
    """
    name = f"Local\\agnes2api-gateway-port{port}"
    if os.name != "nt":
        try:
            import fcntl
            lock = Path(os.environ.get("TEMP") or "/tmp") / f"agnes2api-port{port}.lock"
            fh = open(lock, "w")
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except (OSError, ImportError):
            return object()
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        handle = k32.CreateMutexW(None, True, name)
        err = ctypes.get_last_error()
        if not handle:
            return object()
        if err == 183:            # ERROR_ALREADY_EXISTS：已经有一个实例持有这把锁
            k32.CloseHandle(handle)
            return None
        return handle
    except Exception:             # noqa: BLE001
        return object()


def probe_running(host: str, port: int) -> dict | None:
    """探一下已经在跑的实例（/health），只为把提示写得具体些。"""
    try:
        url = f"http://{host}:{port}/health"
        with urllib.request.urlopen(url, timeout=2.0) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:             # noqa: BLE001
        return None


def hold_console(seconds: float = 30.0) -> None:
    """双击启动的窗口别一闪就没：等回车，最多 seconds 秒。

    只在「标准输入是控制台」时生效（命令行/无窗口模式立即返回），
    这样脚本与自检流程不会被卡住。
    """
    try:
        if not sys.stdin.isatty():
            return
    except Exception:             # noqa: BLE001
        return
    try:
        waiter = threading.Thread(target=sys.stdin.readline, daemon=True)
        waiter.start()
        waiter.join(0.5)
        if waiter.is_alive():     # stdin 真有人在看才提示；管道/EOF 场景直接走人
            log(f"\n窗口会在 {int(seconds)} 秒后自动关闭，按回车可立刻关闭。")
            waiter.join(max(0.0, seconds - 0.5))
    except Exception:             # noqa: BLE001
        try:
            time.sleep(seconds)
        except Exception:         # noqa: BLE001
            pass


def report_duplicate(gw: Gateway, host: str, port: int) -> int:
    """「第二次双击同一个 exe」的分支：说清情况、把管理台打开、窗口留着可读。

    退出码 3 = 重复打开（不是崩溃），调用方/脚本可据此判断。
    """
    log(f"✗ 端口 {port} 上已经有一个网关在跑了 —— 这是重复打开，本实例不启动。")
    info = probe_running(host, port)
    if info:
        log(f"  正在运行：v{info.get('version', '?')}"
            f"，账号 {info.get('accounts', '?')} 个")
    else:
        log("  但 /health 没回应：端口被别的程序占着，或者它正在启动中。")
    admin_url = f"http://{host}:{port}/admin?token={gw.cfg.admin_token}"
    log(f"  管理台（账号 / 生成记录）：{admin_url}")
    log("  要同时跑第二个实例请换端口：agnes2api.exe serve --port 7872")
    try:
        if sys.stdin.isatty():    # 双击打开时顺手把管理台弹出来
            import webbrowser
            webbrowser.open(admin_url)
    except Exception:             # noqa: BLE001
        pass
    hold_console()
    return 3


def build_server(gw: Gateway, host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(gw))
    httpd.daemon_threads = True
    gw.cfg.data["host"] = host
    gw.cfg.data["port"] = httpd.server_address[1]
    return httpd


def cmd_serve(args) -> int:
    global _SINGLE_INSTANCE_GUARD
    gw = Gateway(Path(args.config))
    host = args.host or gw.cfg.data["host"]
    port = args.port or gw.cfg.data["port"]
    guard = acquire_single_instance(int(port))
    if guard is None:
        # 重复打开同一个 exe：不起第二个（抢账号/抢统计/抢端口），
        # 但也不能一闪就没 —— 把状态说清、打开管理台、窗口留 30 秒。
        return report_duplicate(gw, host, int(port))
    _SINGLE_INSTANCE_GUARD = guard       # 引用住，进程存活期间别让 GC 放锁
    httpd = build_server(gw, host, port)
    real_port = httpd.server_address[1]
    admin_url = f"http://{host}:{real_port}/admin?token={gw.cfg.admin_token}"
    log(f"agnes2api v{VERSION} 已启动")
    log(f"  OpenAI 兼容地址 : http://{host}:{real_port}/v1")
    log(f"  上游            : {gw.cfg.upstream}")
    log(f"  账号            : {len(gw.pool.accounts)} 个（启用 "
        f"{sum(1 for a in gw.pool.accounts if a.enabled)}）")
    log(f"  管理台          : {admin_url}")
    if not gw.pool.accounts:
        log("  ⚠ 还没有任何账号，先去管理台加一个 key")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("收到 Ctrl+C，关闭")
    finally:
        httpd.server_close()
    return 0


def cmd_accounts(args) -> int:
    gw = Gateway(Path(args.config))
    sub = args.action
    if sub == "list":
        rows = gw.pool.snapshot()
        if not rows:
            print("（没有账号）")
            return 0
        print(f"{'ID':<14}{'标签':<16}{'Key':<22}{'启用':<6}{'上游':<34}{'成功/失败':<10}")
        for a in rows:
            up = a.get("base") or "(全局)"
            print(f"{a['id']:<14}{a['label']:<16}{a['key']:<22}"
                  f"{'是' if a['enabled'] else '否':<6}{up:<34}{a['ok']}/{a['fail']}")
        return 0
    if sub == "add":
        # 允许 `accounts add sk-xxx`（只给 key，标签自动）与 `accounts add 标签 sk-xxx`
        if args.key is None and args.target and str(args.target).startswith("sk-"):
            args.target, args.key = None, args.target
        if not args.key:
            print("用法: accounts add [标签] KEY [--rpm N] [--base cn|URL]")
            return 2
        base = getattr(args, "base", None)
        if not base and gw.cfg.auto_base:
            base, kind, note = detect_base(gw, args.key, gw.base_candidates)
            if kind != "ok":
                print("这把 Key 在国际站和国内站都过不了鉴权：" + note)
                return 1
            print(f"自动识别上游 → {base}")
        acc = gw.pool.add(args.target or "未命名", args.key,
                          getattr(args, "rpm", None), base)
        extra = f"，每分钟上限 {args.rpm}" if getattr(args, "rpm", None) else ""
        if acc.get("base"):
            extra += f"，上游 {acc['base']}"
        print(f"已添加 {acc['label']} ({acc['key']}, id={acc['id']}{extra})")
        return 0
    if sub == "rm":
        print("已删除" if gw.pool.remove(args.account_id) else "没找到这个 id")
        return 0
    if sub in ("enable", "disable"):
        ok = gw.pool.set_enabled(args.account_id, sub == "enable")
        print("已更新" if ok else "没找到这个 id")
        return 0
    print(f"未知动作: {sub}")
    return 2


def cmd_probe(args) -> int:
    gw = Gateway(Path(args.config))
    if not gw.pool.accounts:
        print("（没有账号）")
        return 0
    for acc in gw.pool.accounts:
        kind, text = probe_key(gw, acc.key, acc.base)
        if kind == "ok":
            print(f"{acc.label:<16}{acc.id:<14}PASS  {text}")
        else:
            print(f"{acc.label:<16}{acc.id:<14}FAIL  {text}")
    return 0


def cmd_status(args) -> int:
    gw = Gateway(Path(args.config))
    d = gw.cfg.data
    print(f"agnes2api v{VERSION}")
    print(f"  配置     : {gw.cfg.path}")
    print(f"  上游     : {gw.cfg.upstream}")
    print(f"  监听     : {d['host']}:{d['port']}")
    print(f"  账号     : {len(gw.pool.accounts)}")
    for a in gw.pool.snapshot():
        print(f"    - {a['label']:<14} {a['key']:<22} 启用={'是' if a['enabled'] else '否'}")
    print(f"  管理台   : http://{d['host']}:{d['port']}/admin?token={gw.cfg.admin_token}")
    s = gw.stats.snapshot()
    print(f"  用量     : 文本 {s['totals']['text']} / 图片 {s['totals']['image']} "
          f"/ 视频 {s['totals']['video']} / 错误 {s['totals']['errors']}")
    return 0


def app_dir() -> Path:
    """程序所在目录。打成 exe 后 __file__ 指向解包临时目录，必须用 exe 自己的位置。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def main(argv=None) -> int:
    reconfigure_stdout()
    default_cfg = app_dir() / "config.json"

    ap = argparse.ArgumentParser(description="Agnes AI 本地网关（OpenAI 兼容）")
    ap.add_argument("--config", default=str(default_cfg))
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    subs = ap.add_subparsers(dest="cmd")

    subs.add_parser("serve", help="启动网关")

    acc = subs.add_parser("accounts", help="账号管理")
    acc.add_argument("action", choices=["list", "add", "rm", "enable", "disable"])
    acc.add_argument("target", nargs="?", help="LABEL（add 时）或 ID")
    acc.add_argument("key", nargs="?", help="add 时的 API Key")
    acc.add_argument("--rpm", type=int, help="add 时该号每分钟上限（付费号可放宽）")
    acc.add_argument("--base", help="add 时该号的上游：cn=国内版 / com=国际版 / 完整 URL")

    subs.add_parser("probe", help="逐个账号探活")
    subs.add_parser("status", help="打印现状")

    args = ap.parse_args(argv)
    if args.cmd in (None, "serve"):
        return cmd_serve(args)
    if args.cmd == "accounts":
        action = args.action
        label_or_id = args.target
        labels = {"list": [], "add": [label_or_id, args.key],
                  "rm": [label_or_id], "enable": [label_or_id], "disable": [label_or_id]}
        setattr(args, "label", labels["add"][0])
        setattr(args, "account_id", label_or_id)
        setattr(args, "key", labels["add"][1])
        return cmd_accounts(args)
    if args.cmd == "probe":
        return cmd_probe(args)
    if args.cmd == "status":
        return cmd_status(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
