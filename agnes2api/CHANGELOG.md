# Changelog · agnes2api

本文件记录本项目所有值得注意的变更。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

版本号与 `agnes2api.py` 里的 `VERSION` 常量、以及 `pyproject.toml` 的 `version` **三处必须一致**。

---

## [1.4.6] - 2026-10-21

### 修复
- **视频任务改成「粘号」轮询**：`POST /v1/videos` 建成任务后，把 `video_id → 账号 id` 记进
  `Gateway._video_owner`（只留最近 200 个）；之后 `GET /v1/videos/<id>` 优先派给建它的那个号
  （`AccountPool.acquire(..., prefer=...)`），派不到才回落到常规轮换。
  起因（实测）：Agnes 的视频任务**只对建它的那个 key 可见**，而网关是账号池、GET 会落到随机号上，
  于是轮询结果在 `200 in_progress` 与 `400 task_not_exist` 之间来回跳 —— 三个号时约 1/3 的轮询有效，
  MCP 的 `generate_video` 要碰运气才能看到 `completed`。修完 6 次连续轮询全 200、且只用那一个 key。

### 测试
- 38/38 通过（新增 24：假上游按 key 隔离任务可见性，断言 6 次轮询全 200 且只打建任务那个 key）。

---

## [1.4.5] - 2026-10-21

### 新增
- **视频类请求单独一档超时**：新增 `timeouts.video`（默认 **300 秒**），图片/文本仍用
  `request_timeout`（默认 180）。起因：实测国际站建视频任务有整笔卡满 180 秒的情况，
  网关判成「网络不通」整笔失败，可服务端其实还在排队 —— 明明能出片却被误判。
  - 配置写法：`"timeouts": {"video": 300}`；写错类型（字符串/None）自动回落默认值，不会崩。
  - `Gateway.upstream_request()` 增加可选 `timeout` 参数，`_proxy` 按 `kind` 取超时。

### 变更
- **MCP 服务 `mcp_agnes.py` 1.3.0：新增 `generate_video` 工具** —— 建任务 → 轮询 → 下载
  mp4 到网关同目录 `videos\`，返回本地路径、体积、耗时；`wait=false` 只建任务。
  轮询走网关 `GET /v1/videos/<id>`，兼容顶层 `url` 与 `metadata.url`。
  同步把 MCP 冒烟脚本扩到校验 5 个工具。

### 测试
- 37/37 通过（新增 23/23b/23c：视频超时 300、配置可覆盖、写坏回落）。

---

## [1.4.4] - 2026-10-21

### 新增
- **`gen_video.py`：命令行生成视频**（`python gen_video.py --prompt "..."`）。走网关
  `POST /v1/videos` 建异步任务，轮询到 `completed` 后把 mp4 下到 `output\`，并留一份同名
  `.json` 记录（请求体 + 最终响应）。
  - 默认 `agnes-video-2.5-flash` / `720P` / `16:9` / 4 秒（Agnes 支持 4–12 秒，字符串传参）。
  - 轮询先走官方推荐的 `GET /agnesapi?video_id=…&model_name=…`，不通再回落网关的
    `GET /v1/videos/<id>` —— 实测这两条路径会**交替**返回非 200/400，只押一条会误判成任务丢了。
  - 兼容两种结果地址：顶层 `url` 与 `metadata.url`。
  - 配置查找与 `gen_image.py` 一致（`--config` → `AGNES_CONFIG` → 脚本同级 → 当前目录）。

### 实测（2026-10-21）
- `agnes-video-2.5-flash`、4 秒、720P、16:9 → **1280×720、4.48 秒、H.264(avc1) + AAC 音轨**，
  文件 2.2–3.8 MB，**出片约 5 分 10 秒**（建任务到 `completed` 307 秒，含排队）。
- 视频可播已验证：`ftypisom` 文件头、`mvhd` 时长 4.48s、`tkhd` 1280×720、存在 `mp4a` 音轨。

---

## [1.4.3] - 2026-10-21

### 修复
- **双击 exe 时窗口一闪就没**：同端口重复启动的提示打印完立刻 `return 3`，控制台来不及看清。
  现在改为「打印正在运行的版本/账号数 → 自动用默认浏览器打开管理台 → 窗口停留 30 秒（按回车可立刻关闭）」，
  新增 `probe_running()` / `hold_console()` / `report_duplicate()`；`hold_console()` 只在
  标准输入是控制台时生效，管道/脚本场景不受影响。
- 澄清「看不到实例窗口」不是故障：后台或无控制台方式启动的进程 `MainWindowHandle` 恒为 0，
  判断存活看 `/health` 是否有 JSON 回应。

### 新增
- `build_exe.py`（PyInstaller 一条命令重打包网关/MCP 的 exe），安装目录读取
  `--dest` → 环境变量 `AGNES2API_DIR` → 脚本同级 `../agnes2api`，**不再写死本机路径**。

## [1.4.2] - 2026-10-21

### 新增
- **单实例保护**：`acquire_single_instance()` 用 Windows 命名互斥体
  （`Localgnes2api-gateway-port<端口>`，非 Windows 走 `fcntl` 文件锁）占位，
  同一端口第二次启动直接打印「已有网关在跑」并以**退出码 3** 结束，不再出现两个实例
  抢账号 / 抢统计 / 抢端口；要跑第二个用 `--port` 换端口。

### 变更
- `mcp_agnes.py` 1.2.1 → **1.2.2**：`main()` 发现 `stdin` 是控制台（`isatty()`）就提示
  「这是给 MCP 客户端用的 stdio 服务，手动打开没有界面也不会出图」并以退出码 2 退出。

## [1.4.1] - 2026-10-21

### Changed
- **可移植性：别人克隆下来就能用**，代码里不再写死本机路径。
  - `gen_image.py` / `mcp_agnes.py` 的 `config.json` 改为按顺序查找：
    `--config` → 环境变量 `AGNES_CONFIG` → 本脚本/exe 同目录 → 上一级 → 当前目录。
  - `gen_image.py` 的网关地址可用环境变量 `AGNES_BASE` 覆盖（默认 `http://127.0.0.1:7871`）。
  - `install_mcp_config.py` 用 `sys.executable` 当解释器、按脚本自身目录推源码，
    profile 目录走 `--profile` / `DSH_PROFILE_DIR`（默认 desktop），
    exe 位置可用 `AGNES_MCP_EXE` 指定；新增 `--remove`。
  - `启动网关.bat` 不再写死 Python 路径，改为 `py -3` → `python` 回退链，找不到才报错。
- `mcp_agnes.py` 版本 1.2.0 → 1.2.1（同步上述查找顺序，去掉写死路径）。

## [1.4.0] - 2026-10-20

### 新增

- **自动识别上游**：加号时只填 Key 也能用。新增 `detect_base()`，
  依次探测国际站（`https://apihub.agnes-ai.com/v1`）与国内站（`https://api.agnes-ai.cn/v1`），
  401/403 视为「该站不认这把 Key」，其它任何状态视为鉴权通过，命中的站自动写进账号。
- 新配置 `auto_base`（默认 `true`）；管理台「上游」输入框留空即走自动识别。
- 探活时若发现账号的站选错了，**自动纠正并落盘**，响应里带 `fixed_base`。
- CLI `accounts add [标签] KEY`：标签可省略；不传 `--base` 时自动识别。

### 修复

- 管理台页头 `<span id="base">` 与加号表单 `<input id="base">` **id 撞名**，
  导致 `getElementById('base').value` 取到 `undefined`，加号请求必抛错
  —— v1.3.0 里那个「上游」输入框其实一次都没生效。页头改名为 `hostline`。

### 测试

- 34/34 通过。新增 `CnUpstream` 假上游与用例 21（只填 Key 自动落国内站）、
  21b（两站都不认 → 400 且不入池）、22（探活发现选错站 → 自动纠正并持久化）。

---

## [1.3.0] - 2026-10-20

### 新增

- **按账号指定上游**：`accounts[].base` 支持 `"cn"` / `"com"` / 完整 URL 三种写法，
  由 `normalize_base()` 归一。新增常量 `DEFAULT_UPSTREAM_CN = "https://api.agnes-ai.cn/v1"`。
- CLI `accounts add … [--base cn|com|URL]`，`accounts list` 多一列「上游」。
- 管理台账号行显示所属站点域名。
- 文档新增「国际版 vs 国内版」一节：两套域名不通用，`apihub.agnes-ai.cn` 只是国际站镜像。

### 修复

- `Account.to_dict()` 必须写回 `base`，否则每次持久化都会把这个字段抹掉（与 `rpm` 同一个坑）。

### 测试

- 31/31 通过。新增 19（第二个假上游，证明请求真打到指定站）、19b（`base` 重启不丢）、
  20（`normalize_base` 四态归一）。

---

## [1.2.0] - 2026-10-20

### 新增

- **生成记录（gallery）**：把每次出图的元数据与图片本体存到 `gallery/`，
  管理台以缩略图墙展示，支持按提示词/模型筛选、单张删除、一键清空。
  新配置 `gallery.{enabled,keep,save_b64,fetch_url,max_bytes}`。
- **控制台请求流水**：每笔请求打一行
  `#序号 | 时间 | 模型 | 类型 | 状态码 | 账号 | 耗时 | 备注`，形似 wb2api。
- 新增 `sniff_ext()`：靠魔数（PNG/JPEG/WebP/GIF）判定图片格式，不信任扩展名。
- 新增 4 个管理接口：`GET /admin/api/gallery`、`GET /admin/api/gallery/file/<name>`、
  `POST /admin/api/gallery/clear`、`DELETE /admin/api/gallery/<id>`。

### 测试

- 28/28 通过。新增 15（url 分支也记元数据）、16（base64 落盘成真文件、
  用 `?token=` 取回）、17（单删连文件一起删）、18（清空）。

---

## [1.1.1] - 2026-10-20

### 修复

- **首次运行会崩**：`Config._load()` 在自动保存时 `self.data` 还没赋值 →
  `AttributeError: 'Config' object has no attribute 'data'`。改为先建目录再原子写文件。
- `/v1/models` 在无可用账号时返回空 `data: []`，改为回退到内置的 `KNOWN_MODELS`（10 个）。
- **探活误判**：原来用 `/v1/models` 验 Key，但该接口实测**不校验 token**（假 Key 也回 200），
  会把坏 Key 判成好的。改成故意用不存在的模型名发出图请求（401=无效，其它=有效），
  不消耗额度、不出图。

### 新增

- 出 Windows exe 发行包（onedir 形态）。

### 测试

- 24/24 通过（新增 7e/7e2 探活用例、13 首跑建 config、14 无号回退模型清单）。

---

## [1.1.0] - 2026-10-20

### 新增

- **按号覆盖每分钟上限**：`accounts[].rpm`，付费号可单独放宽。
- **本地限流短等待**：命中本地 RPM 上限时不再直接 429，而是最多等
  `limits.wait_max_seconds`（默认 3 秒）看有没有号滑出窗口。
- `limits.rpm_window`（默认 60 秒）可配，窗口不再写死。
- 新增 `app_dir()`：打成 exe 后配置写在 exe 旁边，不会落到临时解压目录里丢失。

### 测试

- 21/21 通过（新增 10/10b 上游 429 时本请求内切号并冷却 60 秒、
  11/11b 号级 rpm 覆盖、12 窗口即将滑出时等待约 1 秒后放行）。

---

## [1.0.0] - 2026-10-20

### 新增

- 首个版本：本地 OpenAI 兼容图片网关（`POST /v1/images/generations`、`GET /v1/models`）。
- 多账号池：轮换、失败冷却、每分钟请求数限制（按 1K/2K/3K/4K 分档）。
- 管理台 `/admin`：账号增删启停、逐号探活、实时状态、用量统计。
- 数据文件 `config.json`（含明文 Key）与 `stats.json`，首次运行自动生成。

### 测试

- 16/16 通过：假上游离线回归，覆盖轮换、冷却、限流、探活、错误路径。
