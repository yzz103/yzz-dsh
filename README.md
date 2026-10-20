# yzz-dsh

DSH（DeepSeek Harness）相关的自用小工具合集。**一个仓库装多个项目，每个项目一个顶层文件夹**，
互相独立、各自带 README / CHANGELOG / pyproject / 测试。

| 项目 | 一句话 | 语言 / 依赖 | 测试 |
|---|---|---|---|
| [agnes2api](agnes2api/) | Agnes AI 生图的多账号本地网关：OpenAI 兼容 API + 管理台 + MCP 工具 | Python 3.9+（纯标准库） | 34 项 |
| [github-mcp](github-mcp/) | 给 MCP 客户端用的 GitHub 连接器：建仓库、批量推文件、读文件、看配额 | Python 3.9+（纯标准库） | 37 项 |

## 目录结构

```
yzz-dsh/
├─ README.md            这个索引
├─ LICENSE              MIT
├─ .gitignore           全仓库通用忽略规则
├─ agnes2api/           项目一：Agnes 生图网关
│  ├─ README.md         能力、部署、接口、配置
│  ├─ CHANGELOG.md      版本历史（1.0.0 → 1.4.0）
│  ├─ pyproject.toml    PEP 621 元数据 + 命令行入口
│  ├─ agnes2api.py      网关主程序
│  ├─ mcp_agnes.py      生图 MCP 连接器
│  ├─ gen_image.py      命令行出图小工具
│  ├─ install_mcp_config.py  把 MCP 条目写进 DSH profile
│  ├─ tests/            离线回归测试（假上游，不联网、不花钱）
│  └─ release/          发行包模板（启动脚本、使用说明）
└─ github-mcp/          项目二：GitHub 连接器
   ├─ README.md
   ├─ CHANGELOG.md      版本历史（1.0.0 → 1.0.4）
   ├─ pyproject.toml
   ├─ mcp_github.py     MCP 服务（5 个工具）
   ├─ install_mcp_config.py
   └─ tests/            离线回归测试（假 GitHub，不联网）
```

## 快速开始

两个项目都**零第三方依赖**，装个 Python 3.9+ 就能跑。

```bash
# 项目一：起本地生图网关（127.0.0.1:7871，OpenAI 兼容）
cd agnes2api && python agnes2api.py serve

# 项目二：把 GitHub 连接器接进 MCP 客户端（或直接用提供的 JSON 片段）
cd github-mcp && python mcp_github.py
```

自测（**都不联网、不花钱、不动真实账号**）：

```bash
cd agnes2api  && python tests/test_gateway.py       # 34 项
cd github-mcp && python tests/test_mcp_github.py    # 37 项
```

## 关于 agnes2api

把 **Agnes AI 的图片接口**包成一个**本机 OpenAI 兼容网关**：

- **多账号池**：多个 Key 轮换，429 / 额度耗尽自动切号，冷却期自动避开
- **自动识别上游**：加号时只填 Key，自动判断这把 Key 属于国际站（`apihub.agnes-ai.com`）
  还是国内站（`api.agnes-ai.cn`），选错还能自动纠正
- **分档限流**：按 1K / 2K / 3K / 4K 分别控每分钟张数，也能给单个账号单独设上限
- **管理台**：网页上看账号状态、用量、生成记录（缩略图墙，可筛选 / 删除 / 清空）
- **请求流水**：控制台每笔请求一行，含耗时、尺寸、体积、文件名
- **MCP 工具**：`generate_image` / `list_images` / `gateway_status` / `delete_image`
- **零依赖**：只要 Python 3.9+，不用 pip 装任何东西

详见 [agnes2api/README.md](agnes2api/README.md)。

## 关于 github-mcp

不依赖 GitHub 官方的远程 MCP 端点（那个只认静态 header，且对令牌类型有限制），
直接打 GitHub REST API，暴露 5 个工具：`github_status` / `github_push_files` /
`github_create_repo` / `github_get_file` / `github_list_repos`。

详见 [github-mcp/README.md](github-mcp/README.md)。

## 约定

- **版本号三处一致**：`<项目>.py` 里的版本常量、`pyproject.toml` 的 `version`、`CHANGELOG.md` 的标题。
- **提交信息**遵循 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)：
  `feat(scope): …` / `fix(scope): …` / `docs(scope): …`。
- **测试必须离线**：自测用假上游 / 假凭据，不碰真实服务、不消耗额度。
- **密钥不进仓库**：`config.json`、`stats.json`、`gallery/`、`.credentials.yaml` 全部忽略。

## 许可

[MIT](LICENSE) © 2026 yzz103
