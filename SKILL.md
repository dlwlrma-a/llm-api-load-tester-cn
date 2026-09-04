---
name: llm-api-load-tester-cn
description: 对 OpenAI 兼容模型接口执行有明确上限的并发与限流基准测试，测量成功率、首字节或首 Token 延迟、完整延迟、Token 吞吐和错误分布，并生成 JSON/HTML 报告。适用于上线前容量验证、供应商 SLA 对比和 429 排查；普通接口故障诊断或未经授权的第三方压测不触发。
slug: llm-api-load-tester-cn
displayName: 模型 API 并发压测器
version: 1.0.1
summary: 测试 OpenAI 兼容接口的并发、限流、延迟与 Token 吞吐
license: MIT
---

# 模型 API 并发压测器

只测试用户拥有或明确获准测试的端点。先用小规模预检确认协议，再逐级增加负载；不要从高并发开始。

## 授权边界

- 检查参数、生成计划和读取已有报告无需确认。
- 模型发现和基准测试都会访问目标服务；基准测试还会产生多次模型调用和费用。执行前展示域名、模型、请求数、并发、RPM、最大输出 Token 和总调用上限，并取得明确确认。
- 不测试第三方未授权服务，不绕过限流、WAF、配额或访问控制。
- API Key 只从环境变量读取，不出现在命令、报告、日志或仓库中。

## 工作流

1. 确认端点所有权或测试授权、维护窗口、预算、供应商 RPM/TPM 上限和停止条件。
2. 先生成不联网的计划：

   ```powershell
   python scripts/llm_load_test.py --base-url https://example.com/v1 --model exact-model-id --requests 20 --concurrency 2 --rpm 30 --dry-run
   ```

3. 需要确认模型 ID 时，先经用户确认再只读发现：

   ```powershell
   python scripts/llm_load_test.py --base-url https://example.com/v1 --api-key-env LLM_API_KEY --list-models --confirm-live-run
   ```

4. 用 5-20 个请求建立低负载基线。确认后运行：

   ```powershell
   python scripts/llm_load_test.py --base-url https://example.com/v1 --model exact-model-id --api-key-env LLM_API_KEY --requests 20 --concurrency 2 --rpm 30 --max-tokens 32 --output report.json --html report.html --confirm-live-run
   ```

5. 流式端点使用 `--stream`，此时 `first_response_ms` 表示首个非空文本 Token 到达时间；非流式表示首响应字节到达时间。
   端点要求 `max_completion_tokens` 时加入 `--token-field max_completion_tokens`。
6. 逐级增加并发，每级只改变一个变量。达到用户约定的 429、错误率、延迟或预算阈值立即停止。
7. 根据 [references/metrics.md](references/metrics.md) 解读报告；不要把本机单点结果宣称为供应商全局 SLA。

算点边界是透明标注的可选预设，使用时读取 [references/qixuai-preset.md](references/qixuai-preset.md)。

## 固定安全约束

- 脚本硬上限：500 个计量请求、10 个预热请求、50 并发、600 RPM、2048 输出 Token。
- 压测请求不自动重试。重试会改变真实请求数并掩盖限流行为。
- 只允许 HTTPS；`localhost` 和 `127.0.0.1` 可使用 HTTP 做离线测试。
- 不跟随 HTTP 重定向，不关闭 TLS，不保存模型输出正文。
- 只发送默认无害提示词或用户明确选择的 `--prompt-file` 内容。报告仅保存提示词 SHA-256 和字符数。
- `usage` 缺失时 Token 指标保持未知；不以字符数冒充 Token 数。

## 输出格式

```text
范围: 目标域名、模型、请求数、并发、RPM、最大输出 Token
结果: 成功率、吞吐、首响应 p50/p95/p99、完整延迟 p50/p95/p99
用量: prompt/completion/total Token、Token/s、可选成本估算
错误: 401/403、429、5xx、超时、网络、协议错误
结论: 当前负载是否达标、首次越界点、未验证项和下一档测试建议
```
