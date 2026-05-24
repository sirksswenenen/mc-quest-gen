"""
AI Provider manager for MC Quest Generator.
Supports: OpenRouter, Cloudflare Workers AI, Google Gemini, G4F (Groq/Nvidia).
Auto-fallback chain: tries providers in order, skips failed ones.
Config is saved to providers_config.json in the script directory.
"""

import json
import os
import time
import random
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Optional

CONFIG_PATH = Path(__file__).parent / "providers_config.json"

# ─────────────────────────────────────────────
# Default config skeleton (no keys)
# ─────────────────────────────────────────────
DEFAULT_CONFIG = {
    "primary": "openrouter",
    "chain": ["openrouter", "google_gemini", "cloudflare", "g4f_groq"],
    "openrouter": {
        "api_key": "",
        "model": "deepseek/deepseek-chat-v3-0324:free",
        "fallback_models": [
            "deepseek/deepseek-chat-v3-0324:free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "qwen/qwen-2.5-72b-instruct:free",
            "google/gemini-2.0-flash-exp:free",
            "mistralai/mistral-7b-instruct:free",
        ],
    },
    "cloudflare": {
        "api_token": "",
        "account_id": "",
        "model": "@cf/qwen/qwen3-30b-a3b-fp8",
    },
    "google_gemini": {
        "api_key": "",
        "model": "gemini-2.0-flash",
    },
    "g4f_groq": {
        "api_key": "",
        "model": "meta-llama/llama-4-scout-17b-16e-instruct",
    },
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            # Merge with defaults so new keys are always present
            merged = json.loads(json.dumps(DEFAULT_CONFIG))
            _deep_merge(merged, data)
            return merged
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# ─────────────────────────────────────────────
# Rate-limit cooldown (in-process)
# ─────────────────────────────────────────────
_cooldowns: dict[str, float] = {}  # key -> until_ts


def _is_cooling(key: str) -> bool:
    return _cooldowns.get(key, 0) > time.time()


def _set_cool(key: str, seconds: int = 60) -> None:
    _cooldowns[key] = time.time() + seconds


# ─────────────────────────────────────────────
# HTTP helper (no external deps)
# ─────────────────────────────────────────────
def _post(url: str, headers: dict, body: dict, timeout: int = 30) -> Optional[dict]:
    data = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        code = e.code
        if code == 429:
            return {"_rate_limit": True, "code": code}
        body_text = ""
        try:
            body_text = e.read().decode()[:200]
        except Exception:
            pass
        return {"_error": True, "code": code, "body": body_text}
    except Exception as e:
        return {"_error": True, "msg": str(e)}


def _extract_text(data: dict) -> Optional[str]:
    """Universal text extractor from various provider response formats."""
    if not isinstance(data, dict):
        return None
    # OpenAI-style (OpenRouter, G4F)
    choices = data.get("choices")
    if choices and isinstance(choices, list):
        content = choices[0].get("message", {}).get("content")
        if content:
            return str(content).strip()
    # Cloudflare Workers AI
    result = data.get("result", {})
    if isinstance(result, dict):
        text = result.get("response") or (result.get("choices") or [{}])[0].get("message", {}).get("content")
        if text:
            return str(text).strip()
    # Google Gemini
    candidates = data.get("candidates")
    if candidates and isinstance(candidates, list):
        parts = candidates[0].get("content", {}).get("parts", [])
        bits = [p.get("text", "") for p in parts if "text" in p]
        if bits:
            return " ".join(bits).strip()
    return None


def _strip_thinking(text: str) -> str:
    """Remove <think>...</think> and similar reasoning blocks."""
    import re
    text = re.sub(r"<think\b[^>]*>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<think\b[^>]*>.*$", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"</?think>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[THINK\].*?\[/THINK\]", "", text, flags=re.DOTALL | re.IGNORECASE)
    return text.strip()


# ─────────────────────────────────────────────
# Individual providers
# ─────────────────────────────────────────────

def _call_openrouter(messages: list, cfg: dict) -> Optional[str]:
    pc = cfg.get("openrouter", {})
    key = pc.get("api_key", "")
    if not key:
        return None
    models = list(pc.get("fallback_models", [pc.get("model", "")]))
    if pc.get("model") and pc["model"] not in models:
        models.insert(0, pc["model"])
    models = list(dict.fromkeys(models))  # deduplicate, preserve order

    for model in models:
        ck = f"openrouter::{model}"
        if _is_cooling(ck):
            continue
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": 4096,
            "temperature": 0.7,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "HTTP-Referer": "https://github.com/mc-quest-gen",
            "X-Title": "MC-Quest-Gen",
        }
        resp = _post("https://openrouter.ai/api/v1/chat/completions", headers, body, timeout=60)
        if resp is None:
            continue
        if resp.get("_rate_limit"):
            _set_cool(ck, 90)
            continue
        if resp.get("_error"):
            continue
        text = _extract_text(resp)
        if text:
            return _strip_thinking(text)
    return None


def _call_cloudflare(messages: list, cfg: dict) -> Optional[str]:
    pc = cfg.get("cloudflare", {})
    token = pc.get("api_token", "")
    account = pc.get("account_id", "")
    model = pc.get("model", "@cf/qwen/qwen3-30b-a3b-fp8")
    if not token or not account:
        return None
    ck = f"cloudflare::{model}"
    if _is_cooling(ck):
        return None
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {"messages": messages, "stream": False, "max_tokens": 4096, "temperature": 0.7}
    resp = _post(url, headers, body, timeout=30)
    if resp is None or resp.get("_error") or resp.get("_rate_limit"):
        if resp and resp.get("_rate_limit"):
            _set_cool(ck, 120)
        return None
    return _extract_text(resp)


def _call_gemini(messages: list, cfg: dict) -> Optional[str]:
    pc = cfg.get("google_gemini", {})
    key = pc.get("api_key", "")
    model = pc.get("model", "gemini-2.0-flash")
    if not key:
        return None
    ck = f"gemini::{model}"
    if _is_cooling(ck):
        return None

    # Convert OpenAI messages → Gemini format
    contents = []
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
    headers = {"Content-Type": "application/json"}
    resp = _post(url, headers, payload, timeout=60)
    if resp is None or resp.get("_error") or resp.get("_rate_limit"):
        if resp and resp.get("_rate_limit"):
            _set_cool(ck, 60)
        return None
    return _extract_text(resp)


def _call_g4f_groq(messages: list, cfg: dict) -> Optional[str]:
    pc = cfg.get("g4f_groq", {})
    key = pc.get("api_key", "")
    model = pc.get("model", "meta-llama/llama-4-scout-17b-16e-instruct")
    if not key:
        return None
    ck = f"g4f_groq::{model}"
    if _is_cooling(ck):
        return None
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    }
    body = {"model": model, "messages": messages, "max_tokens": 4096, "temperature": 0.7}
    resp = _post("https://g4f.space/api/groq/chat/completions", headers, body, timeout=30)
    if resp is None or resp.get("_error") or resp.get("_rate_limit"):
        if resp and resp.get("_rate_limit"):
            _set_cool(ck, 60)
        return None
    text = _extract_text(resp)
    return _strip_thinking(text) if text else None


_PROVIDER_CALLERS = {
    "openrouter": _call_openrouter,
    "cloudflare": _call_cloudflare,
    "google_gemini": _call_gemini,
    "g4f_groq": _call_g4f_groq,
}

# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

def ai_call(messages: list, cfg: Optional[dict] = None) -> str:
    """
    Send messages to AI. Walks the provider chain until one responds.
    Returns the text, or raises RuntimeError if all fail.
    """
    if cfg is None:
        cfg = load_config()

    chain = cfg.get("chain", list(_PROVIDER_CALLERS.keys()))
    primary = cfg.get("primary", chain[0] if chain else "openrouter")

    # Ensure primary is first
    ordered = [primary] + [p for p in chain if p != primary]
    # Deduplicate
    seen: set = set()
    ordered = [p for p in ordered if not (p in seen or seen.add(p))]  # type: ignore[func-returns-value]

    for provider in ordered:
        fn = _PROVIDER_CALLERS.get(provider)
        if fn is None:
            continue
        try:
            result = fn(messages, cfg)
            if result and result.strip():
                return result.strip()
        except Exception:
            continue

    raise RuntimeError(
        "All AI providers failed. Check your API keys in providers_config.json "
        "or run: python mc_quest_gen.py --setup"
    )


def test_providers(cfg: Optional[dict] = None) -> dict:
    """Quick ping test for all configured providers. Returns {name: ok/fail/no_key}."""
    if cfg is None:
        cfg = load_config()
    results = {}
    test_msg = [
        {"role": "system", "content": "Reply with exactly one word."},
        {"role": "user", "content": "Say 'ok'"},
    ]
    for name, fn in _PROVIDER_CALLERS.items():
        pc = cfg.get(name, {})
        has_key = bool(
            pc.get("api_key") or pc.get("api_token") or
            (pc.get("account_id") and pc.get("api_token"))
        )
        if not has_key:
            results[name] = "no_key"
            continue
        try:
            t0 = time.time()
            resp = fn(test_msg, cfg)
            ms = int((time.time() - t0) * 1000)
            results[name] = f"ok ({ms}ms)" if resp else "fail (empty response)"
        except Exception as e:
            results[name] = f"fail ({e})"
    return results


def interactive_setup() -> None:
    """CLI wizard to configure API keys."""
    cfg = load_config()
    print("\n=== MC Quest Generator — Provider Setup ===\n")
    print("Press Enter to keep the current value.\n")

    def ask(prompt: str, current: str) -> str:
        show = f"[{current[:8]}…]" if len(current) > 8 else f"[{current or 'not set'}]"
        val = input(f"  {prompt} {show}: ").strip()
        return val if val else current

    # OpenRouter
    print("── OpenRouter (free models available, recommended) ──")
    cfg["openrouter"]["api_key"] = ask("API key", cfg["openrouter"].get("api_key", ""))
    cfg["openrouter"]["model"] = ask("Model", cfg["openrouter"].get("model", "deepseek/deepseek-chat-v3-0324:free"))

    # Cloudflare
    print("\n── Cloudflare Workers AI ──")
    cfg["cloudflare"]["api_token"] = ask("API token", cfg["cloudflare"].get("api_token", ""))
    cfg["cloudflare"]["account_id"] = ask("Account ID", cfg["cloudflare"].get("account_id", ""))
    cfg["cloudflare"]["model"] = ask("Model", cfg["cloudflare"].get("model", "@cf/qwen/qwen3-30b-a3b-fp8"))

    # Google Gemini
    print("\n── Google Gemini ──")
    cfg["google_gemini"]["api_key"] = ask("API key", cfg["google_gemini"].get("api_key", ""))
    cfg["google_gemini"]["model"] = ask("Model", cfg["google_gemini"].get("model", "gemini-2.0-flash"))

    # G4F Groq
    print("\n── G4F Groq ──")
    cfg["g4f_groq"]["api_key"] = ask("API key", cfg["g4f_groq"].get("api_key", ""))
    cfg["g4f_groq"]["model"] = ask("Model", cfg["g4f_groq"].get("model", "meta-llama/llama-4-scout-17b-16e-instruct"))

    # Chain order
    print("\n── Provider fallback chain ──")
    print("  Current:", " → ".join(cfg.get("chain", [])))
    chain_input = input("  New order (comma-separated, or Enter to keep): ").strip()
    if chain_input:
        new_chain = [p.strip() for p in chain_input.split(",") if p.strip() in _PROVIDER_CALLERS]
        if new_chain:
            cfg["chain"] = new_chain
            cfg["primary"] = new_chain[0]

    save_config(cfg)
    print(f"\n✓ Config saved to {CONFIG_PATH}")

    print("\nRunning quick test…")
    results = test_providers(cfg)
    for name, status in results.items():
        icon = "✓" if status.startswith("ok") else ("—" if status == "no_key" else "✗")
        print(f"  {icon} {name}: {status}")
