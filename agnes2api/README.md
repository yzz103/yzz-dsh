# agnes2api

把 **Agnes AI** 的免费生图接口包成一个 **本地 OpenAI 兼容网关**（`127.0.0.1:7871`），
外加一个 **MCP 连接器**，让 DeepSeek Harness / Claude Desktop / Cline 这类客户端
可以直接「说一句话出一张图」。

纯 **Python 3.8+ 标准库**，零第三方依赖，不需要 GPU。

```
MCP 客户端（DSH / Claude Desktop / Cline …）
   └─ mcp_agnes.py         stdio MCP 服务，被客户端拉起
        └─ HTTP 127.0.0.1:7871
             └─ agnes2api.py   账号池 / 限流 / 冷却 / 图片存档
                  └─ https://apihub.agnes-ai.com/v1 （国际站）
                     https://api.agnes-ai.cn/v1      （国内站）
```

## 能做什么

- **多账号池**：轮换调用，命中 429 自动切下一个号并冷却，可用号全挂时短等待再放行
- **自动识别上游**：只粘一个 `sk-...`，程序自己判断这把 Key 是国际站还是国内站签的
- **每分钟限流**：按尺寸档位（1K/2K/3K/4K）分别限速，单号可覆盖
- **图片存档 + 管理台**：出图自动存 `gallery\`，网页上能看缩略图墙、提示词、耗时、体积，能删
- **请求流水**：控制台每笔一行，形如
  `#   1 | 21:15:41 | agnes-image-2.5-flash | image | 200 | 未命名 | 76.4s | 1024x1024 | 1.19 MB | 20261020-211541-79ea.png`
- **OpenAI 兼容**：任何能配 baseURL 的客户端都能接
- **MCP 工具**：`generate_image` / `list_images` / `gateway_status` / `delete_image`

## 快速开始

```bash
# 1) 起网关（首次运行会生成 config.json，里面带随机 admin_token）
python agnes2api.py serve

# 2) 加一个账号（Key 从 platform.agnes-ai.com 拿；国际站/国内站会自动识别）
python agnes2api.py accounts add 主号 sk-xxxxxxxx

# 3) 出图
python gen_image.py --prompt "一只戴围巾的柯基犬坐在雪地里，写实摄影" --size 1024x1024
```

管理台：`http://127.0.0.1:7871/admin?token=<config.json 里的 admin_token>`

## 接 MCP 客户端

把 `mcp_agnes.py` 拷过去，在客户端的 `mcpServers` 里注册：

```json
"agnes": {
  "command": "python3",
  "args": ["/path/to/mcp_agnes.py"],
  "env": {
    "AGNES_BASE": "http://127.0.0.1:7871",
    "AGNES_TOKEN": "网关 config.json 里的 admin_token",
    "PYTHONUTF8": "1"
  }
}
```

DSH 用户也可以直接跑 `python install_mcp_config.py` 自动写进 `cordis.patch.yml`
（**新增条目必须写成 `- insert:`**，写 `- id:` 会被静默跳过）。

自检（不出图、不花钱）：

```bash
python mcp_smoke.py          # 源码模式
python mcp_smoke.py --exe <mcp_agnes.exe>   # 打包模式
```

## 配置

`config.json` 首次运行自动生成，字段说明见 `config.example.json`。常用几项：

| 字段 | 作用 |
|---|---|
| `accounts[]` | 账号列表：`key`、`label`、`base`（空=自动）、`rpm`（单号上限） |
| `limits.image_rpm` | 各尺寸档位的每分钟上限（1K=10 / 2K=5 / 3K=1 / 4K=1） |
| `limits.wait_max_seconds` | 全池限流时最多等几秒再回 429 |
| `auto_base` | 是否自动识别国际站/国内站（默认 true） |
| `gallery.keep` | 存档保留张数（默认 200） |

⚠ `config.json` 里是**明文 API Key**，已在 `.gitignore` 里排除，**不要提交**。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/images/generations` | OpenAI 兼容出图（支持 `b64_json`） |
| GET | `/v1/models` | 模型列表 |
| GET | `/health` | 健康检查 |
| GET | `/admin/api/state` | 账号/用量状态（需 `X-Admin-Token`） |
| GET | `/admin/api/gallery` | 生成记录列表 |

## 测试

```bash
python test_gateway.py     # 离线回归，假上游，34 项用例
python mcp_smoke.py        # MCP 协议冒烟，13 项
```

## 说明

- Agnes 图片模型目前**限时免费**，只受 RPM 限流；具体以官方定价页为准。
- 本项目只是协议转换与账号管理，**不代理、不破解**，出图能力与额度取决于你自己的账号。
- Windows 下打包成 exe 用 `PyInstaller --onedir`（**不要 `--onefile`**：单文件版启动时要把自己
  解压到临时目录，部分机器会报 `[PYI-ERROR] Could not create temporary directory!`）。

## 目录

```
agnes2api.py           网关主程序
mcp_agnes.py           MCP 连接器（stdio）
mcp_smoke.py           MCP 冒烟测试
gen_image.py           命令行出图小工具
install_mcp_config.py  一键写入 DSH 配置
test_gateway.py        网关离线回归测试
config.example.json    配置模板
release/               发行包里的启动脚本与说明
```
