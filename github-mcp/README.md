# github-mcp

给 DSH（或任何 MCP 客户端）用的 **GitHub 连接器**：建仓库、批量推文件、读文件、看配额。

不依赖 GitHub 官方的远程 MCP（`api.githubcopilot.com`），**直接打 GitHub REST API**，
纯 Python 标准库，零第三方依赖。

## 为什么自己写

- 官方远程 MCP 需要 OAuth 能力，而 DSH 的 `dsh-mcp-client` 只支持静态 `Authorization` 头
- 现成的社区插件（`dsh-github-mcp`）是薄封装，仍然依赖那个远程端点
- 自己写的好处：令牌来源更灵活（gh CLI / 环境变量 / DSH 凭据中心都能用）、行为可读可改、离线可测

## 文件

| 文件 | 作用 |
|---|---|
| `mcp_github.py` | MCP 服务本体（stdio 行协议，约 640 行） |
| `tests/test_mcp_github.py` | 离线测试：内置假 GitHub（本地随机端口），37 项用例，不联网 |
| `install_mcp_config.py` | 写入 / 撤销 DSH profile 的 MCP 条目 |
| `push_project.py` | 一键发布：体检骨架 → 密钥扫描 → 分批推到 `<项目名>/`（支持 `--dry-run`） |
| `pyproject.toml` | PEP 621 元数据 + 命令行入口 |
| `CHANGELOG.md` | 版本历史 |

## 克隆即用

没有写死本机路径：服务脚本与 `install_mcp_config.py` 都用**自身所在目录**定位，
profile 走 `--profile` / `DSH_PROFILE_DIR`，令牌按下面六种方式找。
换台机器只要 Python 3.9+（纯标准库），克隆下来 `python tests/test_mcp_github.py` 应全绿。

## 令牌（按顺序找，命中即用）

1. 命令行 `--token`
2. 环境变量 `GITHUB_TOKEN`
3. 环境变量 `GH_TOKEN`
4. 参数 `--token-file` 指向的文件
5. `~/.dsh/.credentials.yaml` 的 `refs.GITHUB_TOKEN`
6. 本机 `gh auth token`（装了 gh 且已登录就自动可用）

推荐用 gh 的网页授权，省得手工拷令牌：

```bash
gh auth login --web --git-protocol https
```

> 本机 `github.com` 直连超时的话，给命令加上 `HTTPS_PROXY=http://127.0.0.1:7897`
> （gh 的下载与授权都要走代理），但**本服务只打 `api.github.com`，不需要代理**。

## 五个工具

| 工具 | 作用 |
|---|---|
| `github_status` | 令牌来源 / 账号 / 作用域 / 剩余配额 |
| `github_create_repo` | 建仓库（默认私有，支持 `org/名字`） |
| `github_push_files` | 一次提交多个文件（Git Data API；空仓库也能首提交） |
| `github_get_file` | 读文件内容；`path` 给目录则列条目 |
| `github_list_repos` | 列可见仓库 |

`github_push_files` 的 `encoding`：

- `utf-8`（默认）文本
- `base64` 二进制
- `path` → `content` 填**本机绝对路径**，服务自己读盘（大文件/二进制最方便）

限制：单文件 ≤ 20 MB，单次 ≤ 100 个文件。

### 空仓库的处理

GitHub 对「还没有任何提交」的仓库，`POST /git/blobs` 会直接返回
`409 Git Repository is empty`。所以本服务遇到空仓库时：

1. 用 Contents API（`PUT /repos/{r}/contents/{path}`）写第一个文件，把仓库「激活」
2. 再用 Git Data API 提交剩下的文件

结果是空仓库会有**两个提交**：一个初始化提交 + 一个正常提交。这是 GitHub 的硬限制，不是 bug。

## 接进 DSH

```bash
python install_mcp_config.py            # 写入（幂等）
python install_mcp_config.py --remove   # 撤销
```

工具名形如 `mcp__github__github_push_files`。

两个要点：

1. profile 的 patch 是覆盖层，**新条目必须写 `- insert:`**，写 `- id:` 只会被静默跳过
2. YAML 里 Windows 路径必须用单引号

新增 insert 条目**热生效**（约 10 秒后 mcp-client 自动起子进程），不用重启 DSH。

## 接进别的 MCP 客户端

```json
{
  "mcpServers": {
    "github": {
      "command": "python",
      "args": ["/path/to/mcp_github.py"],
      "env": {"GITHUB_TOKEN": "ghp_xxx", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    }
  }
}
```

## 一键发布一个项目

不想在对话里拼 `files[]` 的话，用 `push_project.py`（同目录，复用本服务的推送逻辑）：

```bash
python push_project.py --status                  # 先看仓库里已发布了哪些项目
python push_project.py <项目目录> --dry-run       # 看计划：推哪些、多大、缺什么
python push_project.py <项目目录>                 # 已发布项目：直接更新
python push_project.py <新目录> --new             # 新项目：只在用户明确要求发布时才加
```

**发布策略（按状态自动判定）**：

| 项目状态 | 判定 | 行为 |
|---|---|---|
| 已发布（远端已有 `<项目名>/` 顶层文件夹） | 自动 | **更新后直接提交推送**，不需二次确认 |
| 新项目（远端没有该文件夹） | 自动 | **不自动发布**，退出码 **5**；用户明确说要提交才加 `--new` |

它会依次做四件事：

1. **密钥扫描**（`sk-` / `adm-` / `gh[oprsu]_` / `AKIA` / `Bearer 长串`）——命中就中止，
   只打印「文件:行号」，不打印内容；
2. **骨架体检**：`README.md`、`CHANGELOG.md`、`pyproject.toml`、`tests/`（缺了默认拦住，
   `--allow-missing` 可放行）；
3. **本机路径硬门禁**：扫出盘符开头的本机安装路径（APP / DSH / Program Files），命中即中止（退出码 4），确认要发才加 `--allow-local-paths`；
4. **发布状态判定 + 分批推送**：远端有同名文件夹 → 自动更新；没有 → 需 `--new`。
   推送按 `MAX_FILES`（100）切块，路径自动加 `<项目名>/` 前缀。

常用参数：`--name`（改名）、`--repo`（换仓库）、`--message`（提交信息）、`--branch`。

## 自测

```bash
python tests/test_mcp_github.py
```

内置假 GitHub，不联网、不动真仓库，覆盖握手、工具清单、建仓库、空仓库初始化（409 → Contents API）、
增量提交、新分支、四类错误路径、无令牌提示、读文件与目录。当前 **37/37 通过**。
