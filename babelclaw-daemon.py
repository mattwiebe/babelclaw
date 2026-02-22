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
  uninstall  Unload launchd plist + optional config/state cleanup
"""

from __future__ import annotations

import argparse
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
SCRIPT_PATH = Path(__file__).resolve()


def build_system_prompt(from_language: str, to_language: str, regional_context: str) -> str:
    return f"""You are a strict classifier + translator for chat messages.
Task:
1) Detect if the message is MOSTLY {from_language} ({regional_context} context preferred).
2) If mostly {from_language}: output one line exactly: TRANSLATED: <natural {to_language} translation>
3) If not mostly {from_language} (other language, mixed with only tiny {from_language} greeting, unclear, emoji-only, no text): output exactly: IGNORE

Rules:
- Ignore tiny greetings (e.g. a brief hello) when the rest is not {from_language}; output IGNORE.
- Do not add extra text, quotes, thinking traces, JSON, or explanations.
- Keep translation concise and faithful.
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


def default_config() -> dict[str, Any]:
    return {
        "beeper_access_token": "",
        "lmstudio_base_url": "http://127.0.0.1:1234/v1",
        "lmstudio_model": "qwen3-4b-instruct-2507-mlx",
        "discord_target": "#cassiel",
        "interval_seconds": 8,
        "messages_per_chat": 5,
        "send_retries": 3,
        "dedupe_hours": 24,
        "search_limit": 20,
        "openclaw_bin": shutil.which("openclaw") or "openclaw",
        "ignore_muted_chats": True,
        "ignored_networks": ["discord"],
        "ignored_chat_ids": [],
        "ignored_chat_title_contains": [],
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
    cfg["lmstudio_base_url"] = subprocess.os.environ.get("LMSTUDIO_BASE_URL") or cfg.get("lmstudio_base_url")
    cfg["lmstudio_model"] = subprocess.os.environ.get("LMSTUDIO_MODEL") or cfg.get("lmstudio_model")
    cfg["discord_target"] = subprocess.os.environ.get("DISCORD_TARGET") or cfg.get("discord_target")

    if not cfg.get("beeper_access_token"):
        raise RuntimeError(
            f"Missing beeper_access_token. Run install: uv run {SCRIPT_PATH} install"
        )

    return cfg


def prune_seen(seen: dict[str, dict[str, int]], max_age_hours: int) -> dict[str, dict[str, int]]:
    cutoff = now_epoch() - (max_age_hours * 3600)
    return {k: v for k, v in seen.items() if v.get("first_seen_epoch", 0) >= cutoff}


def beeper_client(token: str):
    from beeper_desktop_api import BeeperDesktop

    return BeeperDesktop(access_token=token)


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


def process_once(cfg: dict[str, Any], verbose: bool = False, force_seed_only: bool = False) -> int:
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
        return 0

    client = beeper_client(cfg["beeper_access_token"])
    translated_count = 0

    ignored_networks = {str(x).strip().lower() for x in cfg.get("ignored_networks", []) if str(x).strip()}
    ignored_chat_ids = {str(x).strip() for x in cfg.get("ignored_chat_ids", []) if str(x).strip()}
    ignored_title_contains = [str(x).strip().lower() for x in cfg.get("ignored_chat_title_contains", []) if str(x).strip()]

    try:
        chats = list(client.chats.list())
    except Exception as e:
        if verbose and cfg.get("log_chat_skips", False):
            print(f"[warn] Could not list chats: {e}")
        chats = []

    chat_by_id = {str(getattr(c, "id", "")): c for c in chats}

    date_after = state.get("last_poll_at") or poll_started_at
    limit = max(1, min(20, int(cfg.get("search_limit", 20))))

    try:
        recent_messages = list(
            client.messages.search(
                date_after=date_after,
                include_muted=True,
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

    for msg in recent_messages:
        if getattr(msg, "is_sender", False):
            continue

        key = message_key(msg)
        if key in seen:
            continue

        chat_id = str(getattr(msg, "chat_id", "") or "")
        chat = chat_by_id.get(chat_id)
        chat_network = str(getattr(chat, "network", "") or "").lower() if chat else ""
        chat_title = str(getattr(chat, "title", "") or "") if chat else chat_id
        chat_muted = bool(getattr(chat, "is_muted", False) or getattr(chat, "muted", False)) if chat else False

        if cfg.get("ignore_muted_chats", True) and chat_muted:
            continue
        if chat_network and chat_network in ignored_networks:
            continue
        if chat_id and chat_id in ignored_chat_ids:
            continue
        if chat_title and any(snippet in chat_title.lower() for snippet in ignored_title_contains):
            continue

        text = (getattr(msg, "text", "") or "").strip()
        if not text:
            seen[key] = {"first_seen_epoch": now_epoch()}
            continue

        seen[key] = {"first_seen_epoch": now_epoch()}
        sender = getattr(msg, "sender_name", None) or getattr(msg, "sender_id", "unknown")

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
            continue

        if result == "IGNORE":
            if verbose and cfg.get("log_ignored_messages", False):
                print(f"[skip] IGNORE: {sender}: {text[:80]}")
            continue

        if result.startswith("TRANSLATED:"):
            translation = result.split("TRANSLATED:", 1)[1].strip()
            out = f"{cfg.get('output_flag', '🇲🇽')} {translation} — {sender}"
            try:
                send_to_discord(
                    cfg.get("openclaw_bin") or shutil.which("openclaw") or "openclaw",
                    cfg["discord_target"],
                    out,
                    retries=int(cfg.get("send_retries", 3)),
                )
                translated_count += 1
                if verbose:
                    print(f"[sent] {out}")
            except Exception as e:
                print(f"[error] Discord send failed for {key}: {e}")
            continue

        if verbose:
            print(f"[skip] Unrecognized model output: {result!r}")

    state["seen"] = seen
    state["bootstrapped"] = True
    state["last_poll_at"] = poll_started_at
    state["updated_at"] = now_iso()
    save_json(STATE_PATH, state)
    return translated_count


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

    print("BabelClaw daemon onboarding")
    print("-------------------------------------------")

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
    cfg["openclaw_bin"] = cfg.get("openclaw_bin") or shutil.which("openclaw") or "openclaw"
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


def cmd_run(args: argparse.Namespace) -> None:
    cfg = load_config()
    interval = int(args.interval) if args.interval is not None else int(cfg.get("interval_seconds"))

    while True:
        try:
            process_once(cfg, verbose=args.verbose, force_seed_only=args.seed_seen)
        except Exception as e:
            print(f"[error] loop failure: {e}")
        time.sleep(max(2, interval))


def cmd_once(args: argparse.Namespace) -> None:
    cfg = load_config()
    process_once(cfg, verbose=args.verbose, force_seed_only=args.seed_seen)


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
        p.add_argument("--seed-seen", action="store_true", help="Force seed mode (mark seen, send nothing)")
        p.add_argument("--interval", type=int, default=None, help="Loop interval seconds (run only)") if name == "run" else None
        p.set_defaults(func=fn)

    p_uninstall = sub.add_parser("uninstall", help="Unload launchd plist and optionally remove files")
    p_uninstall.add_argument("--delete-config", action="store_true", help="Also delete config file")
    p_uninstall.add_argument("--delete-state", action="store_true", help="Also delete dedupe state file")
    p_uninstall.set_defaults(func=cmd_uninstall)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
