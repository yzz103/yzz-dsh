#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_video.py —— 用本机 Agnes 网关（127.0.0.1:7871）生成一段视频。

异步流程：
    1) POST /v1/videos 建任务（OpenAI Videos 兼容），撞到队列满（503）会自动等一会儿重试
    2) 轮询任务状态（先问网关 /v1/videos/<video_id>；网关会粘住建任务的号，
       归属记录丢了还会自动挨个号找。网关不通时再直连上游 /agnesapi 兜底）
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
QUEUE_GAP = 20  # 队列满时等多少秒再重建任务


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
    ap.add_argument("--interval", type=float, default=0.0,
                    help="轮询间隔秒数；0（默认）=按耗时自适应（2/4/8 秒，接近完成时 1 秒）")
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
    status, data = 0, {}
    for attempt in range(1, 4):
        status, data = http_json("POST", f"{GATEWAY}/v1/videos", body, timeout=180)
        if status == 200:
            break
        text = json.dumps(data, ensure_ascii=False)
        if status == 503 and "video_queue_full" in text and attempt < 3:
            print(f"  [队列满] {QUEUE_GAP}s 后重试建任务（第 {attempt + 1}/3 次）")
            time.sleep(QUEUE_GAP)
            continue
        print(f"[FAIL] 建任务 HTTP {status}：{text[:500]}")
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
    errors = 0
    while True:
        # 自适应间隔：--interval > 0 时按它固定；否则前 60 秒 2 秒一查、之后 4/8 秒
        elapsed_now = time.time() - t0
        if args.interval and args.interval > 0:
            nap = args.interval
        elif last_seen and isinstance(last_seen[1], (int, float)) and last_seen[1] >= 90:
            nap = 1.0
        elif elapsed_now < 60:
            nap = 2.0
        elif elapsed_now < 300:
            nap = 4.0
        else:
            nap = 8.0
        time.sleep(nap)
        elapsed = time.time() - t0
        if elapsed > args.max_wait:
            print(f"[FAIL] 等待超过 {args.max_wait}s，放弃（任务号 {video_id} 可稍后再查）")
            return 3

        # 先问网关：网关记着这个任务是谁建的，粘号 + 归属丢失时自动挨个号找
        status, info = poll_via_gateway(video_id, args.model)
        where = "网关"
        if status != 200 or "status" not in info:
            # 网关这条不通再直连上游（用第一个号，可能不是建任务的号，仅作兜底）
            acc = accounts[0]
            status, info = poll_via_upstream(video_id, args.model, acc["key"],
                                             acc.get("base") or UPSTREAM_COM)
            where = "上游"
        if status != 200 or "status" not in info:
            errors += 1
            if errors >= 8:
                print(f"[FAIL] 连续 {errors} 次查询失败，放弃（任务号 {video_id}）")
                return 4
            continue
        errors = 0
        state = info.get("status") or info.get("internal_status") or "?"
        prog = info.get("progress")
        if (state, prog) != last_seen:
            print(f"  [{elapsed:5.0f}s] {where} HTTP {status}　status={state}　progress={prog}")
            last_seen = (state, prog)

        if state in ("completed", "succeeded"):
            final = info
            break
        if state in ("failed", "error"):
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
