"""
AI Provider manager for MC Quest Generator.

Multi-provider auto-fallback chain. Each provider is OpenAI-compatible
where possible. All HTTP calls use stdlib `urllib` — no extra deps.

Supported providers (all optional, configured via providers_config.json
or `--setup`):

    openrouter      OpenAI-compatible, free-tier models available
    chutes          OpenAI-compatible (llm.chutes.ai)
    deepseek        OpenAI-compatible direct API
    electronhub     OpenAI-compatible
    huggingface     OpenAI-compatible router
    groq            OpenAI-compatible direct API (api.groq.com)
    google_gemini   Gemini-specific REST format
    g4f_groq        g4f.space proxy (often behind Cloudflare; opt-in)
    cloudflare      Cloudflare Workers AI

Cloudflare is intentionally placed at the **end** of the default chain
because some users (rightly) don't want their prompts going through it.

The chain skips any provider with no key, skips providers that hit a
recent 429, and falls back to the next provider on any failure. The
test mode (`mc_quest_gen.py --test`) prints HTTP status codes and the
first part of the error body so it's clear *why* something failed
(instead of the old "empty response" message that hid all useful info).
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

CONFIG_PATH = Path(__file__).parent / "providers_config.json"


# ─────────────────────────────────────────────
# Default config skeleton (no keys)
# ─────────────────────────────────────────────
DEFAULT_CONFIG: dict = {
    # Order matters. First provider with a key gets the first call.
    # Cloudflare last on purpose — opt-in for users who don't want CF.
    "chain": [
        "openrouter",
        "chutes",
        "deepseek",
        "electronhub",
        "groq",
        "huggingface",
        "google_gemini",
        "g4f_groq",
        "cloudflare",
    ],
    "openrouter": {
        "api_key": "",
        "model": "openai/gpt-oss-120b:free",
        # Confirmed-existing free models on OpenRouter (May 2026).
        # Old IDs like deepseek-chat-v3-0324:free / qwen-2.5-72b:free are
        # gone — they 404. Update via `--setup` or edit config.
        "fallback_models": [
            "openai/gpt-oss-120b:free",
            "openai/gpt-oss-20b:free",
            "google/gemma-4-31b-it:free",
            "nvidia/nemotron-nano-12b-v2-vl:free",
            "z-ai/glm-4.5-air:free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "qwen/qwen3-next-80b-a3b-instruct:free",
            "nvidia/nemotron-nano-9b-v2:free",
            "deepseek/deepseek-v4-flash:free",
            "openrouter/owl-alpha",
        ],
    },
    "chutes": {
        "api_key": "",
        "model": "deepseek-ai/DeepSeek-V3.2-TEE",
        "base_url": "https://llm.chutes.ai/v1",
    },
    "deepseek": {
        "api_key": "",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
    },
    "electronhub": {
        "api_key": "",
        "model": "deepseek-chat",
        "base_url": "https://api.electronhub.ai/v1",
    },
    "groq": {
        "api_key": "",
        "model": "llama-3.3-70b-versatile",
        "base_url": "https://api.groq.com/openai/v1",
    },
    "huggingface": {
        "api_key": "",
        "model": "Qwen/Qwen2.5-72B-Instruct",
        "base_url": "https://router.huggingface.co/v1",
    },
    "google_gemini": {
        "api_key": "",
        "model": "gemini-2.0-flash",
    },
    "g4f_groq": {
        "api_key": "",
        "model": "meta-llama/llama-4-scout-17b-16e-instruct",
        "base_url": "https://g4f.space/api/groq",
    },
    "cloudflare": {
        "api_token": "",
        "account_id": "",
        "model": "@cf/qwen/qwen3-30b-a3b-fp8",
    },
}


# ─────────────────────────────────────────────
# Config IO
# ─────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            merged = json.loads(json.dumps(DEFAULT_CONFIG))
            _deep_merge(merged, data)
            return merged
        except Exception as e:
            print(f"⚠ Failed to read {CONFIG_PATH}: {e}. Using defaults.")
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# ─────────────────────────────────────────────
# Per-(provider, model) cooldown for 429s
# ─────────────────────────────────────────────
_cooldowns: dict[str, float] = {}


def _cool_until(key: str) -> float:
    return _cooldowns.get(key, 0.0)


def _is_cooling(key: str) -> bool:
    return _cool_until(key) > time.time()


def _set_cool(key: str, seconds: int) -> None:
    _cooldowns[key] = time.time() + max(1, seconds)


# ─────────────────────────────────────────────
# HTTP helper
# ─────────────────────────────────────────────

@dataclass
class HttpResult:
    ok: bool
    status: int
    data: Optional[dict]
    text: str
    retry_after: int = 0

    @property
    def short_error(self) -> str:
        snippet = self.text.replace("\n", " ").strip()[:200]
        return f"HTTP {self.status}: {snippet}" if snippet else f"HTTP {self.status}"


def _http_post(
    url: str,
    headers: dict,
    body: dict,
    timeout: int = 60,
) -> HttpResult:
    headers = {"Content-Type": "application/json", **headers}
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return HttpResult(ok=True, status=resp.status, data=json.loads(raw), text=raw)
            except json.JSONDecodeError:
                return HttpResult(ok=False, status=resp.status, data=None, text=raw)
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", errors="replace")
        except Exception:
            text = ""
        ra = 0
        try:
            ra = int(e.headers.get("Retry-After") or 0)
        except Exception:
            ra = 0
        if not ra and text:
            m = re.search(r'retry_after_seconds["\s:]+(\d+)', text)
            if m:
                ra = int(m.group(1))
        return HttpResult(ok=False, status=e.code, data=None, text=text, retry_after=ra)
    except urllib.error.URLError as e:
        return HttpResult(ok=False, status=0, data=None, text=f"URLError: {e.reason}")
    except TimeoutError:
        return HttpResult(ok=False, status=0, data=None, text="Timeout")
    except Exception as e:
        return HttpResult(ok=False, status=0, data=None, text=f"{type(e).__name__}: {e}")


# ─────────────────────────────────────────────
# Response parsing helpers
# ─────────────────────────────────────────────

def _extract_text(data: Optional[dict]) -> Optional[str]:
    """Extract text from various provider response shapes."""
    if not isinstance(data, dict):
        return None

    # OpenAI-style
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] or {}
        msg = first.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        # Some reasoning models put output in `reasoning_content` only
        reasoning = msg.get("reasoning") or msg.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip():
            return reasoning.strip()
        # Some completion-style providers
        text = first.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
        # delta (rare in non-stream)
        delta = first.get("delta") or {}
        if isinstance(delta.get("content"), str) and delta["content"].strip():
            return delta["content"].strip()

    # Cloudflare Workers AI
    result = data.get("result")
    if isinstance(result, dict):
        for key in ("response", "output_text"):
            v = result.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        cf_choices = result.get("choices")
        if isinstance(cf_choices, list) and cf_choices:
            msg = (cf_choices[0] or {}).get("message") or {}
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()

    # Google Gemini
    candidates = data.get("candidates")
    if isinstance(candidates, list) and candidates:
        first = candidates[0] or {}
        parts = ((first.get("content") or {}).get("parts")) or []
        bits = [p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p]
        text = " ".join(b for b in bits if b).strip()
        if text:
            return text
        # Sometimes finish_reason="SAFETY" with no parts — return None to fall through

    return None


_THINK_RE_PAIRED = re.compile(r"<think\b[^>]*>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_RE_OPEN = re.compile(r"<think\b[^>]*>.*$", re.DOTALL | re.IGNORECASE)
_THINK_RE_BARE = re.compile(r"</?think>", re.IGNORECASE)
_THINK_RE_BRACKETS = re.compile(r"\[THINK\].*?\[/THINK\]", re.DOTALL | re.IGNORECASE)


def _strip_thinking(text: str) -> str:
    """Remove <think>...</think> reasoning blocks (Qwen3, GLM, DeepSeek-R1 etc)."""
    text = _THINK_RE_PAIRED.sub("", text)
    text = _THINK_RE_OPEN.sub("", text)
    text = _THINK_RE_BARE.sub("", text)
    text = _THINK_RE_BRACKETS.sub("", text)
    return text.strip()


# ─────────────────────────────────────────────
# Provider call result
# ─────────────────────────────────────────────

@dataclass
class CallResult:
    """Result of an attempt to call a provider."""
    text: Optional[str]
    status: str  # "ok" | "no_key" | "rate_limited" | "empty" | "http_<code>" | "error"
    detail: str = ""
    model_used: str = ""
    elapsed_ms: int = 0


# ─────────────────────────────────────────────
# OpenAI-compatible providers
# ─────────────────────────────────────────────

def _openai_compatible_call(
    provider_name: str,
    base_url: str,
    api_key: str,
    model: str,
    messages: list,
    *,
    extra_headers: Optional[dict] = None,
    cooldown_on_rate_limit: int = 60,
    timeout: int = 90,
) -> CallResult:
    if not api_key:
        return CallResult(None, "no_key", "API key not set")

    ck = f"{provider_name}::{model}"
    if _is_cooling(ck):
        left = int(_cool_until(ck) - time.time())
        return CallResult(None, "rate_limited", f"cooling down ({left}s left)", model)

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}
    if extra_headers:
        headers.update(extra_headers)

    body = {
        "model": model,
        "messages": messages,
        "max_tokens": 4096,
        "temperature": 0.7,
        "stream": False,
    }
    t0 = time.time()
    r = _http_post(url, headers, body, timeout=timeout)
    elapsed = int((time.time() - t0) * 1000)

    if r.status == 429:
        _set_cool(ck, r.retry_after or cooldown_on_rate_limit)
        return CallResult(None, "rate_limited", r.short_error, model, elapsed)
    if not r.ok:
        return CallResult(None, f"http_{r.status}", r.short_error, model, elapsed)

    text = _extract_text(r.data)
    if not text:
        return CallResult(None, "empty", f"no text in response: {r.text[:200]}", model, elapsed)
    return CallResult(_strip_thinking(text), "ok", "", model, elapsed)


def _call_openrouter(messages: list, cfg: dict) -> CallResult:
    pc = cfg.get("openrouter", {})
    api_key = pc.get("api_key", "")
    if not api_key:
        return CallResult(None, "no_key", "API key not set")
    models = [pc.get("model", "")] + list(pc.get("fallback_models", []))
    models = [m for m in dict.fromkeys(models) if m]  # dedupe, keep order
    if not models:
        return CallResult(None, "no_key", "no model configured")

    last: CallResult = CallResult(None, "error", "no models attempted")
    for model in models:
        r = _openai_compatible_call(
            "openrouter",
            "https://openrouter.ai/api/v1",
            api_key,
            model,
            messages,
            extra_headers={
                "HTTP-Referer": "https://github.com/sirksswenenen/mc-quest-gen",
                "X-Title": "MC-Quest-Gen",
            },
        )
        last = r
        if r.status == "ok":
            return r
        if r.status in ("no_key",):
            return r
        # Keep trying next model on rate_limited / http_4xx / empty
    return last


def _call_chutes(messages: list, cfg: dict) -> CallResult:
    pc = cfg.get("chutes", {})
    return _openai_compatible_call(
        "chutes",
        pc.get("base_url", "https://llm.chutes.ai/v1"),
        pc.get("api_key", ""),
        pc.get("model", "deepseek-ai/DeepSeek-V3.2-TEE"),
        messages,
    )


def _call_deepseek(messages: list, cfg: dict) -> CallResult:
    pc = cfg.get("deepseek", {})
    return _openai_compatible_call(
        "deepseek",
        pc.get("base_url", "https://api.deepseek.com"),
        pc.get("api_key", ""),
        pc.get("model", "deepseek-chat"),
        messages,
    )


def _call_electronhub(messages: list, cfg: dict) -> CallResult:
    pc = cfg.get("electronhub", {})
    return _openai_compatible_call(
        "electronhub",
        pc.get("base_url", "https://api.electronhub.ai/v1"),
        pc.get("api_key", ""),
        pc.get("model", "deepseek-chat"),
        messages,
    )


def _call_groq(messages: list, cfg: dict) -> CallResult:
    pc = cfg.get("groq", {})
    return _openai_compatible_call(
        "groq",
        pc.get("base_url", "https://api.groq.com/openai/v1"),
        pc.get("api_key", ""),
        pc.get("model", "llama-3.3-70b-versatile"),
        messages,
    )


def _call_huggingface(messages: list, cfg: dict) -> CallResult:
    pc = cfg.get("huggingface", {})
    return _openai_compatible_call(
        "huggingface",
        pc.get("base_url", "https://router.huggingface.co/v1"),
        pc.get("api_key", ""),
        pc.get("model", "Qwen/Qwen2.5-72B-Instruct"),
        messages,
    )


def _call_g4f_groq(messages: list, cfg: dict) -> CallResult:
    pc = cfg.get("g4f_groq", {})
    return _openai_compatible_call(
        "g4f_groq",
        pc.get("base_url", "https://g4f.space/api/groq"),
        pc.get("api_key", ""),
        pc.get("model", "meta-llama/llama-4-scout-17b-16e-instruct"),
        messages,
        timeout=45,
    )


# ─────────────────────────────────────────────
# Google Gemini (non-OpenAI format)
# ─────────────────────────────────────────────

def _call_gemini(messages: list, cfg: dict) -> CallResult:
    pc = cfg.get("google_gemini", {})
    key = pc.get("api_key", "")
    model = pc.get("model", "gemini-2.0-flash")
    if not key:
        return CallResult(None, "no_key", "API key not set")
    ck = f"gemini::{model}"
    if _is_cooling(ck):
        left = int(_cool_until(ck) - time.time())
        return CallResult(None, "rate_limited", f"cooling down ({left}s left)", model)

    contents: list = []
    system_text = ""
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            system_text += content + "\n"
            continue
        g_role = "model" if role == "assistant" else "user"
        contents.append({"role": g_role, "parts": [{"text": content}]})

    payload: dict = {
        "contents": contents,
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 4096},
    }
    if system_text:
        payload["systemInstruction"] = {"parts": [{"text": system_text.strip()}]}

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
        f":generateContent?key={urllib.parse.quote(key)}"
    )
    t0 = time.time()
    r = _http_post(url, {}, payload, timeout=60)
    elapsed = int((time.time() - t0) * 1000)
    if r.status == 429:
        _set_cool(ck, r.retry_after or 60)
        return CallResult(None, "rate_limited", r.short_error, model, elapsed)
    if not r.ok:
        return CallResult(None, f"http_{r.status}", r.short_error, model, elapsed)
    text = _extract_text(r.data)
    if not text:
        # finish_reason=SAFETY etc.
        return CallResult(None, "empty", r.text[:200], model, elapsed)
    return CallResult(_strip_thinking(text), "ok", "", model, elapsed)


# ─────────────────────────────────────────────
# Cloudflare Workers AI
# ─────────────────────────────────────────────

def _call_cloudflare(messages: list, cfg: dict) -> CallResult:
    pc = cfg.get("cloudflare", {})
    token = pc.get("api_token", "")
    account = pc.get("account_id", "")
    model = pc.get("model", "@cf/qwen/qwen3-30b-a3b-fp8")
    if not token or not account:
        return CallResult(None, "no_key", "api_token + account_id required")
    ck = f"cloudflare::{model}"
    if _is_cooling(ck):
        left = int(_cool_until(ck) - time.time())
        return CallResult(None, "rate_limited", f"cooling down ({left}s left)", model)

    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"
    body = {"messages": messages, "stream": False, "max_tokens": 4096, "temperature": 0.7}
    t0 = time.time()
    r = _http_post(url, {"Authorization": f"Bearer {token}"}, body, timeout=60)
    elapsed = int((time.time() - t0) * 1000)
    if r.status == 429:
        _set_cool(ck, r.retry_after or 120)
        return CallResult(None, "rate_limited", r.short_error, model, elapsed)
    if not r.ok:
        return CallResult(None, f"http_{r.status}", r.short_error, model, elapsed)
    text = _extract_text(r.data)
    if not text:
        return CallResult(None, "empty", r.text[:200], model, elapsed)
    return CallResult(_strip_thinking(text), "ok", "", model, elapsed)


# ─────────────────────────────────────────────
# Provider registry
# ─────────────────────────────────────────────

ProviderFn = Callable[[list, dict], CallResult]

PROVIDERS: dict[str, ProviderFn] = {
    "openrouter": _call_openrouter,
    "chutes": _call_chutes,
    "deepseek": _call_deepseek,
    "electronhub": _call_electronhub,
    "groq": _call_groq,
    "huggingface": _call_huggingface,
    "google_gemini": _call_gemini,
    "g4f_groq": _call_g4f_groq,
    "cloudflare": _call_cloudflare,
}


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

def ai_call(messages: list, cfg: Optional[dict] = None, verbose: bool = False) -> str:
    """
    Send messages to AI. Walks the configured provider chain until one
    responds with non-empty text. Raises RuntimeError if all fail.
    """
    if cfg is None:
        cfg = load_config()

    chain = cfg.get("chain", list(PROVIDERS.keys()))
    chain = [p for p in chain if p in PROVIDERS]
    if not chain:
        chain = list(PROVIDERS.keys())

    attempts: list[str] = []
    for provider in chain:
        fn = PROVIDERS.get(provider)
        if fn is None:
            continue
        try:
            r = fn(messages, cfg)
        except Exception as e:
            attempts.append(f"{provider}: exception {type(e).__name__}: {e}")
            continue
        if r.status == "ok" and r.text:
            if verbose:
                print(f"    ↳ via {provider} ({r.model_used}, {r.elapsed_ms}ms)")
            return r.text
        attempts.append(f"{provider}: {r.status} {r.detail[:120]}")
        if verbose:
            print(f"    ↳ {provider}: {r.status} {r.detail[:120]}")

    raise RuntimeError(
        "All AI providers failed:\n  - "
        + "\n  - ".join(attempts)
        + "\n\nFix your keys with `python mc_quest_gen.py --setup` or edit providers_config.json."
    )


def test_providers(cfg: Optional[dict] = None) -> dict:
    """Per-provider smoke test. Returns {provider_name: status_string}."""
    if cfg is None:
        cfg = load_config()
    results: dict[str, str] = {}
    test_msg = [
        {"role": "system", "content": "Reply with exactly one word."},
        {"role": "user", "content": "Say 'ok'."},
    ]
    for name, fn in PROVIDERS.items():
        try:
            r = fn(test_msg, cfg)
        except Exception as e:
            results[name] = f"error ({type(e).__name__}: {e})"
            continue
        if r.status == "ok":
            results[name] = f"ok ({r.elapsed_ms}ms, model={r.model_used})"
        elif r.status == "no_key":
            results[name] = "no_key"
        elif r.status == "rate_limited":
            results[name] = f"rate_limited [{r.model_used}]: {r.detail}"
        elif r.status == "empty":
            results[name] = f"empty [{r.model_used}]: {r.detail[:160]}"
        else:
            results[name] = f"{r.status} [{r.model_used}]: {r.detail[:160]}"
    return results


# ─────────────────────────────────────────────
# Interactive setup wizard
# ─────────────────────────────────────────────

def _mask(value: str) -> str:
    if not value:
        return "(not set)"
    if len(value) <= 8:
        return value[:2] + "…"
    return value[:6] + "…" + value[-4:]


def _ask(prompt: str, current: str) -> str:
    v = input(f"  {prompt} [{_mask(current)}]: ").strip()
    return v if v else current


def interactive_setup() -> None:
    cfg = load_config()
    print("\n=== MC Quest Generator — Provider Setup ===")
    print("Press Enter to keep current value. Leave blank to skip a provider.\n")

    print("── OpenRouter (https://openrouter.ai — free models available) ──")
    cfg["openrouter"]["api_key"] = _ask("API key", cfg["openrouter"].get("api_key", ""))
    cfg["openrouter"]["model"] = _ask("Primary model", cfg["openrouter"].get("model", ""))

    print("\n── Chutes (https://chutes.ai) ──")
    cfg["chutes"]["api_key"] = _ask("API key", cfg["chutes"].get("api_key", ""))
    cfg["chutes"]["model"] = _ask("Model", cfg["chutes"].get("model", ""))

    print("\n── DeepSeek direct (https://platform.deepseek.com) ──")
    cfg["deepseek"]["api_key"] = _ask("API key", cfg["deepseek"].get("api_key", ""))
    cfg["deepseek"]["model"] = _ask("Model", cfg["deepseek"].get("model", ""))

    print("\n── ElectronHub (https://electronhub.ai) ──")
    cfg["electronhub"]["api_key"] = _ask("API key", cfg["electronhub"].get("api_key", ""))
    cfg["electronhub"]["model"] = _ask("Model", cfg["electronhub"].get("model", ""))

    print("\n── Groq direct (https://console.groq.com) ──")
    cfg["groq"]["api_key"] = _ask("API key", cfg["groq"].get("api_key", ""))
    cfg["groq"]["model"] = _ask("Model", cfg["groq"].get("model", ""))

    print("\n── HuggingFace router (https://huggingface.co/settings/tokens) ──")
    cfg["huggingface"]["api_key"] = _ask("API key", cfg["huggingface"].get("api_key", ""))
    cfg["huggingface"]["model"] = _ask("Model", cfg["huggingface"].get("model", ""))

    print("\n── Google Gemini (https://aistudio.google.com/app/apikey) ──")
    cfg["google_gemini"]["api_key"] = _ask("API key", cfg["google_gemini"].get("api_key", ""))
    cfg["google_gemini"]["model"] = _ask("Model", cfg["google_gemini"].get("model", ""))

    print("\n── g4f.space proxy (https://g4f.space) ──")
    cfg["g4f_groq"]["api_key"] = _ask("API key", cfg["g4f_groq"].get("api_key", ""))
    cfg["g4f_groq"]["model"] = _ask("Model", cfg["g4f_groq"].get("model", ""))

    print("\n── Cloudflare Workers AI ──")
    cfg["cloudflare"]["api_token"] = _ask("API token", cfg["cloudflare"].get("api_token", ""))
    cfg["cloudflare"]["account_id"] = _ask("Account ID", cfg["cloudflare"].get("account_id", ""))
    cfg["cloudflare"]["model"] = _ask("Model", cfg["cloudflare"].get("model", ""))

    print("\n── Fallback chain order ──")
    print("  Current:", " → ".join(cfg.get("chain", [])))
    print(f"  Available: {', '.join(PROVIDERS.keys())}")
    chain_input = input("  New order (comma-separated, Enter to keep): ").strip()
    if chain_input:
        new_chain = [p.strip() for p in chain_input.split(",") if p.strip() in PROVIDERS]
        if new_chain:
            cfg["chain"] = new_chain

    save_config(cfg)
    print(f"\n✓ Config saved to {CONFIG_PATH}\n")
    print("Running smoke test…")
    for name, status in test_providers(cfg).items():
        icon = "✓" if status.startswith("ok") else ("·" if status == "no_key" else "✗")
        print(f"  {icon} {name:14s} {status}")
