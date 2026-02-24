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
"""

import sys
import argparse
import importlib.util

# Import actual functions from the daemon (handles hyphenated filename)
from pathlib import Path

daemon_path = Path(__file__).parent / "babelclaw-daemon.py"
spec = importlib.util.spec_from_file_location("babelclaw_daemon", daemon_path)
daemon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daemon)

load_config = daemon.load_config
classify_or_translate = daemon.classify_or_translate


def test_message(text: str) -> None:
    cfg = load_config()
    
    print("=" * 60)
    print("BabelClaw Smoke Test")
    print("=" * 60)
    print(f"\nInput: {text}")
    print(f"Model: {cfg['lmstudio_model']}")
    print()
    
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
    
    args = parser.parse_args()
    
    # Get text from arg or stdin
    if args.text:
        text = args.text
    else:
        if sys.stdin.isatty():
            print("Error: No text provided. Provide as argument or pipe via stdin.", file=sys.stderr)
            sys.exit(1)
        text = sys.stdin.read().strip()
    
    if not text:
        print("Error: Empty input", file=sys.stderr)
        sys.exit(1)
    
    test_message(text)


if __name__ == "__main__":
    main()
