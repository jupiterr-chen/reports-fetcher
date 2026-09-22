# 独立验收记录

2026-09-22，结果：**ACCEPTED**。

实现执行器：OpenCode，模型 `opencode-go/deepseek-v4.1-flash`，EconomyAuto；Codex 独立审查与验收。生产服务及主项目业务代码未修改。此前发现的总超时、HK 默认类型、重复代码、报告 ID 重复及 coverage 字段偏差已补修。

## 实际执行结果

在仓库根目录运行：

```powershell
docker compose -f integration-kit/compose.yaml up -d mock
docker compose -f integration-kit/compose.yaml run --rm test
docker compose -f integration-kit/compose.yaml run --rm client
```

- 启动成功，服务保留运行，监听 `127.0.0.1:18765`。
- **33 个真实 HTTP 契约测试通过，23.720 秒**，含慢 HTTP 请求的总等待时限、状态流转、幂等、请求边界、文件下载、可读文件名、进度初值及 SHA-256。
- 示例客户端退出码 0，取得 4 个 MOCK HTML 文件，4/4 校验通过；产物在 `examples/out/`，已由套件自身 `.gitignore` 排除。
- Windows 宿主直接访问 `/health/ready`、`/docs`、`/openapi.json` 均返回 200。
- 额外可移植性检查：仅只读挂载此目录到一个 `python:3.12-slim` 容器，复制到容器内临时目录，在 `--network none` 下启动服务；离线文档、OpenAPI、提交、轮询、下载及校验均通过。未挂载主项目，未访问任何生产/来源接口。

故意超时的两个测试可能打印测试 HTTP 服务器的 `BrokenPipeError`：这是客户端按时断开后，延迟响应的测试服务器继续写入导致的日志；测试断言通过，不是 mock 主服务启动失败。以测试最终 `Ran 33 tests ... OK` 与退出码 0 为准。

## 交接

当前可访问的本地入口：<http://127.0.0.1:18765/docs>。停机或重启 Docker 后，重新执行启动命令即可。

交给开发 agent 时复制整个目录，并让其先阅读 [AGENT_INSTRUCTIONS.md](AGENT_INSTRUCTIONS.md)。接口说明见 [API.md](API.md)，机器定义见 [openapi.json](openapi.json)，测试案例见 [cases.json](cases.json) 和 `tests/test_contract.py`。

数据与文件是明确标注的合成 MOCK，任务状态仅存内存；mock 能力边界及生产切换见 [README.md](README.md)。基础镜像首次准备可能需要网络，服务及测试运行本身不需要外网。不把本次 mock 验收视为新的生产验证。
