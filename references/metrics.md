# 指标与解读

## 延迟

- `first_response_ms`：流式请求为首个非空 `delta.content` 到达时间；非流式请求为响应正文第一个字节到达时间。
- `total_ms`：从请求开始到响应正文完整读取、解析完成的时间。
- p50 表示典型请求，p95/p99 更适合观察尾延迟。样本很小时，p99 只接近最慢样本，不应作为稳定 SLA。

## 吞吐和用量

- `achieved_requests_per_minute` 按首个计量请求开始到最后一个结束计算，因此短测试会受启动和收尾影响。
- `completion_tokens_per_second` 使用成功响应中供应商返回的 completion token 总数除以测试墙钟时间。
- `usage_coverage` 是包含 usage 的成功响应比例。低于 100% 时，总 Token 与成本仅覆盖已知部分。
- 流式请求发送 `stream_options.include_usage=true`。兼容端点不支持该字段时，应明确记录协议失败，或在用户同意后改用非流式基线。

## 错误类别

- `auth`：HTTP 401/403。
- `rate_limit`：HTTP 429。
- `server`：HTTP 5xx。
- `timeout`：连接或读取超时。
- `network`：DNS、连接拒绝等传输问题。
- `protocol`：成功状态但 JSON/SSE 结构不符合预期。
- `http_other`：其他 HTTP 状态。

脚本不自动重试，所有错误都代表一次真实尝试。对比两个供应商时使用相同提示词、最大输出、请求数、并发、RPM、网络环境和时间窗口。

## 协议兼容

默认发送 `max_tokens`，部分新模型或端点要求 `max_completion_tokens`，可用 `--token-field max_completion_tokens` 显式切换。不要同时发送两个字段。Chat Completions 与流式结构依据 [OpenAI 官方 API Reference](https://developers.openai.com/api/reference/cli/resources/chat/subresources/completions)；限流是模型和账户等级相关的外部约束，不应把某个模型的 RPM/TPM 写死在 Skill 中。
