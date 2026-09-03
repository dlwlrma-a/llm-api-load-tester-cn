#!/usr/bin/env python3
"""Bounded load tester for OpenAI-compatible chat completion endpoints."""
import argparse
import concurrent.futures
import datetime
import hashlib
import html
import json
import math
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

MAX_REQUESTS = 500
MAX_WARMUP = 10
MAX_CONCURRENCY = 50
MAX_RPM = 600
MAX_OUTPUT_TOKENS = 2048
MAX_PROMPT_CHARS = 100000
RATE_HEADERS = (
    "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-reset-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-tokens",
    "retry-after",
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


class StartRateLimiter:
    """Space request starts globally while allowing responses to overlap."""

    def __init__(self, rpm):
        self.interval = 60.0 / rpm
        self.next_start = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.perf_counter()
            slot = max(now, self.next_start)
            self.next_start = slot + self.interval
        delay = slot - now
        if delay > 0:
            time.sleep(delay)


def validate_base_url(value):
    parsed = urllib.parse.urlparse(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("base URL must not contain credentials, query parameters, or fragments")
    if parsed.scheme == "https" and parsed.netloc:
        return value.rstrip("/")
    if parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost"):
        return value.rstrip("/")
    raise ValueError("base URL must use HTTPS, except localhost/127.0.0.1")


def build_opener():
    return urllib.request.build_opener(
        NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )


def rate_headers(headers):
    return {name: headers.get(name) for name in RATE_HEADERS if headers.get(name) is not None}


def classify_http(code):
    if code in (401, 403):
        return "auth"
    if code == 429:
        return "rate_limit"
    if 500 <= code <= 599:
        return "server"
    return "http_other"


def request_payload(model, prompt, max_tokens, token_field, stream):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "stream": stream,
    }
    payload[token_field] = max_tokens
    if stream:
        payload["stream_options"] = {"include_usage": True}
    return payload


def perform_request(index, base_url, api_key, model, prompt, max_tokens, stream, timeout, token_field="max_tokens"):
    started = time.perf_counter()
    result = {
        "index": index,
        "ok": False,
        "status": None,
        "error_category": None,
        "first_response_ms": None,
        "total_ms": None,
        "response_chars": 0,
        "usage": None,
        "rate_limit_headers": {},
    }
    body = json.dumps(request_payload(model, prompt, max_tokens, token_field, stream)).encode("utf-8")
    request = urllib.request.Request(
        base_url + "/chat/completions",
        data=body,
        method="POST",
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
    )
    try:
        with build_opener().open(request, timeout=timeout) as response:
            result["status"] = response.status
            result["rate_limit_headers"] = rate_headers(response.headers)
            if stream:
                chars = 0
                usage = None
                saw_done = False
                for raw_line in response:
                    line = raw_line.decode("utf-8").strip()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        saw_done = True
                        break
                    event = json.loads(data)
                    if event.get("usage"):
                        usage = event["usage"]
                    choices = event.get("choices") or []
                    if choices:
                        content = (choices[0].get("delta") or {}).get("content")
                        if content:
                            if result["first_response_ms"] is None:
                                result["first_response_ms"] = round((time.perf_counter() - started) * 1000, 3)
                            chars += len(content) if isinstance(content, str) else len(json.dumps(content, ensure_ascii=False))
                if not saw_done:
                    raise ValueError("stream ended without [DONE]")
                result["response_chars"] = chars
                result["usage"] = normalize_usage(usage)
            else:
                first = response.read(1)
                if not first:
                    raise ValueError("empty response body")
                result["first_response_ms"] = round((time.perf_counter() - started) * 1000, 3)
                payload = json.loads(first + response.read())
                choices = payload.get("choices") or []
                if not choices or not isinstance(choices[0].get("message"), dict):
                    raise ValueError("missing choices[0].message")
                content = choices[0]["message"].get("content")
                result["response_chars"] = len(content) if isinstance(content, str) else len(json.dumps(content, ensure_ascii=False))
                result["usage"] = normalize_usage(payload.get("usage"))
        result["ok"] = True
    except urllib.error.HTTPError as exc:
        result["status"] = exc.code
        result["error_category"] = classify_http(exc.code)
        result["rate_limit_headers"] = rate_headers(exc.headers)
        result["error"] = f"HTTP {exc.code}"
    except (socket.timeout, TimeoutError):
        result["error_category"] = "timeout"
        result["error"] = "request timed out"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, socket.timeout):
            result["error_category"] = "timeout"
            result["error"] = "request timed out"
        else:
            result["error_category"] = "network"
            result["error"] = type(exc.reason).__name__
    except (ValueError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        result["error_category"] = "protocol"
        result["error"] = f"{type(exc).__name__}: {exc}"[:200]
    except Exception as exc:
        result["error_category"] = "network"
        result["error"] = type(exc).__name__
    result["total_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return result


def normalize_usage(usage):
    if not isinstance(usage, dict):
        return None
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    if not all(isinstance(usage.get(key), int) and usage[key] >= 0 for key in keys):
        return None
    return {key: usage[key] for key in keys}


def percentile(values, percent):
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percent / 100.0 * len(ordered)) - 1)
    return round(ordered[index], 3)


def latency_summary(values):
    return {
        "min": round(min(values), 3) if values else None,
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "max": round(max(values), 3) if values else None,
    }


def summarize(results, wall_seconds, prices=None):
    successes = [item for item in results if item["ok"]]
    first = [item["first_response_ms"] for item in successes if item["first_response_ms"] is not None]
    total = [item["total_ms"] for item in successes]
    with_usage = [item for item in successes if item["usage"] is not None]
    usage = {
        key: sum(item["usage"][key] for item in with_usage)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    summary = {
        "attempted": len(results),
        "succeeded": len(successes),
        "failed": len(results) - len(successes),
        "success_rate": round(len(successes) / len(results), 6) if results else 0,
        "wall_seconds": round(wall_seconds, 6),
        "achieved_requests_per_minute": round(len(results) / wall_seconds * 60, 3) if wall_seconds else None,
        "first_response_ms": latency_summary(first),
        "total_ms": latency_summary(total),
        "errors": dict(sorted(Counter(item["error_category"] for item in results if not item["ok"]).items())),
        "usage_coverage": round(len(with_usage) / len(successes), 6) if successes else 0,
        "known_usage": usage,
        "completion_tokens_per_second": round(usage["completion_tokens"] / wall_seconds, 3) if wall_seconds else None,
    }
    if prices:
        input_cost = usage["prompt_tokens"] * prices[0] / 1_000_000
        output_cost = usage["completion_tokens"] * prices[1] / 1_000_000
        summary["known_cost_estimate"] = round(input_cost + output_cost, 8)
        summary["cost_estimate_scope"] = "responses_with_usage_only"
    return summary


def discover_models(base_url, api_key, timeout):
    request = urllib.request.Request(base_url + "/models", headers={"Authorization": "Bearer " + api_key})
    try:
        with build_opener().open(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"model discovery failed with HTTP {exc.code}") from exc
    models = sorted(item["id"] for item in payload.get("data", []) if isinstance(item, dict) and isinstance(item.get("id"), str))
    if not models:
        raise ValueError("model discovery returned no model IDs")
    return models


def report_html(report):
    summary = report.get("summary", {})
    first = summary.get("first_response_ms", {})
    total = summary.get("total_ms", {})
    errors = ", ".join(f"{key}: {value}" for key, value in summary.get("errors", {}).items()) or "none"
    embedded = json.dumps(report, ensure_ascii=False).replace("<", "\\u003c")
    rows = [
        ("Requests", summary.get("attempted")),
        ("Success rate", f"{summary.get('success_rate', 0) * 100:.2f}%"),
        ("Achieved RPM", summary.get("achieved_requests_per_minute")),
        ("First response p50 / p95", f"{first.get('p50')} / {first.get('p95')} ms"),
        ("Total p50 / p95", f"{total.get('p50')} / {total.get('p95')} ms"),
        ("Usage coverage", f"{summary.get('usage_coverage', 0) * 100:.2f}%"),
        ("Completion tokens/s", summary.get("completion_tokens_per_second")),
        ("Errors", errors),
    ]
    table = "".join(f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>" for key, value in rows)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LLM API Load Test</title><style>
body{{font-family:Arial,sans-serif;max-width:900px;margin:40px auto;padding:0 20px;color:#17202a;background:#f7f8fa}}
h1{{font-size:28px;letter-spacing:0}}.meta{{color:#59636e}}table{{width:100%;border-collapse:collapse;background:white}}
th,td{{padding:12px 14px;border:1px solid #d9dee5;text-align:left}}th{{width:42%;background:#f0f4f8}}
code{{background:#fff3e8;padding:2px 5px}}.note{{margin-top:20px;padding:12px;border-left:4px solid #c2410c;background:white}}
</style></head><body><h1>LLM API Load Test</h1>
<p class="meta"><code>{html.escape(report['config']['base_url'])}</code> · {html.escape(report['config'].get('model') or '')}</p>
<table>{table}</table><p class="note">No model response content or API key is stored in this report. Token totals cover only responses that returned usage.</p>
<script type="application/json" id="report-data">{embedded}</script></body></html>"""


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model")
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    prompt_group = parser.add_mutually_exclusive_group()
    prompt_group.add_argument("--prompt", default="Reply with exactly: OK")
    prompt_group.add_argument("--prompt-file")
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--rpm", type=int, default=60)
    parser.add_argument("--max-tokens", type=int, default=32)
    parser.add_argument("--token-field", choices=("max_tokens", "max_completion_tokens"), default="max_tokens")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--list-models", action="store_true")
    parser.add_argument("--input-price-per-million", type=float)
    parser.add_argument("--output-price-per-million", type=float)
    parser.add_argument("--output", default="report.json")
    parser.add_argument("--html")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-live-run", action="store_true")
    return parser.parse_args(argv)


def validate_args(args):
    args.base_url = validate_base_url(args.base_url)
    limits = (
        ("requests", args.requests, 1, MAX_REQUESTS),
        ("warmup", args.warmup, 0, MAX_WARMUP),
        ("concurrency", args.concurrency, 1, MAX_CONCURRENCY),
        ("rpm", args.rpm, 1, MAX_RPM),
        ("max-tokens", args.max_tokens, 1, MAX_OUTPUT_TOKENS),
    )
    for name, value, minimum, maximum in limits:
        if not minimum <= value <= maximum:
            raise ValueError(f"--{name} must be between {minimum} and {maximum}")
    if not 1 <= args.timeout <= 300:
        raise ValueError("--timeout must be between 1 and 300 seconds")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", args.api_key_env):
        raise ValueError("--api-key-env must be an uppercase environment variable name")
    prices = (args.input_price_per_million, args.output_price_per_million)
    if (prices[0] is None) != (prices[1] is None):
        raise ValueError("provide both input and output prices, or neither")
    if prices[0] is not None and (prices[0] < 0 or prices[1] < 0):
        raise ValueError("prices cannot be negative")
    if not args.list_models and not args.model:
        raise ValueError("--model is required unless --list-models is used")


def main(argv=None):
    args = parse_args(argv)
    try:
        validate_args(args)
    except ValueError as exc:
        raise SystemExit(str(exc))
    prompt = Path(args.prompt_file).read_text(encoding="utf-8-sig") if args.prompt_file else args.prompt
    if not prompt or len(prompt) > MAX_PROMPT_CHARS:
        raise SystemExit(f"prompt must contain 1-{MAX_PROMPT_CHARS} characters")
    prompt_meta = {"characters": len(prompt), "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}
    plan = {
        "base_url": args.base_url,
        "model": args.model,
        "requests": args.requests,
        "warmup": args.warmup,
        "total_calls": args.requests + args.warmup,
        "concurrency": args.concurrency,
        "rpm": args.rpm,
        "max_tokens": args.max_tokens,
        "token_field": args.token_field,
        "stream": args.stream,
        "prompt": prompt_meta,
    }
    if args.dry_run:
        print(json.dumps({"mode": "dry-run", "plan": plan}, ensure_ascii=False, indent=2))
        return 0
    if not args.confirm_live_run:
        raise SystemExit("live network requests require --confirm-live-run")
    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        raise SystemExit("missing API key environment variable: " + args.api_key_env)
    if args.list_models:
        print(json.dumps({"base_url": args.base_url, "models": discover_models(args.base_url, api_key, args.timeout)}, ensure_ascii=False, indent=2))
        return 0

    warmup_results = [
        perform_request(-(index + 1), args.base_url, api_key, args.model, prompt, args.max_tokens, args.stream, args.timeout, args.token_field)
        for index in range(args.warmup)
    ]
    if warmup_results and not any(item["ok"] for item in warmup_results):
        report = {
            "schema_version": 1,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "config": plan,
            "aborted": "all warmup requests failed",
            "warmup": warmup_results,
            "requests": [],
        }
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"aborted": report["aborted"], "output": args.output}, ensure_ascii=False))
        return 2

    limiter = StartRateLimiter(args.rpm)

    def run_one(index):
        limiter.wait()
        return perform_request(index, args.base_url, api_key, args.model, prompt, args.max_tokens, args.stream, args.timeout, args.token_field)

    wall_start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(run_one, range(1, args.requests + 1)))
    wall_seconds = time.perf_counter() - wall_start
    prices = None
    if args.input_price_per_million is not None:
        prices = (args.input_price_per_million, args.output_price_per_million)
    report = {
        "schema_version": 1,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "config": plan,
        "summary": summarize(results, wall_seconds, prices),
        "warmup": warmup_results,
        "requests": results,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.html:
        html_path = Path(args.html)
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(report_html(report), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0 if report["summary"]["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
