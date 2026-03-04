# BabelClaw

Inbound foreign language messages get a followup auto-translation message sent to you, using Beeper Desktop and LM Studio.

BabelClaw is a language-aware Beeper bridge that uses local LM Studio inference to translate inbound messages and forward translated output to Discord through OpenClaw.

## What it does

- Watches Beeper Desktop API for new inbound messages
- Filters chats with guardrails (muted chats, ignored networks, ignored chat IDs/title patterns)
- Uses fail-safe muted filtering: excludes muted chats at API query time and drops messages when chat metadata cannot be resolved (configurable)
- Classifies/translates with LM Studio (configurable FROM/TO language)
- Applies local English-vs-Spanish heuristics before LLM calls to reduce false-positive translations
- Sends translated messages to Discord via `openclaw message send`
- Uses 24h dedupe state to avoid duplicates
- Runs as a macOS launchd daemon

## Requirements

- [uv](https://docs.astral.sh/uv/)
- Beeper Desktop with Desktop API enabled
- LM Studio local server (OpenAI-compatible API)
- OpenClaw CLI configured with Discord access
- macOS (only required if you want launchd/LaunchAgent auto-start)

## Install / Onboard

```bash
uv run --script babelclaw-daemon.py install
```

This will:
- prompt for Beeper token
- prompt for LM Studio model (uses `lms` if available)
- write config to `~/.config/babelclaw-daemon/config.json`
- reset state for safe first run
- on macOS: write/load `~/Library/LaunchAgents/com.mattwiebe.babelclaw-daemon.plist`
- on non-macOS: skip launchd and print the `uv run --script ... run --verbose` command to start manually

## Manual run

```bash
uv run --script babelclaw-daemon.py run --verbose
```

Trace decisions in detail:

```bash
uv run --script babelclaw-daemon.py run --verbose --trace
```

One pass only:

```bash
uv run --script babelclaw-daemon.py once --verbose
```

Restart launchd service (macOS):

```bash
uv run --script babelclaw-daemon.py restart
```

## Uninstall

```bash
uv run --script babelclaw-daemon.py uninstall
```

Optionally remove local files too:

```bash
uv run --script babelclaw-daemon.py uninstall --delete-config --delete-state
```

## License

MIT
