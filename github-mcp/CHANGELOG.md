# Changelog · github-mcp

本文件记录本项目所有值得注意的变更。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

版本号与 `mcp_github.py` 里的 `SERVER_VERSION` 常量、以及 `pyproject.toml` 的 `version`
**三处必须一致**。

---

## [Unreleased]

### 修复
- **`install_mcp_config.py` 会吃掉前面别的 MCP 段落**：`strip_block()` 旧实现把「顶层注释行」
  当成上一个 insert 块的内容，于是紧跟其后的段落被整段删掉（实测：跑一次 Agnes 的安装脚本，
  本项目的 `mcp-github` 条目从 `cordis.patch.yml` 消失，设置 → 连接器里只剩 agnes）。
  块边界改为「缩进回到 0 的非空行」。
- **找不到 profile**：DSH 注入的 `DSH_PROFILE_DIR` 指向 profile 目录本身，
  旧代码按「上级目录 + profile 名」拼会多一层 `desktop`，直接报「找不到 profile patch」。

> 本次只动安装脚本，服务端 `SERVER_VERSION` 无变化。

---

## [1.0.8] - 2026-10-20

### 变更

- **发布策略改了（用户 2026-10-20 指令）：新项目不自动发，已发布项目更新自动推。**
  `push_project.py` 现在自己做发布状态判定 —— 远端仓库里已有同名顶层文件夹就算「已发布」，
  更新时**直接提交推送**，不再要 `--yes` 二次确认；远端没有同名文件夹的新项目则**默认拦住**
  （退出码 **5**），必须用户明确说「提交 / 上传」后加 `--new` 才发。
  - 新增 `--status`：列远端仓库的顶层项目文件夹，即「已发布」清单（判定依据）。
  - 默认提交信息按状态分开：新项目 `feat(<name>): 首次发布`，
    已发布项目 `chore(<name>): 更新（N 个文件）`。
  - `--yes` 保留但已无作用（兼容旧命令）。
- **修一个潜伏崩溃**：`GitHub.contents()` 对不存在的路径是抛 `HTTP 404` 而不是返回 `None`，
  旧代码直接调用 → 新项目（以及空仓库）会崩栈。新增 `remote_exists()` 吃掉 404。
- `push_project.py` 开头把 stdout/stderr 强制成 UTF-8（Windows 下输出被管道接走时，
  cp936 编码会让中文在按 UTF-8 读的宿主里变成乱码）。

## [1.0.7] - 2026-10-21

### 变更

- **可移植性：别人克隆下来就能用**，不再依赖本机目录。
  - `install_mcp_config.py` 改用 `sys.executable` 与脚本自身所在目录定位解释器和服务脚本，
    profile 走 `--profile` / `DSH_PROFILE_DIR`（默认 desktop），并新增 `--remove`。
  - `push_project.py` 的本机专属路径从「只提示」升级为**硬门禁**：扫到盘符开头的本机安装
    路径即中止（退出码 4），确要发布才加 `--allow-local-paths`；`--repo` 支持 `GH_REPO`
    环境变量；文档示例改成中性写法，脚本自身不会再被自己的门禁误伤。

## [1.0.6] - 2026-10-20

### 修复

- **单次请求层重试**：`GitHub._request()` 遇到网络层错误（断连 / `SSL: UNEXPECTED_EOF_WHILE_READING` /
  超时）自动重试最多 4 次，间隔 1s / 2s / 4s。此前只在整次推送外重试，而一次推送要发十几个请求，
  任何一环断掉就整次重来 —— 实测三个整轮重试全部失败，改到单请求层重试后才推成功。
  失败时的报错也带上重试次数：`网络不通（已重试 4 次）：…`。

### 测试

- 37/37 通过（假服务端不触发重试路径，行为无变化）。

---

## [1.0.5] - 2026-10-20

### 新增

- `push_project.py`：把「把一个项目目录推上仓库」这件事做成一条命令——
  体检骨架（README / CHANGELOG / pyproject / tests）、密钥扫描（命中即中止，只报行号不报内容）、
  本机路径提示、按 `MAX_FILES` 分批推送、自动加 `<项目名>/` 前缀。
  支持 `--dry-run`（只出计划不联网）、`--allow-missing`、`--name`、`--yes`。
  典型用法：`python push_project.py <项目目录> --dry-run`。
- 推送遇到瞬时网络断连（`HTTP 0`）时**自动重试两次**（等 2s / 4s），覆盖读仓库、查目录、
  建 blob / tree / commit 全过程。本机到 `api.github.com` 实测偶发
  `SSL: UNEXPECTED_EOF_WHILE_READING`，第一次跑就撞上过一次。

### 变更

- 版本常量升到 1.0.5（MCP 服务本身行为未变，仅随项目新增脚本同步）。

---

## [1.0.4] - 2026-10-20

### 修复

- `github_get_file` 的 `path` 传空串时报「path 必填」，而按工具描述空串应当表示
  **仓库根目录**。现在空串会列出顶层条目（文件 + 目录）。

### 测试

- 37/37 通过。假服务端补齐 `GET /contents`（单文件回 base64 正文、目录回条目列表），
  用例 [10] 扩成 5 条断言：根目录列表、读文件正文、目录列表、不存在的路径、缺 repo 参数。

---

## [1.0.3] - 2026-10-20

### 新增

- 抽出模块级 `encode_content(content, encoding)`：`utf-8` / `base64` / `path`（读本地文件），
  超 `MAX_BLOB`（20 MB）时明确报错。`blob()` 改为复用它。

### 修复

- `ref_sha()` 把 **409** 也当「没有 ref」（此前只认 404）。空仓库读 ref 会回 409
  `Git Repository is empty`，不处理会直接抛错。

### 测试

- 33/33 通过。假服务端补齐：空仓库建 blob 回 409、`PUT /contents` 建首个提交并置上 ref、
  空仓库读 ref 回 409；无令牌用例改用临时 `HOME` 隔离，避免读到真实凭据而假通过。

---

## [1.0.2] - 2026-10-20

### 修复

- **空仓库无法推送**：对「一个提交都没有」的仓库，`POST /repos/{r}/git/blobs` 一律回
  `409 Git Repository is empty.`（GitHub 硬限制，不是权限问题）。
  现在先用 **Contents API** `PUT /repos/{full}/contents/{path}` 写第一个文件把仓库「激活」，
  其余文件再走 Git Data API。
  **代价**：空仓库的首次推送会产生两个提交（「初始化仓库」+ 正常提交）。

---

## [1.0.1] - 2026-10-20

### 修复

- 推送结果的体积统计错误：`encoding: path` 时把**本地路径字符串的长度**当成了字符数，
  显示成「约 658 字符」。新增 `human_size()`，改为累加真实文件字节数。

---

## [1.0.0] - 2026-10-20

### 新增

- 首个版本：把 GitHub REST API 包成 MCP stdio 服务，纯标准库、零依赖。
- 5 个工具：`github_status`、`github_push_files`、`github_create_repo`、
  `github_get_file`、`github_list_repos`。
- 六路令牌查找：`--token` → `GITHUB_TOKEN` → `GH_TOKEN` → `--token-file` →
  `~/.dsh/.credentials.yaml` 的 `refs.GITHUB_TOKEN` → 本机 `gh auth token`。
- 支持一次提交多个文件、自动建分支、空仓库首提交。

### 测试

- 29/29 通过：内置假 GitHub（随机端口）+ 子进程驱动 JSON-RPC，不联网。
