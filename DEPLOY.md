# 部署记录与步骤

## 当前状态

2026-09-26 10:45（Asia/Shanghai）：**归档已迁移至存储卷 `/vol2/1000/10.Develop/reports-fetcher/reports`（SMB 共享 `\fnos.Develop` 内），v1.0.5 服务运行正常。** v1.0.5（`8069fb0`）为此前当日两连发的最终版本；v1.0.1–v1.0.4 上线记录作为历史保留在下文。存储迁移详情见文末「归档迁移到存储卷」一节。

服务目录：`/home/chen/dev/reports-fetcher`；宿主入口：`http://127.0.0.1:8000`，仅服务器本机监听；其他机器通过 SSH 隧道调用（下文有命令）。本次采用回环本地模式，未配置应用令牌或对外发布端口。

最初审查 `709bb72`（v1.0.0）未通过，因 T1–T7 暂缓部署；修复后从 v1.0.1 起持续按 release 目录升级。下文按版本保留环境检查、阻断与上线凭证，最新记录为文末 v1.0.3。

## 已执行：首次环境预检查（部署前）

目标：`chen@192.168.1.150`；指定父目录 `/home/chen/dev`；拟部署独立目录 `/home/chen/dev/reports-fetcher`。

| 检查 | 实际结果 |
|---|---|
| SSH | BatchMode 登录成功，ConnectTimeout=10，无交互凭据提示 |
| 系统 | Linux fnOS，`6.18.18.c877-trim`，x86_64 |
| 用户 | chen，uid=1000；属于 docker 组，可访问 Docker daemon |
| 父目录 | `/home/chen/dev` 存在，chen:Users，`drwxrwxrwx`，可写 |
| 应用目录 | `/home/chen/dev/reports-fetcher` 尚不存在，无旧版本需替换 |
| 磁盘 | 所在分区约 63G，总已用 20G，可用 40G |
| 内存 | 15Gi 总内存，约 9Gi available；swap 4Gi，总已用约 422Mi |
| Docker | `/usr/bin/docker`，Server 28.5.2 |
| Compose | v2.40.3 |
| 基础镜像 | 已有 `python:3.12-slim`，约 127MB |
| 工具 | `/usr/bin/git`、`/usr/bin/curl` 可用 |
| 监听端口 | TCP 8000 未占用；已有其他容器和服务，不改动其端口、镜像或目录 |
| 目标机出网 | 首次检查未验证；本次远端构建及三市场 HTTP 抓取全部通过，详见最终上线记录 |

执行过的检查包括：

```powershell
ssh -o BatchMode=yes -o ConnectTimeout=10 chen@192.168.1.150 'uname -a; id; pwd; ls -ld /home/chen/dev; df -h /home/chen/dev; command -v docker; docker version --format "{{.Server.Version}}"; docker compose version; ss -ltn'
ssh -o BatchMode=yes -o ConnectTimeout=10 chen@192.168.1.150 'ls -la /home/chen/dev; docker ps --format "{{.Names}} {{.Image}} {{.Ports}}"; docker image ls --format "{{.Repository}}:{{.Tag}} {{.Size}}"; free -h; command -v git; command -v curl'
```

目录权限较宽，实际应用目录和配置按单独目录管理；不批量改变父目录或其他项目权限。

## 历史：v1.0.0 部署前验证

开发机 Docker Server 29.8.0，Compose v5.5.1；所有 Python 执行在 Docker 内。

```powershell
docker compose run --rm --no-deps -T test python -m pytest tests/test_core_pipeline.py tests/test_store.py tests/test_jobs.py tests/test_api.py -q
```

结果：85 passed，17.82 秒；2 条依赖弃用警告。另做 2 请求规模的定向边界复现，证实任务书 T1/T2/T3/T5/T6/T7 的行为；T4 为代码与设计契约比对发现。

真实抓取抽查：本机 Docker 隔离临时归档中，600519 / 0700.HK / AAPL 各 1 份，全部下载、校验、归档成功，耗时 3.73 / 10.35 / 4.33 秒。只降低该次探测的超时/重试预算，未改产品配置；SEC 使用真实联系邮箱，未留存到文档。没有覆盖现有报告文件。

上述结果证明正常抓取可用，**不足以覆盖常驻、并发和报告语义边界**；部署门禁未通过。

## 发现的问题与处理状态

本表保留问题及首次判定；T1–T7 已在 `76fee13` 修复并复审通过，当前没有阻断本次回环部署的问题。

| 问题 | 处理/部署影响 |
|---|---|
| T1：加锁前执行恢复，能改写其他实例的在途意图 | 原 P1，已修复；额外真实第二进程验证拒绝写入且在途状态/文件不变 |
| T2：SEC submissions 永久缓存 | 原 P1，已修复；额外确认同服务 refresh=True 能发现来源新申报 |
| T3：并发幂等冲突、队列容量超限 | 原 P1，已修复；2 请求并发边界通过 |
| T4：仅凭 `/A` 把修订标记为全文 | 原 P1，已修复；未证实完整性的修订不替代原全文 |
| T5：HK 仅凭年份猜造期末 | 原 P1，已修复；真实 HK 下载 period=null，保留警告 |
| T6：无可用文件仍 partial、正常截取也 partial | 原 P1，已修复；状态矩阵通过，真实 CN/US 均 succeeded |
| T7：docs/OpenAPI/ReDoc 未受 token 保护 | 原 P2，鉴权定向测试通过；实际部署为回环本地模式 |
| 配置 bind mount | 已先创建 shared/config.toml 普通文件，再启动容器；保持固定路径持久化 |
| 访问范围 | 默认仅宿主 `127.0.0.1:8000`，其他机器不能直接访问 `192.168.1.150:8000`；先通过 SSH 隧道调用，局域网直接接入需配置令牌与 HTTPS 入口 |
| 首次解包命令中的包名 | 初次命令遗漏 `reports-fetcher-` 前缀，hash 检查即失败，未执行解包；按上传文件实际名称修正并校验通过后继续，无服务或数据影响 |
| 验收文档 | 已补充独立复审和远端部署结果；原 v1.0.0 验收数据作为历史保留 |

## 部署步骤（本次已执行，保留为后续发布流程）

本次在 T1–T7 修复复审通过后执行。后续升级仍须先固定通过验收的新提交；以下通用变量示例应替换为实际发布版本。本次固定入口脚本和命令见文末。

### 1. 固定待发布版本与传输包

记录修复后的完整 Git SHA。先确认业务修复已提交且通过复审；使用 `git archive` 导出这一提交，避免携带开发机归档库、本地配置或密钥。文档未提交时也不会自动包含在 archive 中，部署记录仍保留本地并在最终提交同步。

开发机 PowerShell 示例（`$releaseSha` 必须为实际验收通过的提交）：

```powershell
$releaseSha = git rev-parse HEAD
$releaseTar = Join-Path $env:TEMP "reports-fetcher-$releaseSha.tar"
git archive --format=tar --output=$releaseTar $releaseSha
ssh chen@192.168.1.150 'mkdir -p /home/chen/dev/reports-fetcher/releases /home/chen/dev/reports-fetcher/shared/reports'
scp $releaseTar chen@192.168.1.150:/home/chen/dev/reports-fetcher/releases/
```

登录目标机后，在其原生 shell 中设定经验证的 `release_sha`，创建全新 `/home/chen/dev/reports-fetcher/releases/$release_sha` 目录，再把同名 tar 解压到该目录。若该目录已存在，先检查来源与内容，不覆盖未知目录。用 `sha256sum` 与开发机 `Get-FileHash` 核对传输包。此流程不删除或移动任何旧版本。

### 2. 创建配置与稳定数据路径

从该 release 的 `config.example.toml` 首次复制生成 `/home/chen/dev/reports-fetcher/shared/config.toml`；确认其为普通文件，保留 `out_dir="./reports"`。已有配置先检查再复用，不静默覆盖。

在 `/home/chen/dev/reports-fetcher/shared/.env` 配置真实 `SEC_UA_EMAIL`。令牌模式用 `RF_API_TOKENS=client:token`，凭据不提交、不写日志或本文。可在原生 shell 以 `umask 077` 创建新配置；不递归修改目录权限。部署期间不要输出会展开令牌的完整 `docker compose config`。

在应用根目录创建 `compose.deploy.yml`，示例：

```yaml
services:
  serve:
    image: reports-fetcher:${RF_RELEASE}
    build:
      context: ${RF_RELEASE_DIR}
      target: server
    volumes:
      - /home/chen/dev/reports-fetcher/shared/reports:/app/reports
      - /home/chen/dev/reports-fetcher/shared/config.toml:/app/config.toml:ro
```

它与 release 内原有 compose 合并，相同容器目标路径的挂载由此文件覆盖，数据不随 release 目录切换。保留基础 compose 的 `127.0.0.1:8000:8000` 和单 worker，不另起 CLI 共享写入同一归档目录。

### 3. 构建与启动

以下在目标机原生 shell 执行，`release_sha` 为步骤 1 的同一提交；先检查没有既有同名项目，所有命令固定项目名，避免影响其他应用。

```sh
export RF_RELEASE="$release_sha"
export RF_RELEASE_DIR="/home/chen/dev/reports-fetcher/releases/$release_sha"
rf_compose() {
  docker compose -p reports-fetcher \
    --env-file /home/chen/dev/reports-fetcher/shared/.env \
    -f "$RF_RELEASE_DIR/docker-compose.yml" \
    -f /home/chen/dev/reports-fetcher/compose.deploy.yml "$@"
}
test -f /home/chen/dev/reports-fetcher/shared/config.toml
rf_compose config --quiet
rf_compose build serve
rf_compose up -d --no-deps serve
rf_compose ps serve
rf_compose logs --tail 100 serve
```

每条命令成功后才执行下一条。记录镜像 ID、构建时间、启动日志摘要。pip/registry 出网失败按实际失败记录，不能把开发机镜像存在当作远端构建成功。

### 4. 少量 HTTP 冒烟及持久化验证

具体请求字段和路由以 [HTTP_API.md](HTTP_API.md) 及修复后 OpenAPI 为准。在目标机访问 `http://127.0.0.1:8000`；令牌模式携带 Bearer 凭据。

1. 健康检查返回正常；查看状态和日志确认不是容器重启循环。
2. POST `/api/v1/fetch-jobs`，独立 `Idempotency-Key`，`symbols=["600519","0700.HK","AAPL"]`、`last_n=1`；轮询任务直到终态，逐项确认 3 份原文可用。这一步同时验证目标机来源出网。真实未知期可有明确质量警告，不能把下载错误当成可接受 partial。
3. 相同 key/body 重放，job_id 不变；换 key 再取同样报告应复用已归档文件。
4. 按接口返回的 report_id 查询 metadata 并下载文件，核对 Content-Type、非空正文和已记录 SHA-256；测试缺失 key 和无效参数分别符合 400/422。
5. 任务结束后 `rf_compose restart serve`，健康恢复后仍可查询任务并下载原文件。不要在真实在途下载时用 CLI 测锁，锁边界应已在隔离测试验证。

不重复压力测试或批量下载。源站失败要记录实际域名、错误码及重试情况，不记录机密 headers。最终回填本节执行时间和结果；未执行的项继续标“待执行”。

### 5. 访问方式与回滚

默认目标机调用：`http://127.0.0.1:8000`。开发机/其他受控调用方可以建立 SSH 隧道：

```powershell
ssh -N -L 18000:127.0.0.1:8000 chen@192.168.1.150
```

随后通过 `http://127.0.0.1:18000` 调用。需要其他应用通过局域网地址直接调用时，使用应用令牌和 HTTPS 反向代理，并验证 T7；不要仅把端口绑定改成 `0.0.0.0`。

首次部署若冒烟失败：用同一 compose 定义 `stop serve`，保留配置、日志和归档以便排查。升级时保留上个已通过 release 和镜像；停止服务后可复制备份整个 shared/reports（含 SQLite，备份期间禁止写入），不删除源文件。回滚前确认 schema 与旧版本兼容；若不兼容，保持停服并制定恢复步骤，不能让旧程序直接打开新 schema。兼容时将 RF_RELEASE/RF_RELEASE_DIR 指回上次通过版本，再 `up -d --no-deps serve` 并做健康/读取检查。

任何递归删除或移动不属于本步骤；需要清理旧版本时按用户文件安全规则另行确认精确目标。

## 最终上线记录（2026-09-21）

### 复审与版本

- 已复审提交：`76fee13be63fe727e7476c739cfad9ba39a6fa45`（v1.0.1），业务代码未在部署期间修改。
- 定向测试：`test_store.py test_jobs.py test_api.py test_core_pipeline.py test_us_edgar.py test_hk_hkexnews.py test_selection.py`，**178 passed，21.52 秒**，2 条测试依赖弃用警告。
- 另在独立 Docker 临时库补验：真实子进程打开已持锁 Store 被拒绝，registered 意图与临时文件保持原样；同一个常驻 JobService 先读旧 fixture，再以不同 key、`refresh=True` 读取完整 fixture，成功取得新 source_id，submissions 请求次数为 2。均通过。
- 未重复全量测试、未压力测试、未开展二期内容分析。

### 构建、路径与启动

| 项目 | 实际值 |
|---|---|
| 首次启动 | 2026-09-21 15:36:53 +08:00 |
| 冒烟与重启复核完成 | 2026-09-21 15:38:16 +08:00 |
| 源码 release | `/home/chen/dev/reports-fetcher/releases/76fee13be63fe727e7476c739cfad9ba39a6fa45` |
| 上传包 | `releases/reports-fetcher-76fee13be63fe727e7476c739cfad9ba39a6fa45.tar`，768000 bytes |
| 包 SHA-256 | `2883304269fd7e85d9f51b1dcaf3acd3cbeb490c4984dce33d330b7be71b1f09`（本地 Get-FileHash 与远端 sha256sum 一致） |
| 镜像标签 | `reports-fetcher:76fee13be63fe727e7476c739cfad9ba39a6fa45` |
| 镜像 ID | `sha256:b8078dfeef95bbf17eef4ab8a6d755bc9c691ad022f79823d7c1994dda6a07ce` |
| 容器 / Compose 项目 | `reports-fetcher-serve-1` / `reports-fetcher` |
| 归档与数据库 | `/home/chen/dev/reports-fetcher/shared/reports` |
| 配置与环境 | `shared/config.toml`、`shared/.env`；SEC 真实联系邮箱已注入，未写入日志或本文 |
| 固定运维入口 | `/home/chen/dev/reports-fetcher/compose-release.sh`，用 `sh` 调用，不依赖当前目录 |
| 端口 / 模式 | `127.0.0.1:8000 -> container:8000`，`RF_LOCAL_MODE=1`，无应用令牌 |
| 运行策略 | 单 worker，`restart: unless-stopped`；最终 running，自动重启计数 0 |

构建在目标机完成，使用其已有 `python:3.12-slim` 和清华 pip 源，安装步骤约 72 秒，无构建错误。实际依赖包括 requests 2.34.2、FastAPI 0.141.1、Uvicorn 0.53.0、Pydantic 2.13.5。保留本次镜像作为可复用制品；未来重新构建可能解析到不同依赖版本。

本次已执行的运维命令（在目标机）：

```sh
sh /home/chen/dev/reports-fetcher/compose-release.sh config --quiet
sh /home/chen/dev/reports-fetcher/compose-release.sh build serve
sh /home/chen/dev/reports-fetcher/compose-release.sh up -d --no-deps serve
sh /home/chen/dev/reports-fetcher/compose-release.sh ps serve
curl --fail --silent --show-error http://127.0.0.1:8000/health/ready
# 所有抓取任务终态后执行一次受控重启
sh /home/chen/dev/reports-fetcher/compose-release.sh restart serve
sh /home/chen/dev/reports-fetcher/compose-release.sh logs --tail 12 serve
```

`compose-release.sh` 固定上述 SHA 和 release 目录，再组合原始 compose、根目录 compose.deploy.yml 与 shared/.env。**升级时必须更新脚本的版本绑定**，不能仅把新 release 上传后继续调用旧脚本。

### HTTP 冒烟结果

宿主机 curl 验证发布端口的健康接口；完整 HTTP 调用脚本通过 SSH 在服务器容器内运行 requests，访问真实运行服务，没有使用 TestClient 或假来源。

| 检查 | 结果 |
|---|---|
| `/health/live`、`/health/ready` | 200 / ok |
| `/openapi.json` | 200，版本 1.0.1 |
| POST 缺少 Idempotency-Key / symbols 空数组 | 400 / 422 |
| 新任务 `deploy-76fee13-smoke-001` | `job_85794434f82f40cfbbb6`；约 20.07 秒；downloaded=3、cached=0、failed=0 |
| 同 key/body 终态重放 | 200，同 job_id |
| 新 key 缓存任务 `deploy-76fee13-cache-001` | `job_4a3211fa38a5412c96c4`；约 8.03 秒；downloaded=0、cached=3、failed=0 |
| metadata / 原文 | 三份均 200，字节数、媒体类型、SHA-256 与档案记录一致 |
| 受控重启后 | 两任务查询 200；报告列表仍有 3 条；3 份原文再次读取并校验 SHA-256 一致；ready=ok |

首次任务与缓存任务均为 **partial**：CN/US 证券 succeeded；HK 证券 partial，仅因报告标题 `中期報告 2026` 无可确认的期末日，`report_period=null` 并明确警告。三份原文均可用，0 下载失败。这是 T5 的正确数据质量行为，不是部署失败。

| 市场 / 报告 ID | bytes | SHA-256 |
|---|---:|---|
| CN / `916deaafc98bfbaf5cfc` | 832321 | `0e10aa26be46b1cf3cd03f06e834c7fb98d5dd0d661b96f8fddd4af7e846a4f6` |
| HK / `2727be0c6689aabad5a3` | 5451089 | `6231374bc3d7bd104fcebb7735f9692050b55be32fa0f34d7d7f20478efa71f1` |
| US / `7b726888c37d9415aafd` | 1018326 | `18e97eeb6606bc98cdbfaa64b66822f3a2483415d4ad4b2ab53e67636e08c689` |

### 调用与后续维护

在服务器上使用 `http://127.0.0.1:8000/api/v1`；其他机器先运行：

```powershell
ssh -N -L 18000:127.0.0.1:8000 chen@192.168.1.150
```

隧道建立后，可在本机访问 `http://127.0.0.1:18000/docs` 或调用 `/api/v1/fetch-jobs`。`192.168.1.150:8000` 不直接对局域网开放。服务日志与重启使用上述固定入口；生产归档目录不要与并行 CLI 共享写入。

本次仅新增 reports-fetcher 自身的目录、配置、镜像、Compose 网络和容器；未改动其他项目、未递归删除/移动文件。保留 3 份冒烟报告和 2 个任务用于部署追溯。本记录另存于服务器应用根目录 `DEPLOY.md`，release 内文件保留原提交快照。

## v1.0.2 升级上线记录（2026-09-22）

### 范围与发布门禁

本次升级处理 agents-manage 生产联调发现的五项问题：HK PDF 正文明示期末日提取及缓存回填、下载文件名、未选中候选 warning 噪音、任务初始 `symbols_total`，以及 integration-kit 契约同步。数据质量规则仍为“未知即 null”；`filing_date` 只用于下载名回退，不作为报告期来源。

- 发布提交及标签：`7f5fbadadc63d1b026019e72c0a2dc9f5edfb40e` / `v1.0.2`。
- 本地 Docker 全量测试：**353 passed，2 warnings，24.00 秒**。
- integration-kit 契约测试：**33 tests OK，23.720 秒**；超时场景产生两条预期的 BrokenPipe 测试日志。
- 使用 HKEX 官方腾讯中期报告 `2026082500557_c.pdf` 和年报 `2026040901232_c.pdf` 做真实 PDF 提取复核，分别得到 `2026-06-30`、`2025-12-31`。
- `git diff --check` 通过；`pdfminer.six 20260107` 在目标镜像内可导入，应用版本命令返回 `reports-fetcher 1.0.2`。

### 环境预检查与制品

升级前确认 SSH、Docker 28.5.2、Compose v2.40.3 和约 40G 可用磁盘正常；v1.0.1 容器持续运行，端口仍为 `127.0.0.1:8000`。复用并保留 `shared/config.toml`、`shared/.env`、`shared/reports`，没有输出凭据，也没有删除或移动旧 release、镜像或归档。

| 项目 | 实际值 |
|---|---|
| 完成时间 | 2026-09-22 17:17:34 +08:00 |
| 源码 release | `/home/chen/dev/reports-fetcher/releases/7f5fbadadc63d1b026019e72c0a2dc9f5edfb40e` |
| 上传包 | `releases/reports-fetcher-7f5fbadadc63d1b026019e72c0a2dc9f5edfb40e.tar`，1054720 bytes |
| 包 SHA-256 | `740b340e15c678d34d1fb58c6286fa03d83c06ee1b4ddf56fd7a58951ae8ba44`（本地与远端一致） |
| 镜像标签 | `reports-fetcher:7f5fbadadc63d1b026019e72c0a2dc9f5edfb40e` |
| 镜像 ID | `sha256:dfb1434ca8cc3e7a8c26b388ac777087db157efcad9d04a08d59bd8dcf3b79db` |
| 容器 / Compose 项目 | `reports-fetcher-serve-1` / `reports-fetcher` |
| 端口 / 模式 | `127.0.0.1:8000 -> container:8000`，回环本地模式 |
| 固定运维入口 | `/home/chen/dev/reports-fetcher/compose-release.sh`，已固定 v1.0.2 SHA |
| 回滚入口备份 | `/home/chen/dev/reports-fetcher/compose-release-76fee13be63fe727e7476c739cfad9ba39a6fa45.sh` |

目标机原生 Docker 构建约 140 秒。先完成新镜像的版本和依赖自检，再备份旧入口并替换固定运维入口，最后执行：

```sh
sh /home/chen/dev/reports-fetcher/compose-release.sh config --quiet
sh /home/chen/dev/reports-fetcher/compose-release.sh build serve
sh /home/chen/dev/reports-fetcher/compose-release.sh up -d --no-deps serve
curl --fail --silent --show-error http://127.0.0.1:8000/health/ready
sh /home/chen/dev/reports-fetcher/compose-release.sh restart serve
```

升级未改变 Compose 项目名、服务名或网络，因此同一 Docker 网络内的调用方仍使用 `http://serve:8000`。宿主端仍仅监听 `127.0.0.1:8000`。

### 生产定向验收

在运行容器内通过真实 HTTP 调用生产服务，未使用 TestClient 或 mock。任务请求为 `symbols=["0700.HK"]`、`last_n=3`、`refresh=false`；只验证本次修复及必要的持久化边界，没有重复三市场抓取或压力测试。

| 检查 | 结果 |
|---|---|
| `/health/ready` / `/openapi.json` | 200 / ok；OpenAPI 版本 1.0.2 |
| 任务 | `job_2563e8787f4042c3a3be`；终态 succeeded |
| 缓存行为 | downloaded=0、cached=3、failed=0，未重复下载 |
| 初始进度 | `symbols_total=1`；首次验收脚本输出前任务已很快完成，终态为 1/1；queued/running 的 1/0 边界由本地测试覆盖 |
| 幂等重放 | 相同 key/body 返回 200 和同一 job_id |
| warnings | 任务与证券 warnings 均为空；未选中候选不污染结果 |
| 2026 中报 | filing_date 2026-08-25；report_period 2026-06-30；period_source=document |
| 2025 年报 | filing_date 2026-04-09；report_period 2025-12-31；period_source=document |
| 2025 中报 | filing_date 2025-08-26；report_period 2025-06-30；period_source=document |
| 文件 | `HK_00700_INTERIM_2026-06-30_2727be0c6689aabad5a3.pdf`；5451089 bytes；SHA-256 与档案一致 |
| 缓存验证 | ETag 为 SHA-256；匹配 `If-None-Match` 返回 304 |
| 受控重启后 | ready=ok；同一任务、三份报告和文件仍可读，校验结果不变 |

### 部署中发现的情况

1. 首次切换后的立即探测返回一次 empty reply；受控重启后的立即探测出现两次 connection reset。容器日志均显示应用正常完成 startup，带连接错误重试的就绪探测随后返回 200。最终容器运行、镜像和归档读取均正常。这是启动窗口内的瞬时连接状态，发布脚本后续应继续使用有界重试。
2. v1.0.1 的固定入口末尾残留一行不可达的 `SH`，位于 `exec docker compose ...` 之后，未影响历史运行。v1.0.2 入口已移除该残行；旧入口按完整 SHA 原样保留用于审计和回滚参考。
3. 首次验收脚本在全部断言通过后，因输出 JSON 使用 tuple key 导致格式化异常；仅影响测试结果打印，不影响服务或数据。修正验收脚本后以同一幂等任务重新执行并完整通过。

截至 v1.0.2 上线记录完成时，生产服务为 v1.0.2。该次未修改其他项目，未执行递归删除、移动或权限变更；旧 release、镜像和启动入口备份均保留。

## v1.0.3 升级上线记录（2026-09-23）

### 范围与发布门禁

本次升级交付 RF-HK-QTR-DEFAULT-001：HK 省略 `forms_by_market` 时默认使用 `ANNUAL/INTERIM/QTR-HK`；季度业绩存在即按报告期参与统一选择，不存在则正常跳过。ANNUAL/INTERIM 在混合选择前使用 Store 可信期回填与冷库有界原文判期，避免已知期季度公告挤掉报告期尚未富化的完整报告。近期完整报告判期失败会形成可追溯 warning/partial，不再静默遗漏。

- 发布提交及标签：`26c648d53133eec646677ca95acafe8a6165c960` / `v1.0.3`，均已推送。
- Docker 定向测试：**92 passed，7.82 秒**。
- Docker 全量测试：**369 passed，2 warnings，26.39 秒**；警告为 FastAPI TestClient 依赖弃用提示。
- integration-kit：**37 tests OK，27.824 秒**；两个超时用例产生预期 BrokenPipe 测试日志。
- `git diff --check` 通过；目标镜像内 `reports-fetcher version` 与 Python 导入均返回 `1.0.3`。

### 环境预检查、制品与切换

升级前确认 SSH 可用，Docker 28.5.2、Compose v2.40.3 正常，目标分区约 40G 可用；v1.0.2 容器健康运行。新镜像构建和版本自检期间旧容器持续服务，完成自检后才切换。共享归档、SQLite、配置和环境文件继续复用，没有输出凭据。

| 项目 | 实际值 |
|---|---|
| 首次切换 | 2026-09-23 10:50:34 +08:00 |
| 配置修正及重启 | 2026-09-23 10:53:15 +08:00 |
| 最终重启验收 | 2026-09-23 10:55:06 +08:00 |
| 源码 release | `/home/chen/dev/reports-fetcher/releases/26c648d53133eec646677ca95acafe8a6165c960` |
| 上传包 | `releases/reports-fetcher-26c648d53133eec646677ca95acafe8a6165c960.tar`，1146880 bytes |
| 包 SHA-256 | `138958a3fc783b238ed3429832464be4e64a64722951e3a5604261f75a44dbb1`（本地与远端一致） |
| 镜像 | `reports-fetcher:26c648d53133eec646677ca95acafe8a6165c960` |
| 镜像 ID | `sha256:259c3e450ef13eb5f4d35c757d25de1af1761252c3e22bbeadb7ae039d363143` |
| 固定运维入口 | `/home/chen/dev/reports-fetcher/compose-release.sh`，已固定 v1.0.3 SHA |
| v1.0.2 回滚入口 | `/home/chen/dev/reports-fetcher/compose-release-7f5fbadadc63d1b026019e72c0a2dc9f5edfb40e.sh` |
| 生产配置备份 | `/home/chen/dev/reports-fetcher/shared/config.toml.v1.0.2` |
| 端口 / 网络 | 宿主 `127.0.0.1:8000`；Compose 网络内仍为 `http://serve:8000` |

目标机原生构建约 28 秒。稳定入口切换后使用以下命令启动与复验：

```sh
sh /home/chen/dev/reports-fetcher/compose-release.sh config --quiet
sh /home/chen/dev/reports-fetcher/compose-release.sh up -d --no-deps serve
curl --fail --silent --show-error http://127.0.0.1:8000/health/ready
sh /home/chen/dev/reports-fetcher/compose-release.sh restart serve
```

### 生产定向验收

在服务容器内通过真实 HTTP 调用生产 API。请求只传 `symbols=["0700.HK"]`、`last_n=4`、`refresh=false`，明确省略 `forms_by_market`，验证生产生效默认值。任务 `job_0ac7d301d72c401d892c` 终态 succeeded，`downloaded=0`、`cached=4`、`failed=0`，证券 warnings 为空。

| 顺序 | 类型 | 报告期 | period_source | report_id | bytes |
|---:|---|---|---|---|---:|
| 1 | INTERIM | 2026-06-30 | document | `2727be0c6689aabad5a3` | 5451089 |
| 2 | QTR-HK | 2026-03-31 | explicit_title | `fcb8cfe60ceca32501c1` | 865653 |
| 3 | ANNUAL | 2025-12-31 | document | `5f543428c4cc35b8f8b3` | 4428270 |
| 4 | QTR-HK | 2025-09-30 | explicit_title | `edecd9471ec07ac3e2e4` | 958746 |

同一幂等键重放返回 200 和相同 job_id。中报文件 ETag 为 SHA-256 `6231374bc3d7bd104fcebb7735f9692050b55be32fa0f34d7d7f20478efa71f1`，匹配 `If-None-Match` 返回 304。最终受控重启后再次执行同一验收脚本，任务、四份元数据和文件仍可读，顺序、字节数、ETag 与缓存统计不变。`/openapi.json` 版本为 1.0.3，并已回存 `docs/integration/openapi.production.json`。

### 部署中发现并处理的问题

1. 首次切换后，省略 forms 的 0700.HK 请求仍返回四份年报/中报。原因是生产沿用的 `shared/config.toml` 显式配置 `HK = ["ANNUAL", "INTERIM"]`，覆盖了 v1.0.3 代码默认值。原配置先复制为 `shared/config.toml.v1.0.2`（SHA-256 与修改前一致），再把唯一 HK 行更新为 `HK = ["ANNUAL", "INTERIM", "QTR-HK"]`；`docker compose config --quiet` 通过后重启，重新使用新幂等键验收通过。此问题只影响部署配置，未修改归档或数据库。
2. 第一次镜像自检误用了不存在的 `--version` 参数；项目 CLI 使用 `version` 子命令。该命令在容器切换前失败，没有影响运行服务；改用 `python -m reports_fetcher version` 后返回 1.0.3。
3. 初版验收脚本从报告详情顶层读取 `bytes`，而详情契约将其放在当前 `artifacts[]` 中。业务断言（四份顺序、任务状态和缓存统计）此前已经通过；脚本按契约改为读取当前 artifact 后，文件长度、ETag、304、幂等和重启复验全部通过。
4. 服务切换和两次受控重启的启动窗口内出现短暂 connection reset；带上限重试的 ready 探测随后返回 200，日志显示应用正常 startup，最终容器重启计数 0。

生产服务当前为 v1.0.3。旧 release、镜像、固定入口和原生产配置备份均保留；未执行递归删除、移动或权限变更，也未修改其他项目。回滚前应先确认 SQLite schema 兼容，再恢复 v1.0.2 固定入口与配置备份并执行 `up -d --no-deps serve`。

## v1.0.4 → v1.0.5 升级上线记录（2026-09-26）

### 背景与发布门禁

生产排查（2026-09-26，用户授权只读排查+探针）：`1810.HK`（小米）`last_n=10` 只返回季度公告——小米历年定期报告 PDF 正文缺 ToUnicode 映射，文本判期恒 None，未知期完整报告被已知期季度挤出。修复方案 A（用户拍板）：業績公告标题判期回填（任务书 [HK_PERIOD_EVIDENCE_TASK.md](HK_PERIOD_EVIDENCE_TASK.md)；B/OCR 与 C/排序不采用）。

- v1.0.4 提交 `41d3b12f972929ee111250e49ebe57a893f1c5b6`；v1.0.5 提交 `8069fb09f88e42e056937481f46cca6e90453dae`（`v1.0.5` 标签）。
- 本地 Docker 全量测试：两版均 **375 passed，2 warnings**（新增 6 项专项：证据采集/1810 真实标题形态/歧义/生产场景复刻/优先级/证据回填后下载失败可见）。
- integration-kit：**37 tests OK，28.159 秒**。

### v1.0.4 生产验证发现并当日修复

v1.0.4 上线后 1810 验证（`v104-1810-verify-001`）：**9 份 INTERIM（2018–2026）全部回填成功、中报排序第一**，但 3 份 ANNUAL 仍未回填。容器内探针定位：小米年报 title 为「2025**年度報告**」（年度報告变体，非「2025 年報」），年份标签正则不识别 → `year_label=None` → 证据无从匹配；同期证据键 ANNUAL/2018–2025 已确认齐备。补正则后发 v1.0.5。

### 制品与切换（v1.0.5）

| 项目 | 实际值 |
|---|---|
| 完成时间 | 2026-09-26 10:40:34 +08:00 |
| 源码 release | `/home/chen/dev/reports-fetcher/releases/8069fb09f88e42e056937481f46cca6e90453dae` |
| 上传包 | `releases/reports-fetcher-8069fb09f88e42e056937481f46cca6e90453dae.tar`，1177600 bytes |
| 包 SHA-256 | `472b3e9772b7599ddf3ad574c04da2a935a1463baceb28f636d38eadb1fc42c8`（本地与远端一致） |
| 镜像 | `reports-fetcher:8069fb09f88e42e056937481f46cca6e90453dae`（构建约 40s） |
| 镜像自检 | `python -m reports_fetcher version` → 1.0.5；`PeriodSource.ANNOUNCEMENT_TITLE`、pdfminer 可导入 |
| 固定运维入口 | `compose-release.sh` 已固定 v1.0.5 SHA；v1.0.4 入口备份 `compose-release-41d3b12f972929ee111250e49ebe57a893f1c5b6.sh` |
| 端口 / 模式 | 宿主 `127.0.0.1:8000`，回环本地模式，RestartCount=0 |

### 生产定向验收（v1.0.5）

任务 `v105-1810-verify-001`（`job_3795525beab3477992fd`）：`01810`、`last_n=10`、省略 forms。

| 检查 | 结果 |
|---|---|
| 任务终态 | **succeeded**（此前 partial+11 份判期失败警告 → 现零警告）；downloaded=3、cached=7、failed=0 |
| 入选顺序（按报告期穿插） | 中报 2026-06-30 → Q1 2026-03-31 → **年报 2025-12-31** → Q3 2025-09-30 → 中报 2025-06-30 → Q1 2025 → 年报 2024-12-31 → Q3 2024 → 中报 2024 → 年报 2023-12-31 |
| 档案报告期 | 17 条 done 全部有期：ANNUAL×3 + INTERIM×4 = `announcement_title`（证据回填）；QTR-HK×10 = `explicit_title` |
| 证据可追溯 | 2025 年报 `source_metadata.period_evidence` → 2026-03-24《截至2025年12月31日止年度之全年業績公告》[末期業績] |
| 日志留痕 | 9 条「業績公告证据回填报告期」INFO（v1.0.4 验证窗口）；判期失败 WARNING 路径已由单测覆盖 |
| `/openapi.json` | 版本 1.0.5 |

用户原始诉求（8 月发布的半年报）已满足：最新入选第一项即 2026-09-23 发布的《2026年中期報告》（report_period=2026-06-30，其業績公告 2026-08-18 为证据）。旧 release、镜像、入口备份均保留；未修改归档外的其他项目。

## 归档迁移到存储卷（2026-09-26，非版本发布）

### 目的与路径

用户需求：财报文件落存储盘（非系统盘）并对局域网 SMB 可见。`/vol2/1000/10.Develop` 本就是 fnOS 按用户（chen）导出的 SMB 共享（`users/1000.share.conf`，writeable、hide unreadable），归档迁入即自动可达，无需改动 SMB 配置。

| 项 | 值 |
|---|---|
| 归档新路径 | `/vol2/1000/10.Develop/reports-fetcher/reports`（btrfs，680G 可用） |
| SMB 路径 | `\fnos.Developeports-fetchereports`（用户认证后可见；用户级共享匿名枚举不可见属 fnOS 预期行为） |
| 旧归档 | `/home/chen/dev/reports-fetcher/shared/reports` **原样保留作回滚备份**；`compose.deploy.yml.systemdisk-bak` 为挂载修改前备份 |
| 挂载变更 | `compose.deploy.yml` volumes 首行指向新路径；新增 `entrypoint` 包装 `umask 022`（新文件 755/644，root 写、SMB 用户只读） |

### 迁移步骤（已执行）

停服（释放 flock/SQLite 干净关闭）→ `cp -a` → 校验（源/目标文件数一致 38、`PRAGMA integrity_check=ok`、33 artifacts）→ 改挂载 → `up -d` → 验证：health ok、reports 读取正常、同幂等键重放 200、按 ID 下载 ETag=sha256 匹配；refresh 任务新 artifact 权限 755 且旧版本保留（多版本并存验证）。存量文件以 `chmod -R a+rX` 放开只读。

### SMB 使用注意（重要）

- **请勿通过 SMB 写入或删除归档内任何文件**，尤其 `archive.sqlite3*`（SQLite 对 SMB 并发写敏感，可能损坏索引）；局域网机器请以只读方式取用 PDF/HTML。
- 程序化取文件建议走 HTTP（`/api/v1/reports/{id}/file`，含校验与 304）；SMB 适合人肉浏览/拷贝。
- Windows 首次访问：资源管理器打开 `\fnos.Develop`（或 `net use \fnos.Develop /user:chen`），输入 NAS 密码后勾选记住凭据。
- 回滚：`compose-release.sh stop serve` → 恢复 `compose.deploy.yml.systemdisk-bak` → `up -d --no-deps serve`（旧归档未动）。
