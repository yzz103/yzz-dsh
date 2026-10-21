#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_video.py —— 用本机 Agnes 网关（127.0.0.1:7871）生成一段视频。

异步流程：
    1) POST /v1/videos 建任务（OpenAI Videos 兼容）
    2) 轮询任务状态（先试网关 /v1/videos/<video_id>，不行再直连上游 /agnesapi）
    3) completed 后把 mp4 下到 output\

用法：
    python gen_video.py --prompt "..." [--seconds 4] [--model agnes-video-2.5-flash]

参数全部可选，默认 720P / 16:9 / 4 秒（Agnes 支持 4–12 秒）。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
GATEWAY = os.environ.get("AGNES_BASE", "http://127.0.0.1:7871").rstrip("/")
UPSTREAM_CN = "https://api.agnes-ai.cn/v1"
UPSTREAM_COM = "https://apihub.agnes-ai.com"


def find_config(explicit=None) -> Path:
    """按顺序找网关的 config.json：--config → AGNES_CONFIG → 脚本同级 → 当前目录。"""
    cands = []
    if explicit:
        cands.append(Path(explicit))
    env = os.environ.get("AGNES_CONFIG")
    if env:
        cands.append(Path(env))
    cands += [HERE / "config.json", Path.cwd() / "config.json"]
    for cand in cands:
        if cand.is_file():
            return cand
    raise SystemExit(
        "找不到网关的 config.json（这个文件由网关首次启动时自动生成）。\n"
        "  可用 --config 指定路径，或设置环境变量 AGNES_CONFIG，\n"
        "  或把 config.json 放到本脚本同目录 / 当前目录。"
    )


def http_json(method, url, body=None, headers=None, timeout=60):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw[:400]}
    except (urllib.error.URLError, OSError) as exc:
        return 0, {"error": f"网络不通：{exc}"}


def poll_via_gateway(video_id, model):
    return http_json("GET", f"{GATEWAY}/v1/videos/{video_id}")


def poll_via_upstream(video_id, model, key, base):
    root = base.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    url = f"{root}/agnesapi?video_id={video_id}&model_name={model}"
    return http_json("GET", url, headers={"Authorization": f"Bearer {key}"})


def pick_result_url(info: dict) -> str:
    """Agnes 有时把播放地址放顶层 url，有时塞在 metadata.url 里。"""
    if not isinstance(info, dict):
        return ""
    if info.get("url"):
        return info["url"]
    meta = info.get("metadata")
    if isinstance(meta, dict) and meta.get("url"):
        return meta["url"]
    return ""


def download(url, dest: Path):
    req = urllib.request.Request(url, headers={"User-Agent": "gen_video/1.0"})
    with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as fh:
        total = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            total += len(chunk)
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--model", default="agnes-video-2.5-flash")
    ap.add_argument("--seconds", default="4")
    ap.add_argument("--size", default="720P")
    ap.add_argument("--aspect-ratio", default="16:9")
    ap.add_argument("--mode", default="text")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--config", default=None)
    ap.add_argument("--out", default=str(HERE / "output"))
    ap.add_argument("--max-wait", type=int, default=1800)
    ap.add_argument("--interval", type=float, default=3.0)
    args = ap.parse_args()

    cfg_path = find_config(args.config)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    accounts = [a for a in (cfg.get("accounts") or []) if a.get("key") and not a.get("disabled")]
    print(f"配置：{cfg_path}")
    print(f"网关：{GATEWAY}　可用账号：{len(accounts)}")
    if not accounts:
        print("没有可用账号，先往网关里加一个 Agnes Key")
        return 1

    body = {
        "model": args.model,
        "prompt": args.prompt,
        "mode": args.mode,
        "seconds": str(args.seconds),
        "size": args.size,
        "aspect_ratio": args.aspect_ratio,
        "n": 1,
    }
    if args.seed is not None:
        body["seed"] = args.seed

    print(f"\n建任务：model={args.model} size={args.size} {args.aspect_ratio} {args.seconds}s")
    print(f"提示词：{args.prompt[:120]}{'…' if len(args.prompt) > 120 else ''}")
    t0 = time.time()
    status, data = http_json("POST", f"{GATEWAY}/v1/videos", body, timeout=180)
    if status != 200:
        print(f"[FAIL] 建任务 HTTP {status}：{json.dumps(data, ensure_ascii=False)[:500]}")
        return 2
    video_id = data.get("video_id") or data.get("id") or data.get("task_id")
    print(f"[ok] 已建任务 video_id={video_id}　state={data.get('status')}")
    if not video_id:
        print(json.dumps(data, ensure_ascii=False)[:800])
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    final = None
    last_seen = None
    while True:
        time.sleep(args.interval)
        elapsed = time.time() - t0
        if elapsed > args.max_wait:
            print(f"[FAIL] 等待超过 {args.max_wait}s，放弃")
            return 3

        # 官方推荐走 /agnesapi?...&model_name=...，网关的 /v1/videos/<id> 作为兜底
        acc = accounts[0]
        status, info = poll_via_upstream(video_id, args.model, acc["key"],
                                         acc.get("base") or UPSTREAM_COM)
        where = "上游"
        if status != 200 or "status" not in info:
            status, info = poll_via_gateway(video_id, args.model)
            where = "网关"
        state = info.get("status") or info.get("internal_status") or "?"
        prog = info.get("progress")
        if (state, prog) != last_seen:
            print(f"  [{elapsed:5.0f}s] {where} HTTP {status}　status={state}　progress={prog}")
            last_seen = (state, prog)

        if state == "completed":
            final = info
            break
        if state == "failed":
            print(f"[FAIL] 任务失败：{json.dumps(info.get('error') or info, ensure_ascii=False)[:400]}")
            return 4

    url = pick_result_url(final)
    print(f"\n[ok] 完成，用时 {time.time() - t0:.1f}s")
    if not url:
        print(json.dumps(final, ensure_ascii=False)[:800])
        return 5
    print(f"视频地址：{url}")
    name = f"video_{datetime.now().strftime('%H%M%S')}.mp4"
    dest = out_dir / name
    size_bytes = download(url, dest)
    print(f"已下载：{dest}　{size_bytes / 1024 / 1024:.2f} MB")
    (out_dir / f"{dest.stem}.json").write_text(
        json.dumps({"request": body, "result": final}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
