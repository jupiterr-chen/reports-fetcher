# 部署记录与步骤

## 当前状态

2026-09-21：**已完成目标机只读预检查，尚未部署。** 审查版本为 `709bb72b798efbec115fa886b1aa51bdb7d06231`（v1.0.0）。

用户授权的部署条件是“验证通过后部署”。本次发现归档锁、常驻缓存、并发提交及数据质量等阻断问题，故没有上传文件、构建远端镜像、启动容器或修改现有服务。问题、验收条件及 agent 提示词见 [一期审查任务书](docs/PHASE1_REVIEW.md)。

以下明确区分已执行的检查与**修复后待执行**的部署步骤。不能将本文当作已经上线的证明。

## 已执行：环境预检查

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
| 目标机出网 | 尚未验证 SEC / 巨潮 / 港交所，也未验证 pip 镜像下载；正式部署阶段验证 |

执行过的检查包括：

```powershell
ssh -o BatchMode=yes -o ConnectTimeout=10 chen@192.168.1.150 'uname -a; id; pwd; ls -ld /home/chen/dev; df -h /home/chen/dev; command -v docker; docker version --format "{{.Server.Version}}"; docker compose version; ss -ltn'
ssh -o BatchMode=yes -o ConnectTimeout=10 chen@192.168.1.150 'ls -la /home/chen/dev; docker ps --format "{{.Names}} {{.Image}} {{.Ports}}"; docker image ls --format "{{.Repository}}:{{.Tag}} {{.Size}}"; free -h; command -v git; command -v curl'
```

目录权限较宽，实际应用目录和配置按单独目录管理；不批量改变父目录或其他项目权限。

## 已执行：部署前验证

开发机 Docker Server 29.8.0，Compose v5.5.1；所有 Python 执行在 Docker 内。

```powershell
docker compose run --rm --no-deps -T test python -m pytest tests/test_core_pipeline.py tests/test_store.py tests/test_jobs.py tests/test_api.py -q
```

结果：85 passed，17.82 秒；2 条依赖弃用警告。另做 2 请求规模的定向边界复现，证实任务书 T1/T2/T3/T5/T6/T7 的行为；T4 为代码与设计契约比对发现。

真实抓取抽查：本机 Docker 隔离临时归档中，600519 / 0700.HK / AAPL 各 1 份，全部下载、校验、归档成功，耗时 3.73 / 10.35 / 4.33 秒。只降低该次探测的超时/重试预算，未改产品配置；SEC 使用真实联系邮箱，未留存到文档。没有覆盖现有报告文件。

上述结果证明正常抓取可用，**不足以覆盖常驻、并发和报告语义边界**；部署门禁未通过。

## 发现的问题与处理状态

| 问题 | 处理/部署影响 |
|---|---|
| T1：加锁前执行恢复，能改写其他实例的在途意图 | P1，待修复，阻断部署 |
| T2：SEC submissions 永久缓存 | P1，待修复，常驻服务无法发现新报告 |
| T3：并发幂等冲突、队列容量超限 | P1，待修复，HTTP 协议边界不成立 |
| T4：仅凭 `/A` 把修订标记为全文 | P1，待修复，可能漏取真正完整报告 |
| T5：HK 仅凭年份猜造期末 | P1，待修复，违反未知即 null 的要求 |
| T6：无可用文件仍 partial、正常截取也 partial | P1，待修复，调用方不能可靠判断结果 |
| T7：docs/OpenAPI/ReDoc 未受 token 保护 | P2，非回环开放前修复；首次部署保持回环绑定 |
| 配置 bind mount | Compose 固定挂载 `config.toml`；首次运行必须先创建普通文件，不能照“仅设环境变量即可”的注释省略，否则短语法挂载可能生成同名目录 |
| 访问范围 | 默认仅宿主 `127.0.0.1:8000`，其他机器不能直接访问 `192.168.1.150:8000`；先通过 SSH 隧道调用，局域网直接接入需配置令牌与 HTTPS 入口 |
| 验收文档 | 既有“一期已完成”不能替代本次审查；修复后按新 SHA 更新验收结果 |

## 待执行：修复后的部署步骤

仅在 T1–T6 修复并完成任务书中的定向验收后执行。不要对当前未通过版本直接运行以下部署流程。

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

## 最终上线记录（待填）

- 修复通过的提交：未产生。
- 实际部署时间 / 镜像 ID：未部署。
- 目标机三市场 HTTP 冒烟 / 重启读取：未执行。
- 最终入口与认证模式：待部署确认；计划回环 8000 + SSH 隧道。
- 现阶段服务器改动：无。
