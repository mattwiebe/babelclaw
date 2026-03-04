#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["beeper-desktop-api", "httpx"]
# ///
"""
Beeper -> LM Studio -> Discord bridge daemon.

Flow:
- Watch new inbound Beeper Desktop messages.
- If message is mostly Spanish (or other language), translate to Spanish with LM Studio.
- Send only translated Spanish messages to selected OpenClaw CLI messaging channel.
- Ignore English / mixed-greeting messages.

Subcommands:
  install    Interactive onboarding + write config + install launchd plist
  run        Run daemon loop
  once       Run one processing pass and exit
  doctor     Validate binary paths and OpenClaw messaging dry-run
  uninstall  Unload launchd plist + optional config/state cleanup
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

CONFIG_PATH = Path.home() / ".config" / "babelclaw-daemon" / "config.json"
STATE_PATH = Path.home() / ".openclaw" / "state" / "babelclaw-daemon" / "state.json"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / "com.mattwiebe.babelclaw-daemon.plist"
LAUNCHD_LABEL = "com.mattwiebe.babelclaw-daemon"
SCRIPT_PATH = Path(__file__).resolve()


def build_system_prompt(from_language: str, to_language: str, regional_context: str) -> str:
    return f"""You are a strict language gate and translator.

Task:
- Source language: {from_language}
- Target language: {to_language}
- Regional preference for source: {regional_context}

For each input message, do exactly one of:

1) If the message is predominantly in {from_language}, output:
TRANSLATED: <natural {to_language} translation>

2) Otherwise output:
IGNORE

Predominantly means at least ~70% of meaningful tokens are in {from_language}.
If the message is mixed-language, uncertain, or mostly not {from_language}, output IGNORE.

Rules:
- Output exactly one line.
- Do not output explanations, labels other than TRANSLATED:/IGNORE, JSON, quotes, thinking traces, or extra text.
- Preserve intent and tone in translation; keep it concise.
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_epoch() -> int:
    return int(time.time())


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def normalize_text(text: str) -> str:
    return " ".join(str(text or "").strip().split()).casefold()


def normalize_name(name: str) -> str:
    value = str(name or "").strip()
    if not value:
        return ""
    if value[0] in {"#", "@"}:
        value = value[1:]
    return normalize_text(value)


def default_config() -> dict[str, Any]:
    return {
        "beeper_access_token": "",
        "lmstudio_base_url": "http://127.0.0.1:1234/v1",
        "lmstudio_model": "qwen3-4b-instruct-2507-mlx",
        "discord_target": "#cassiel",
        "interval_seconds": 8,
        "adaptive_polling": True,
        "active_interval_seconds": 3,
        "active_window_seconds": 60,
        "medium_idle_after_seconds": 300,
        "idle_interval_seconds": 20,
        "max_idle_after_seconds": 1800,
        "max_interval_seconds": 60,
        "messages_per_chat": 5,
        "send_retries": 3,
        "dedupe_hours": 24,
        "search_limit": 20,
        "chat_metadata_ttl_seconds": 900,
        "chat_metadata_error_ttl_seconds": 120,
        "chat_metadata_max_entries": 2000,
        "openclaw_bin": shutil.which("openclaw") or "openclaw",
        "ignore_muted_chats": True,
        "drop_messages_without_chat_metadata": True,
        "ignored_networks": ["discord"],
        "ignored_chat_ids": [],
        "ignored_chat_title_contains": [],
        "ignored_sender_names": [],
        "ignored_sender_name_contains": [],
        "ignore_recently_sent_echoes": True,
        "recent_sent_ttl_seconds": 900,
        "recent_sent_max_entries": 200,
        "from_language": "Spanish",
        "to_language": "English",
        "regional_context": "Mexican/LatAm",
        "output_flag": "🇲🇽",
        "log_ignored_messages": False,
        "log_chat_skips": False,
    }


def load_config() -> dict[str, Any]:
    cfg = load_json(CONFIG_PATH, default_config())

    # Optional env overrides for ops flexibility.
    cfg["beeper_access_token"] = (
        os.environ.get("BEEPER_ACCESS_TOKEN")
        or cfg.get("beeper_access_token", "")
    )
    cfg["lmstudio_base_url"] = os.environ.get("LMSTUDIO_BASE_URL") or cfg.get("lmstudio_base_url")
    cfg["lmstudio_model"] = os.environ.get("LMSTUDIO_MODEL") or cfg.get("lmstudio_model")
    cfg["discord_target"] = os.environ.get("DISCORD_TARGET") or cfg.get("discord_target")

    if not cfg.get("beeper_access_token"):
        raise RuntimeError(
            f"Missing beeper_access_token. Run install: uv run {SCRIPT_PATH} install"
        )

    return cfg


def resolve_openclaw_bin(cfg: dict[str, Any]) -> str | None:
    configured = str(cfg.get("openclaw_bin") or "").strip()
    if configured and Path(configured).exists():
        return configured
    discovered = shutil.which("openclaw")
    if discovered:
        return discovered
    return None


def validate_install_environment() -> tuple[str, str]:
    uv_bin = shutil.which("uv")
    if not uv_bin:
        raise RuntimeError("`uv` not found in PATH. Install uv first.")

    openclaw_bin = shutil.which("openclaw")
    if not openclaw_bin:
        raise RuntimeError("`openclaw` not found in PATH. Install/configure OpenClaw CLI first.")

    return uv_bin, openclaw_bin


def prune_seen(seen: dict[str, dict[str, int]], max_age_hours: int) -> dict[str, dict[str, int]]:
    cutoff = now_epoch() - (max_age_hours * 3600)
    return {k: v for k, v in seen.items() if v.get("first_seen_epoch", 0) >= cutoff}


def beeper_client(token: str):
    from beeper_desktop_api import BeeperDesktop

    return BeeperDesktop(access_token=token)


def likely_english_with_tiny_spanish_tail(text: str, from_language: str) -> bool:
    """Cheap guardrail to prevent obvious mixed-English false positives.

    Applies only when translating FROM Spanish.
    """
    if from_language.strip().lower() != "spanish":
        return False

    import re

    tokens = re.findall(r"[A-Za-zÁÉÍÓÚáéíóúÑñ']+", text.lower())
    if not tokens:
        return False

    english_markers = {
        "i", "am", "doing", "well", "today", "thanks", "thank", "you", "the", "and", "is", "are",
        "to", "of", "for", "with", "on", "in", "my", "your", "we", "it",
    }
    spanish_markers = {
        "hola", "gracias", "como", "estas", "estás", "buenos", "buenas", "dias", "días", "por", "favor",
        "que", "qué", "de", "la", "el", "y", "muy", "bien", "amigo", "amiga",
    }

    en = sum(1 for t in tokens if t in english_markers)
    es = sum(1 for t in tokens if t in spanish_markers)

    # English-dominant + tiny Spanish tail (like "..., gracias") => ignore.
    return en >= 3 and es <= 2 and en > es


def likely_english_not_spanish(text: str, from_language: str) -> bool:
    """Conservative local guardrail to avoid obvious English false positives."""
    if from_language.strip().lower() != "spanish":
        return False

    import re

    tokens = re.findall(r"[A-Za-zÁÉÍÓÚáéíóúÑñ']+", text.lower())
    if not tokens:
        return False

    english_words = {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from", "had", "has", "have", "he",
        "her", "here", "him", "his", "i", "i'm", "if", "in", "into", "is", "it", "its", "just", "me", "my", "not", "of",
        "on", "or", "our", "over", "please", "replied", "reply", "said", "send", "sent", "she", "since", "that", "the",
        "their", "them", "then", "there", "they", "this", "to", "today", "tomorrow", "was", "we", "were",
        "what", "when", "where", "with", "you", "your", "hello", "hey", "thanks", "thank", "message",
        "meeting", "schedule", "call", "received", "like", "knowing", "things", "long", "yes",
        "unfortunately", "taken", "politics", "perfect", "okay",
    }
    spanish_words = {
        "que", "qué", "de", "la", "el", "los", "las", "y", "en", "por", "para", "con", "hola", "gracias",
        "buenos", "buenas", "días", "dias", "cómo", "como", "estás", "estas", "usted", "ustedes", "nosotros",
        "muy", "bien", "pero", "porque", "también", "tambien", "tengo", "quiero", "puedo", "mensaje",
    }

    en = sum(1 for t in tokens if t in english_words)
    es = sum(1 for t in tokens if t in spanish_words)
    has_spanish_punct = any(ch in text for ch in ("¿", "¡"))
    has_spanish_diacritics = bool(re.search(r"[áéíóúñÁÉÍÓÚÑ]", text))

    # Short plain-English texts are high-confidence false positives in one-way mode.
    if len(tokens) <= 2 and es == 0 and en >= 1 and not has_spanish_punct and not has_spanish_diacritics:
        return True

    # Clear English with little/no Spanish signals.
    if en >= 3 and es == 0 and not has_spanish_punct and not has_spanish_diacritics:
        return True

    # Broad English dominance.
    if en >= 4 and en >= (es + 3):
        return True

    # Extra guard for medium-length plain ASCII English-like text.
    if len(tokens) >= 7 and en >= 3 and es <= 1 and not has_spanish_punct and not has_spanish_diacritics:
        return True

    return False


def has_spanish_signals(text: str) -> bool:
    import re

    lowered = text.lower()
    if any(ch in text for ch in ("¿", "¡")):
        return True
    if re.search(r"[áéíóúñÁÉÍÓÚÑ]", text):
        return True

    tokens = re.findall(r"[A-Za-zÁÉÍÓÚáéíóúÑñ']+", lowered)
    if not tokens:
        return False

    spanish_words = {
        "que", "qué", "de", "la", "el", "los", "las", "y", "en", "por", "para", "con", "hola", "gracias",
        "buenos", "buenas", "días", "dias", "cómo", "como", "estás", "estas", "usted", "ustedes", "nosotros",
        "muy", "bien", "pero", "porque", "también", "tambien", "tengo", "quiero", "puedo", "mensaje",
    }
    return sum(1 for t in tokens if t in spanish_words) >= 2


def should_accept_translation(source_text: str, translated_text: str, from_language: str) -> bool:
    """Reject suspicious no-op translations for one-way translation mode."""
    if from_language.strip().lower() != "spanish":
        return True

    src = " ".join(source_text.strip().split()).lower()
    dst = " ".join(translated_text.strip().split()).lower()
    if not src or not dst:
        return False

    similarity = difflib.SequenceMatcher(a=src, b=dst).ratio()
    if similarity >= 0.92 and not has_spanish_signals(source_text):
        return False
    return True


def is_chat_muted(chat: Any) -> bool:
    if not chat:
        return False
    return bool(
        getattr(chat, "is_muted", False)
        or getattr(chat, "muted", False)
        or getattr(chat, "isMuted", False)
    )


def classify_or_translate(
    base_url: str,
    model: str,
    text: str,
    from_language: str,
    to_language: str,
    regional_context: str,
    timeout_s: int = 25,
) -> str:
    with httpx.Client(timeout=timeout_s) as client:
        r = client.post(
            f"{base_url}/chat/completions",
            json={
                "model": model,
                "temperature": 0,
                "messages": [
                    {
                        "role": "system",
                        "content": build_system_prompt(from_language, to_language, regional_context),
                    },
                    {"role": "user", "content": text},
                ],
            },
        )
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()


def send_to_discord(openclaw_bin: str, target: str, body: str, retries: int = 3) -> None:
    cmd = [
        openclaw_bin,
        "message",
        "send",
        "--channel",
        "discord",
        "--target",
        target,
        "--message",
        body,
    ]

    attempt = 1
    while True:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            return
        if attempt >= retries:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "unknown send error")
        time.sleep(2 ** (attempt - 1))
        attempt += 1


def message_key(msg: Any) -> str:
    mid = getattr(msg, "id", None)
    if mid:
        return str(mid)
    return f"{getattr(msg, 'chat_id', '')}:{getattr(msg, 'timestamp', '')}:{getattr(msg, 'sender_id', '')}"


def process_once(
    cfg: dict[str, Any],
    verbose: bool = False,
    force_seed_only: bool = False,
    trace: bool = False,
) -> dict[str, int | bool]:
    state = load_json(
        STATE_PATH,
        {"seen": {}, "bootstrapped": False, "last_poll_at": None, "updated_at": None},
    )
    seen = prune_seen(state.get("seen", {}), max_age_hours=int(cfg.get("dedupe_hours", 24)))

    poll_started_at = now_iso()
    first_run_seed = force_seed_only or not state.get("bootstrapped", False)
    if first_run_seed:
        if verbose:
            print("[info] First run detected. Setting cursor to now and waiting for new messages.")
        state["seen"] = seen
        state["bootstrapped"] = True
        state["last_poll_at"] = poll_started_at
        state["updated_at"] = poll_started_at
        save_json(STATE_PATH, state)
        return {"translated_count": 0, "activity_count": 0, "seeded": True}

    client = beeper_client(cfg["beeper_access_token"])
    translated_count = 0
    activity_count = 0

    ignored_networks = {str(x).strip().lower() for x in cfg.get("ignored_networks", []) if str(x).strip()}
    ignored_chat_ids = {str(x).strip() for x in cfg.get("ignored_chat_ids", []) if str(x).strip()}
    ignored_title_contains = [str(x).strip().lower() for x in cfg.get("ignored_chat_title_contains", []) if str(x).strip()]
    ignored_sender_names = {normalize_name(x) for x in cfg.get("ignored_sender_names", []) if normalize_name(x)}
    ignored_sender_name_contains = [normalize_text(x) for x in cfg.get("ignored_sender_name_contains", []) if normalize_text(x)]
    target_sender_name = normalize_name(str(cfg.get("discord_target") or ""))
    if target_sender_name:
        ignored_sender_names.add(target_sender_name)

    chat_cache: dict[str, dict[str, Any]] = cfg.setdefault("_runtime_chat_cache", {})
    chat_ttl_s = max(10, int(cfg.get("chat_metadata_ttl_seconds", 900)))
    chat_error_ttl_s = max(5, int(cfg.get("chat_metadata_error_ttl_seconds", 120)))
    chat_max_entries = max(100, int(cfg.get("chat_metadata_max_entries", 2000)))
    recent_sent_cache: dict[str, int] = cfg.setdefault("_runtime_recent_sent_texts", {})
    recent_sent_ttl_s = max(30, int(cfg.get("recent_sent_ttl_seconds", 900)))
    recent_sent_max_entries = max(20, int(cfg.get("recent_sent_max_entries", 200)))

    date_after = state.get("last_poll_at") or poll_started_at
    limit = max(1, min(20, int(cfg.get("search_limit", 20))))

    try:
        recent_messages = list(
            client.messages.search(
                date_after=date_after,
                include_muted=not bool(cfg.get("ignore_muted_chats", True)),
                limit=limit,
                direction="after",
            )
        )
    except Exception as e:
        if verbose:
            print(f"[warn] Could not search recent messages: {e}")
        recent_messages = []

    def msg_ts(m: Any) -> str:
        return str(getattr(m, "timestamp", "") or "")

    recent_messages.sort(key=msg_ts)

    def prune_recent_sent_cache(now_value: int | None = None) -> None:
        now_epoch_value = now_epoch() if now_value is None else now_value
        expired = [k for k, ts in recent_sent_cache.items() if now_epoch_value - int(ts) >= recent_sent_ttl_s]
        for k in expired:
            recent_sent_cache.pop(k, None)
        if len(recent_sent_cache) <= recent_sent_max_entries:
            return
        oldest = sorted(recent_sent_cache.items(), key=lambda item: int(item[1]))
        drop_n = len(recent_sent_cache) - recent_sent_max_entries
        for k, _ in oldest[:drop_n]:
            recent_sent_cache.pop(k, None)

    def remember_recent_sent(text: str) -> None:
        key = normalize_text(text)
        if not key:
            return
        now_value = now_epoch()
        recent_sent_cache[key] = now_value
        prune_recent_sent_cache(now_value)

    def is_recently_sent_echo(text: str) -> bool:
        if not cfg.get("ignore_recently_sent_echoes", True):
            return False
        prune_recent_sent_cache()
        key = normalize_text(text)
        return bool(key and key in recent_sent_cache)

    def prune_chat_cache() -> None:
        if len(chat_cache) <= chat_max_entries:
            return
        # Drop oldest cache entries first.
        oldest = sorted(
            ((cid, int(meta.get("cached_at_epoch", 0))) for cid, meta in chat_cache.items()),
            key=lambda item: item[1],
        )
        drop_n = len(chat_cache) - chat_max_entries
        for cid, _ in oldest[:drop_n]:
            chat_cache.pop(cid, None)

    def get_chat(chat_id: str) -> Any:
        if not chat_id:
            return None
        now = now_epoch()
        cached = chat_cache.get(chat_id)
        if cached:
            cached_at = int(cached.get("cached_at_epoch", 0))
            cached_chat = cached.get("chat")
            ttl = chat_ttl_s if cached_chat is not None else chat_error_ttl_s
            if now - cached_at < ttl:
                return cached_chat
        try:
            chat = client.chats.retrieve(chat_id)
            chat_cache[chat_id] = {"chat": chat, "cached_at_epoch": now}
            prune_chat_cache()
            return chat
        except Exception as e:
            if verbose and cfg.get("log_chat_skips", False):
                print(f"[warn] Could not retrieve chat metadata for {chat_id}: {e}")
            # Cache lookup failures briefly to avoid hammering.
            chat_cache[chat_id] = {"chat": None, "cached_at_epoch": now}
            prune_chat_cache()
            return None

    for msg in recent_messages:
        chat_id = str(getattr(msg, "chat_id", "") or "")

        def trace_msg(reason: str) -> None:
            if not trace:
                return
            sender = getattr(msg, "sender_name", None) or getattr(msg, "sender_id", "unknown")
            text = (getattr(msg, "text", "") or "").strip().replace("\n", " ")
            text_preview = text[:120]
            msg_id = getattr(msg, "id", "") or message_key(msg)
            print(f"[trace] {reason} | id={msg_id} | chat={chat_id or '?'} | sender={sender} | text={text_preview!r}")

        if getattr(msg, "is_sender", False):
            trace_msg("SKIP_SELF")
            continue

        key = message_key(msg)
        if key in seen:
            trace_msg("SKIP_SEEN")
            continue

        activity_count += 1

        chat = get_chat(chat_id)
        chat_network = str(getattr(chat, "network", "") or "").lower() if chat else ""
        chat_title = str(getattr(chat, "title", "") or "") if chat else chat_id
        chat_muted = is_chat_muted(chat)
        sender = getattr(msg, "sender_name", None) or getattr(msg, "sender_id", "unknown")
        sender_name_norm = normalize_name(sender)

        if cfg.get("ignore_muted_chats", True) and chat_muted:
            trace_msg("SKIP_MUTED")
            continue
        if cfg.get("ignore_muted_chats", True) and not chat and cfg.get("drop_messages_without_chat_metadata", True):
            if verbose and cfg.get("log_chat_skips", False):
                print(f"[skip] Unknown chat metadata; dropped while mute filtering is enabled: {chat_id}")
            trace_msg("SKIP_UNKNOWN_CHAT_METADATA")
            continue
        if chat_network and chat_network in ignored_networks:
            trace_msg("SKIP_IGNORED_NETWORK")
            continue
        if chat_id and chat_id in ignored_chat_ids:
            trace_msg("SKIP_IGNORED_CHAT_ID")
            continue
        if chat_title and any(snippet in chat_title.lower() for snippet in ignored_title_contains):
            trace_msg("SKIP_IGNORED_CHAT_TITLE")
            continue
        if sender_name_norm and sender_name_norm in ignored_sender_names:
            trace_msg("SKIP_IGNORED_SENDER")
            continue
        if sender_name_norm and any(snippet in sender_name_norm for snippet in ignored_sender_name_contains):
            trace_msg("SKIP_IGNORED_SENDER_CONTAINS")
            continue

        text = (getattr(msg, "text", "") or "").strip()
        if not text:
            seen[key] = {"first_seen_epoch": now_epoch()}
            trace_msg("SKIP_EMPTY_TEXT")
            continue
        if is_recently_sent_echo(text):
            seen[key] = {"first_seen_epoch": now_epoch()}
            trace_msg("SKIP_RECENTLY_SENT_ECHO")
            continue

        seen[key] = {"first_seen_epoch": now_epoch()}

        if likely_english_with_tiny_spanish_tail(text, cfg.get("from_language", "Spanish")):
            if verbose and cfg.get("log_ignored_messages", False):
                print(f"[skip] MIXED-ENGLISH: {sender}: {text[:80]}")
            trace_msg("SKIP_MIXED_ENGLISH_TAIL")
            continue
        if likely_english_not_spanish(text, cfg.get("from_language", "Spanish")):
            if verbose and cfg.get("log_ignored_messages", False):
                print(f"[skip] ENGLISH-HEURISTIC: {sender}: {text[:80]}")
            trace_msg("SKIP_ENGLISH_HEURISTIC")
            continue

        try:
            result = classify_or_translate(
                cfg["lmstudio_base_url"],
                cfg["lmstudio_model"],
                text,
                cfg.get("from_language", "Spanish"),
                cfg.get("to_language", "English"),
                cfg.get("regional_context", "Mexican/LatAm"),
            )
        except Exception as e:
            if verbose:
                print(f"[warn] LM Studio classify failed for {key}: {e}")
            trace_msg("SKIP_LLM_ERROR")
            continue

        if result == "IGNORE":
            if verbose and cfg.get("log_ignored_messages", False):
                print(f"[skip] IGNORE: {sender}: {text[:80]}")
            trace_msg("SKIP_LLM_IGNORE")
            continue

        if result.startswith("TRANSLATED:"):
            translation = result.split("TRANSLATED:", 1)[1].strip()
            if not should_accept_translation(text, translation, cfg.get("from_language", "Spanish")):
                if verbose and cfg.get("log_ignored_messages", False):
                    print(f"[skip] NO-OP-TRANSLATION: {sender}: {text[:80]}")
                trace_msg("SKIP_NOOP_TRANSLATION")
                continue
            out = f"{cfg.get('output_flag', '🇲🇽')} {translation} — {sender}"
            try:
                send_to_discord(
                    cfg.get("openclaw_bin") or shutil.which("openclaw") or "openclaw",
                    cfg["discord_target"],
                    out,
                    retries=int(cfg.get("send_retries", 3)),
                )
                remember_recent_sent(out)
                translated_count += 1
                if verbose:
                    print(f"[sent] {out}")
                trace_msg("SEND_TRANSLATION")
            except Exception as e:
                print(f"[error] Discord send failed for {key}: {e}")
                trace_msg("SKIP_DISCORD_SEND_ERROR")
            continue

        if verbose:
            print(f"[skip] Unrecognized model output: {result!r}")
        trace_msg("SKIP_UNRECOGNIZED_MODEL_OUTPUT")

    state["seen"] = seen
    state["bootstrapped"] = True
    state["last_poll_at"] = poll_started_at
    state["updated_at"] = now_iso()
    save_json(STATE_PATH, state)
    return {"translated_count": translated_count, "activity_count": activity_count, "seeded": False}


def choose_model_interactive(default_model: str) -> str:
    models: list[str] = []
    if shutil.which("lms"):
        proc = subprocess.run(["lms", "ls", "--llm", "--json"], capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                payload = json.loads(proc.stdout)
                items = payload if isinstance(payload, list) else payload.get("data", [])
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    model_id = (
                        item.get("modelKey")
                        or item.get("id")
                        or item.get("identifier")
                        or item.get("slug")
                        or item.get("name")
                    )
                    if model_id:
                        model_id = str(model_id).strip()
                        if model_id and model_id not in models:
                            models.append(model_id)
            except Exception:
                models = []

    if models:
        print("\nAvailable LM Studio LLM models (from `lms ls --llm --json`):")
        for i, m in enumerate(models, start=1):
            print(f"  {i}. {m}")
        choice = input(f"Choose model [default {default_model}]: ").strip()
        if not choice:
            return default_model
        if choice.isdigit() and 1 <= int(choice) <= len(models):
            return models[int(choice) - 1]
        return choice

    print("\nCould not read LLM models via `lms ls --llm --json` in this shell.")
    value = input(f"Enter LM Studio model id [default {default_model}]: ").strip()
    return value or default_model


def build_launchd_plist(interval_seconds: int) -> str:
    uv_bin = shutil.which("uv") or "/opt/homebrew/bin/uv"
    path_env = os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    return f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
  <key>Label</key>
  <string>com.mattwiebe.babelclaw-daemon</string>

  <key>ProgramArguments</key>
  <array>
    <string>{uv_bin}</string>
    <string>run</string>
    <string>{SCRIPT_PATH}</string>
    <string>run</string>
    <string>--verbose</string>
  </array>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>WorkingDirectory</key>
  <string>{SCRIPT_PATH.parent}</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
    <key>PATH</key>
    <string>{path_env}</string>
    <key>HOME</key>
    <string>{Path.home()}</string>
  </dict>

  <key>StandardOutPath</key>
  <string>{Path.home()}/Library/Logs/babelclaw-daemon.log</string>
  <key>StandardErrorPath</key>
  <string>{Path.home()}/Library/Logs/babelclaw-daemon.err.log</string>
</dict>
</plist>
"""


def cmd_install(_args: argparse.Namespace) -> None:
    cfg = load_json(CONFIG_PATH, default_config())

    uv_bin, openclaw_bin = validate_install_environment()

    print("BabelClaw daemon onboarding")
    print("-------------------------------------------")
    print(f"Detected uv: {uv_bin}")
    print(f"Detected openclaw: {openclaw_bin}")

    token = input("Beeper access token (required): ").strip()
    if not token:
        raise RuntimeError("A Beeper access token is required.")

    model = choose_model_interactive(default_model=cfg.get("lmstudio_model"))

    target = input(f"Discord target [default {cfg.get('discord_target')}]: ").strip() or cfg.get("discord_target")
    interval_raw = input(f"Polling interval seconds [default {cfg.get('interval_seconds')}]: ").strip()

    from_language = input(f"Translate FROM language [default {cfg.get('from_language')}]: ").strip() or cfg.get("from_language")
    to_language = input(f"Translate TO language [default {cfg.get('to_language')}]: ").strip() or cfg.get("to_language")
    regional_context = input(f"Regional context hint [default {cfg.get('regional_context')}]: ").strip() or cfg.get("regional_context")

    ignore_muted_default = "y" if cfg.get("ignore_muted_chats") else "n"
    ignore_muted_raw = input(f"Ignore muted chats? [Y/n, default {ignore_muted_default.upper()}]: ").strip().lower()
    ignore_muted = cfg.get("ignore_muted_chats") if not ignore_muted_raw else (ignore_muted_raw in {"y", "yes"})

    ignored_networks_default = ",".join(cfg.get("ignored_networks"))
    ignored_networks_raw = input(
        f"Ignored platforms/networks (comma-separated) [default {ignored_networks_default}]: "
    ).strip()
    ignored_networks = [x.strip().lower() for x in (ignored_networks_raw or ignored_networks_default).split(",") if x.strip()]

    ignored_chat_ids_raw = input("Ignored Beeper chat IDs (comma-separated, optional): ").strip()
    ignored_chat_ids = [x.strip() for x in ignored_chat_ids_raw.split(",") if x.strip()]

    ignored_titles_raw = input("Ignored chat title contains (comma-separated, optional): ").strip()
    ignored_titles = [x.strip() for x in ignored_titles_raw.split(",") if x.strip()]

    log_ignored_default = "y" if cfg.get("log_ignored_messages") else "n"
    log_ignored_raw = input(f"Log ignored/non-matching messages? [y/N, default {log_ignored_default.upper()}]: ").strip().lower()
    log_ignored = cfg.get("log_ignored_messages") if not log_ignored_raw else (log_ignored_raw in {"y", "yes"})

    log_skips_default = "y" if cfg.get("log_chat_skips") else "n"
    log_skips_raw = input(f"Log skipped/unreadable chats? [y/N, default {log_skips_default.upper()}]: ").strip().lower()
    log_skips = cfg.get("log_chat_skips") if not log_skips_raw else (log_skips_raw in {"y", "yes"})

    cfg["beeper_access_token"] = token
    cfg["lmstudio_model"] = model
    cfg["discord_target"] = target
    cfg["from_language"] = from_language
    cfg["to_language"] = to_language
    cfg["regional_context"] = regional_context
    cfg["ignore_muted_chats"] = ignore_muted
    cfg["ignored_networks"] = ignored_networks
    cfg["ignored_chat_ids"] = ignored_chat_ids
    cfg["ignored_chat_title_contains"] = ignored_titles
    cfg["log_ignored_messages"] = log_ignored
    cfg["log_chat_skips"] = log_skips
    cfg["openclaw_bin"] = openclaw_bin
    if interval_raw:
        cfg["interval_seconds"] = max(2, int(interval_raw))

    save_json(CONFIG_PATH, cfg)
    print(f"\nSaved config: {CONFIG_PATH}")

    # Reset state so first daemon run auto-seeds and does not spam historical messages.
    save_json(STATE_PATH, {"seen": {}, "bootstrapped": False, "updated_at": now_iso()})
    print(f"Reset state for safe first run: {STATE_PATH}")

    if sys.platform == "darwin":
        plist = build_launchd_plist(interval_seconds=int(cfg.get("interval_seconds")))
        PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        PLIST_PATH.write_text(plist)
        print(f"Wrote launchd plist: {PLIST_PATH}")

        subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)
        load = subprocess.run(["launchctl", "load", str(PLIST_PATH)], capture_output=True, text=True)
        if load.returncode == 0:
            print("launchd service loaded and started.")
        else:
            print(f"launchd load warning: {load.stderr.strip() or load.stdout.strip()}")
    else:
        print("Non-macOS detected: skipping launchd setup.")
        print(f"Run manually: uv run --script {SCRIPT_PATH} run --verbose")


def apply_runtime_path_fallbacks(cfg: dict[str, Any], verbose: bool = False) -> dict[str, Any]:
    resolved_openclaw = resolve_openclaw_bin(cfg)
    configured = str(cfg.get("openclaw_bin") or "").strip()
    if not resolved_openclaw:
        raise RuntimeError("OpenClaw CLI binary not found. Set openclaw_bin in config or add `openclaw` to PATH.")
    if configured != resolved_openclaw and verbose:
        print(f"[warn] openclaw_bin invalid/missing ({configured or 'unset'}), using: {resolved_openclaw}")
    cfg["openclaw_bin"] = resolved_openclaw
    cfg["search_limit"] = max(1, min(20, int(cfg.get("search_limit", 20))))
    return cfg


def cmd_run(args: argparse.Namespace) -> None:
    cfg = apply_runtime_path_fallbacks(load_config(), verbose=args.verbose)
    normal_interval = max(2, int(args.interval) if args.interval is not None else int(cfg.get("interval_seconds")))
    adaptive = bool(cfg.get("adaptive_polling", True))
    active_interval = max(2, min(normal_interval, int(cfg.get("active_interval_seconds", 3))))
    active_window_s = max(0, int(cfg.get("active_window_seconds", 60)))
    medium_idle_after_s = max(active_window_s, int(cfg.get("medium_idle_after_seconds", 300)))
    idle_interval = max(normal_interval, int(cfg.get("idle_interval_seconds", 20)))
    max_idle_after_s = max(medium_idle_after_s, int(cfg.get("max_idle_after_seconds", 1800)))
    max_interval = max(idle_interval, int(cfg.get("max_interval_seconds", 60)))
    last_activity_epoch: int | None = None
    current_sleep = normal_interval

    while True:
        try:
            stats = process_once(cfg, verbose=args.verbose, force_seed_only=args.seed_seen, trace=args.trace)
            if int(stats.get("activity_count", 0)) > 0:
                last_activity_epoch = now_epoch()
        except Exception as e:
            print(f"[error] loop failure: {e}")
            stats = {"translated_count": 0, "activity_count": 0, "seeded": False}

        next_sleep = normal_interval
        if adaptive and not bool(stats.get("seeded", False)) and last_activity_epoch is not None:
            idle_for = max(0, now_epoch() - last_activity_epoch)
            if idle_for <= active_window_s:
                next_sleep = active_interval
            elif idle_for <= medium_idle_after_s:
                next_sleep = normal_interval
            elif idle_for <= max_idle_after_s:
                next_sleep = idle_interval
            else:
                next_sleep = max_interval

        if args.verbose and next_sleep != current_sleep:
            print(
                f"[info] Poll interval now {next_sleep}s"
                f" (activity={int(stats.get('activity_count', 0))}, translated={int(stats.get('translated_count', 0))})"
            )
        current_sleep = next_sleep
        time.sleep(current_sleep)


def cmd_once(args: argparse.Namespace) -> None:
    cfg = apply_runtime_path_fallbacks(load_config(), verbose=args.verbose)
    process_once(cfg, verbose=args.verbose, force_seed_only=args.seed_seen, trace=args.trace)


def cmd_restart(_args: argparse.Namespace) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("restart is only supported on macOS launchd.")

    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{LAUNCHD_LABEL}"
    proc = subprocess.run(["launchctl", "kickstart", "-k", target], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"failed to restart {target}")
    print(f"Restarted launchd service: {target}")


def cmd_doctor(_args: argparse.Namespace) -> None:
    print("BabelClaw doctor")
    print("----------------")

    uv_bin = shutil.which("uv")
    openclaw_bin = shutil.which("openclaw")

    print(f"uv: {'OK' if uv_bin else 'MISSING'} {uv_bin or ''}")
    print(f"openclaw (PATH): {'OK' if openclaw_bin else 'MISSING'} {openclaw_bin or ''}")

    cfg_exists = CONFIG_PATH.exists()
    print(f"config: {'OK' if cfg_exists else 'MISSING'} {CONFIG_PATH}")

    if cfg_exists:
        cfg = load_json(CONFIG_PATH, default_config())
        configured_openclaw = cfg.get("openclaw_bin")
        print(f"openclaw_bin (config): {configured_openclaw}")
        resolved = resolve_openclaw_bin(cfg)
        print(f"openclaw_bin (resolved): {resolved or 'MISSING'}")

        if resolved:
            dry = subprocess.run(
                [
                    resolved,
                    "message",
                    "send",
                    "--channel",
                    "discord",
                    "--target",
                    str(cfg.get("discord_target") or "#cassiel"),
                    "--message",
                    "BabelClaw doctor dry-run",
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
            )
            print(f"openclaw send dry-run: {'OK' if dry.returncode == 0 else 'FAIL'}")
            if dry.returncode != 0:
                print((dry.stderr or dry.stdout).strip())


def cmd_uninstall(args: argparse.Namespace) -> None:
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)
        if PLIST_PATH.exists():
            PLIST_PATH.unlink()
            print(f"Removed plist: {PLIST_PATH}")
        else:
            print(f"Plist not found (already removed): {PLIST_PATH}")
    else:
        print("Non-macOS detected: no launchd plist to remove.")

    if args.delete_config and CONFIG_PATH.exists():
        CONFIG_PATH.unlink()
        print(f"Removed config: {CONFIG_PATH}")

    if args.delete_state and STATE_PATH.exists():
        STATE_PATH.unlink()
        print(f"Removed state: {STATE_PATH}")

    print("Uninstall complete.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BabelClaw language bridge daemon")
    sub = parser.add_subparsers(dest="command", required=True)

    p_install = sub.add_parser("install", help="Interactive onboarding + launchd install")
    p_install.set_defaults(func=cmd_install)

    for name, help_text, fn in (
        ("run", "Run daemon loop", cmd_run),
        ("once", "Run one pass and exit", cmd_once),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--verbose", action="store_true", help="Verbose logs")
        p.add_argument("--trace", action="store_true", help="Trace per-message decision path")
        p.add_argument("--seed-seen", action="store_true", help="Force seed mode (mark seen, send nothing)")
        p.add_argument("--interval", type=int, default=None, help="Loop interval seconds (run only)") if name == "run" else None
        p.set_defaults(func=fn)

    p_doctor = sub.add_parser("doctor", help="Validate binary paths and OpenClaw dry-run")
    p_doctor.set_defaults(func=cmd_doctor)

    p_uninstall = sub.add_parser("uninstall", help="Unload launchd plist and optionally remove files")
    p_uninstall.add_argument("--delete-config", action="store_true", help="Also delete config file")
    p_uninstall.add_argument("--delete-state", action="store_true", help="Also delete dedupe state file")
    p_uninstall.set_defaults(func=cmd_uninstall)

    p_restart = sub.add_parser("restart", help="Restart launchd service via launchctl kickstart -k")
    p_restart.set_defaults(func=cmd_restart)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
