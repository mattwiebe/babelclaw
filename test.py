#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["beeper-desktop-api", "httpx"]
# ///
"""
BabelClaw smoke test — debug translation decisions.

Usage:
  echo "Hello world" | uv run test.py
  uv run test.py "Hello world"
  uv run test.py --chat-id "<beeper-chat-id>"
  uv run test.py "hola gracias" --chat-id "<beeper-chat-id>"
"""

import sys
import argparse
import importlib.util
from typing import Any

# Import actual functions from the daemon (handles hyphenated filename)
from pathlib import Path

daemon_path = Path(__file__).parent / "babelclaw-daemon.py"
spec = importlib.util.spec_from_file_location("babelclaw_daemon", daemon_path)
daemon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daemon)

load_config = daemon.load_config
classify_or_translate = daemon.classify_or_translate
likely_english_with_tiny_spanish_tail = daemon.likely_english_with_tiny_spanish_tail
likely_english_not_spanish = daemon.likely_english_not_spanish
beeper_client = daemon.beeper_client
is_chat_muted = daemon.is_chat_muted
should_accept_translation = daemon.should_accept_translation


def inspect_chat(chat_id: str, cfg: dict[str, Any]) -> None:
    print("\n" + "=" * 60)
    print("Beeper Chat Mute Inspection")
    print("=" * 60)
    print(f"Chat ID: {chat_id}")

    try:
        client = beeper_client(cfg["beeper_access_token"])
        chat = client.chats.retrieve(chat_id)
    except Exception as e:
        print(f"❌ Could not retrieve chat: {e}")
        print("=" * 60)
        return

    title = getattr(chat, "title", "") or "(no title)"
    network = getattr(chat, "network", "") or "(unknown)"
    raw_is_muted = getattr(chat, "is_muted", None)
    raw_muted = getattr(chat, "muted", None)
    raw_is_muted_camel = getattr(chat, "isMuted", None)
    muted = is_chat_muted(chat)

    print(f"Title: {title}")
    print(f"Network: {network}")
    print(f"is_muted: {raw_is_muted}")
    print(f"muted: {raw_muted}")
    print(f"isMuted: {raw_is_muted_camel}")
    print(f"Resolved muted? {'YES' if muted else 'NO'}")
    print("=" * 60)


def test_message(text: str, cfg: dict[str, Any], skip_llm: bool = False) -> None:
    print("=" * 60)
    print("BabelClaw Smoke Test")
    print("=" * 60)
    print(f"\nInput: {text}")
    print(f"Model: {cfg['lmstudio_model']}")
    print(f"From language: {cfg.get('from_language', 'Spanish')}")
    print(f"To language: {cfg.get('to_language', 'English')}")
    print()

    if likely_english_with_tiny_spanish_tail(text, cfg.get("from_language", "Spanish")):
        print("Result: IGNORE")
        print()
        print("✅ Decision: IGNORE (mixed English with tiny Spanish tail heuristic)")
        print("=" * 60)
        return

    if likely_english_not_spanish(text, cfg.get("from_language", "Spanish")):
        print("Result: IGNORE")
        print()
        print("✅ Decision: IGNORE (English-dominance heuristic)")
        print("=" * 60)
        return

    if skip_llm:
        print("Result: SKIPPED")
        print()
        print("ℹ️  LLM classification skipped by --skip-llm")
        print("=" * 60)
        return

    try:
        result = classify_or_translate(
            cfg["lmstudio_base_url"],
            cfg["lmstudio_model"],
            text,
            cfg.get("from_language", "Spanish"),
            cfg.get("to_language", "English"),
            cfg.get("regional_context", "Mexican/LatAm"),
        )
        
        print(f"Result: {result}")
        print()
        
        if result == "IGNORE":
            print("✅ Decision: IGNORE (not translated)")
        elif result.startswith("TRANSLATED:"):
            translation = result.split("TRANSLATED:", 1)[1].strip()
            if not should_accept_translation(text, translation, cfg.get("from_language", "Spanish")):
                print("✅ Decision: IGNORE (suspicious no-op translation)")
                print(f"   Model output looked like non-translation: {translation}")
                print("=" * 60)
                return
            print("🔴 Decision: TRANSLATE")
            print(f"   Translation: {translation}")
        else:
            print("⚠️  Unexpected output format")
    
    except Exception as e:
        print(f"❌ Error: {e}")

    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="Test BabelClaw translation decisions")
    parser.add_argument("text", nargs="?", help="Text to test (reads from stdin if not provided)")
    parser.add_argument("--chat-id", help="Optional Beeper chat ID to inspect mute metadata")
    parser.add_argument("--skip-llm", action="store_true", help="Run only local heuristics, skip LM Studio call")

    args = parser.parse_args()

    cfg = load_config()

    if args.chat_id:
        inspect_chat(args.chat_id, cfg)

    text = args.text
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()

    if text:
        test_message(text, cfg, skip_llm=args.skip_llm)
        return

    if not args.chat_id:
        print("Error: Provide text (arg/stdin) and/or --chat-id.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
