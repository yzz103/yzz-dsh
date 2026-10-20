# yzz-dsh

我的 DSH（DeepSeek Harness）相关项目合集。每个项目一个文件夹，互不干扰。

| 项目 | 一句话 | 语言/依赖 |
|---|---|---|
| [agnes2api](agnes2api/) | Agnes AI 生图的多账号本地网关：OpenAI 兼容 API + 管理台 + MCP 工具 | Python 3.9+（纯标准库） |

## 目录约定

```
yzz-dsh/
├─ README.md          这个索引
├─ .gitignore         全仓库通用忽略规则
└─ agnes2api/         项目一：Agnes 生图网关
   ├─ README.md       项目说明（能力、部署、接口、配置）
   ├─ agnes2api.py    网关主程序
   ├─ mcp_agnes.py    MCP 连接器
   └─ …
```

## 关于 agnes2api

把 **Agnes AI 的免费生图能力**包成一个**本机 OpenAI 兼容网关**：

- **多账号池**：多个 Key 轮换，429/额度耗尽自动切号，冷却期自动避开
- **自动识别上游**：加号时只填 Key，自动判断这把 Key 属于国际站（`apihub.agnes-ai.com`）还是国内站（`api.agnes-ai.cn`）
- **分档限流**：按 1K / 2K / 3K / 4K 分别控每分钟张数，还能给单个账号单独设上限
- **管理台**：网页上看账号状态、用量、生成记录（缩略图墙，可筛选/删除/清空）
- **请求流水**：控制台每笔请求一行，含耗时、尺寸、体积、文件名
- **MCP 工具**：`generate_image` / `list_images` / `gateway_status` / `delete_image`，任何 MCP 客户端可直接调
- **零依赖**：只要 Python 3.9+，不用 pip 装任何东西

详见 [agnes2api/README.md](agnes2api/README.md)。

## 说明

- 这些项目是我个人在 Windows 上用的工具，按「能跑、可读、可维护」写，不是通用框架。
- 仓库里的 `config.json`、`stats.json`、`gallery/` 都已忽略——**密钥不进仓库**。
