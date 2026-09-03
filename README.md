# llm-api-load-tester-cn

面向 Codex/SkillHub 的 OpenAI 兼容模型 API 有界压测技能。测量并发、限流、首字节或首 Token 延迟、完整延迟、Token 吞吐和错误分布，输出 JSON 与自包含 HTML 报告。

先生成计划，不联网：

```powershell
python scripts/llm_load_test.py --base-url https://example.com/v1 --model exact-model-id --requests 20 --concurrency 2 --rpm 30 --dry-run
```

实时测试必须使用环境变量密钥和 `--confirm-live-run`。工具不自动重试、不保存模型正文，并设有请求数、并发、RPM 和输出 Token 硬上限。

```powershell
python -m unittest discover -s scripts -p "test_*.py" -v
```

MIT License
