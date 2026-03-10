# CPA Auth-files CLI

一个**单文件 Python 脚本**（零第三方依赖），用于管理 **CLIProxyAPI (CPA)** 的管理端 `auth-files`：

- 列出 / 查询 `auth-files`
- 对指定条目一键 `enable/disable`
- **批量**禁用 `plan_type=free` 的账号：只保留必要的 **N 个**（默认 5 个）仍为 enabled

> 安全策略（默认行为）  
> - `prune-free` 默认 **dry-run**，必须加 `--apply` 才会真正修改。  
> - `disable` 默认只允许禁用 **free**；遇到 `team/plus` 等非 free 会直接拒绝，除非你显式加 `--force`。  

---

## 依赖

- Python 3.8+（推荐 3.10+）
- 不需要安装任何 pip 包

---

## 快速开始

1) 准备环境变量（推荐使用 `.env`）

```bash
cp .env.example .env
```

编辑 `.env`，填入你的 CPA 管理端 token：

```bash
CPA_MANAGEMENT_BASE_URL=http://127.0.0.1:8317
CPA_MANAGEMENT_TOKEN=your_token_here
```

2) 直接运行（推荐用 `run.sh` 一键执行）

```bash
./run.sh --help
./run.sh list --limit 20
./run.sh show ACCOUNT_QUERY
```

如果你不想用 `run.sh`：

```bash
python3 cpa_authfiles.py --help
```

---

## 常用命令

### 1) 列出（list）

```bash
./run.sh list --plan team --enabled-only
./run.sh list --plan free --enabled-only --limit 50
./run.sh list --contains KEYWORD --limit 20
```

### 2) 查看详情（show）

```bash
./run.sh show ACCOUNT_QUERY
./run.sh show AUTH_FILE_NAME.json
./run.sh show KEYWORD --contains --all-matches
```

### 3) 启用/禁用（enable/disable）

```bash
./run.sh disable ACCOUNT_QUERY --dry-run
./run.sh disable ACCOUNT_QUERY

./run.sh enable ACCOUNT_QUERY
```

禁用非 free（例如 team/plus）需要显式 `--force`：

```bash
./run.sh disable ACCOUNT_QUERY --force
```

### 4) 批量禁用 free，只保留 N 个（prune-free）

默认 dry-run（安全）：

```bash
./run.sh prune-free --keep 5
```

真正执行（危险）：

```bash
./run.sh prune-free --keep 5 --apply
```

建议先加一个安全上限，例如本次最多禁用 100 个：

```bash
./run.sh prune-free --keep 5 --apply --max-disable 100
```

---

## 一键脚本

- `run.sh`：通用入口（自动读取 `.env`）
- `run_prune_free.sh`：快捷执行 prune-free（第一个参数为 keep，默认 5）
- `run_enable.sh` / `run_disable.sh`：快捷启用/禁用
- Windows：`run.ps1`

示例：

```bash
./run_prune_free.sh 5          # dry-run
./run_prune_free.sh 5 --apply  # apply
```

---

## 发布到 GitHub

仓库内置了一键发布脚本：`scripts/publish_public_repo.sh`

1) 准备 `.env.github`（不会被 git 提交）

```bash
cp .env.github.example .env.github
```

编辑 `.env.github`，填入你的 GitHub PAT：

```bash
GITHUB_TOKEN=your_token_here
GITHUB_PUBLIC=true
GITHUB_REPO=cpa-authfiles-cli
```

2) 一键创建 public repo 并 push：

```bash
./scripts/publish_public_repo.sh
```

---

## 免责声明

请确保你对目标 CPA 管理端拥有合法授权，并遵守相关服务条款与法律法规。
