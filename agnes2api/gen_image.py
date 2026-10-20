r"""接正在跑的 agnes2api（默认 7871），先探活再出一张图。

用法：
  python _gen.py                            # 默认小猫，1024x1024
  python _gen.py --prompt "赛博朋克雨夜街道" --size 2K
输出：output\gen_<时间>.png
"""
import argparse
import base64
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = os.environ.get("AGNES_BASE", "http://127.0.0.1:7871").rstrip("/")


def find_config(explicit=None) -> Path:
    """按顺序找网关的 config.json：--config → AGNES_CONFIG → 脚本同级 → 当前目录。

    网关首次启动会在自己所在目录生成 config.json，所以把本脚本放在网关目录里
    就能直接用，不用改代码。
    """
    cands = []
    if explicit:
        cands.append(Path(explicit))
    env = os.environ.get("AGNES_CONFIG")
    if env:
        cands.append(Path(env))
    cands += [HERE / "config.json", Path.cwd() / "config.json"]
    for c in cands:
        if c.is_file():
            return c
    raise SystemExit(
        "找不到网关的 config.json（这个文件由网关首次启动时自动生成）。\n"
        "  可用 --config 指定路径，或设置环境变量 AGNES_CONFIG，\n"
        "  或把 config.json 放到本脚本同目录 / 当前目录。"
    )


def read_cfg(path):
    p = Path(path)
    if not p.exists():
        print(f"找不到 {p}")
        sys.exit(2)
    return json.loads(p.read_text(encoding="utf-8"))


def call(path, body=None, token=None, timeout=180):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Admin-Token", token)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read(), time.time() - t0


def img_size(raw):
    if raw[:8] == b"\x89PNG\r\n\x1a\n" and raw[12:16] == b"IHDR":
        return struct.unpack(">II", raw[16:24])
    if raw[:2] == b"\xff\xd8":
        i = 2
        while i < len(raw) - 9:
            if raw[i] != 0xFF:
                i += 1
                continue
            m = raw[i + 1]
            if m in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                return w, h
            i += 2 + struct.unpack(">H", raw[i + 2:i + 4])[0]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None, help="网关 config.json 路径（默认自动查找）")
    ap.add_argument("--prompt", default="一只橘白色的小奶猫，坐在木地板上，阳光从窗户照进来，浅景深，写实摄影风格")
    ap.add_argument("--size", default="1024x1024")
    ap.add_argument("--model", default="agnes-image-2.5-flash")
    ap.add_argument("--probe", action="store_true", help="只探活，不出图")
    args = ap.parse_args()

    cfg_path = find_config(args.config)
    cfg = read_cfg(cfg_path)
    token = cfg["admin_token"]
    accs = cfg.get("accounts") or []
    print(f"配置      : {cfg_path}")
    print(f"网关      : {BASE}")
    print(f"账号数    : {len(accs)}  启用的: {sum(1 for a in accs if a.get('enabled', True))}")

    st, body, dt = call("/health")
    print(f"/health   : {st} {body.decode('utf-8', 'replace').strip()}  ({dt:.2f}s)")
    if st != 200:
        sys.exit(1)

    print("\n--- 探活（故意非法出图请求，不消耗额度） ---")
    st, body, dt = call("/admin/api/probe", {}, token=token)
    if st != 200:
        print(f"探活失败 {st} {body[:300]}")
        sys.exit(1)
    for a in json.loads(body)["accounts"]:
        print(f"  {a['label']:<10} {a['probe']}")
    if args.probe:
        return

    print(f"\n--- 出图 ---\n  model={args.model}  size={args.size}\n  prompt={args.prompt}")
    req = {"model": args.model, "prompt": args.prompt, "size": args.size, "return_base64": True}
    st, body, dt = call("/v1/images/generations", req, timeout=300)
    print(f"HTTP {st}  {dt:.1f}s  响应 {len(body)} 字节")
    if st != 200:
        print(body[:1000].decode("utf-8", "replace"))
        sys.exit(1)

    doc = json.loads(body)
    item = (doc.get("data") or [{}])[0]
    out_dir = HERE / "output"
    out_dir.mkdir(exist_ok=True)

    if item.get("b64_json"):
        raw = base64.b64decode(item["b64_json"])
        print(f"返回 b64_json（{len(raw)} 字节）")
    elif item.get("url"):
        print(f"返回 url: {item['url'][:120]}")
        with urllib.request.urlopen(item["url"], timeout=120) as r:
            raw = r.read()
    else:
        print("响应里既没有 b64_json 也没有 url：")
        print(json.dumps(doc, ensure_ascii=False)[:800])
        sys.exit(1)

    ext = ".png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"
    out = out_dir / f"gen_{time.strftime('%H%M%S')}{ext}"
    out.write_bytes(raw)
    size = img_size(raw)
    dim = "  %dx%d" % size if size else ""
    print(f"已保存    : {out}  ({len(raw)} 字节{dim})")


if __name__ == "__main__":
    main()
