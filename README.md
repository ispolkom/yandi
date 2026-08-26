# YANDI

P2P network node + multi-model AI council + local AI assistant.

## Structure

```
yandi/
├── node/       Rust P2P network node (QUIC, DHT, onion routing)
├── pet/        AI council chat server (FastAPI + WebSocket)
│   └── extension/  Firefox browser extension (bridges AI chat tabs)
├── agent/      AI orchestrator, validator, knowledge tools
└── registry/   Local data (not tracked in git)
```

## Quick start

### Requirements

- Python 3.10+
- Redis
- Ollama (for local AI — optional)
- Firefox with browser extension loaded from `pet/extension/`

### Install

```bash
pip install -r requirements.txt
```

### Run

```bash
./start.sh          # default port 9010
./start.sh 8080     # custom port
```

Open: http://127.0.0.1:9010

### Browser extension

1. Firefox → `about:debugging` → This Firefox → Load Temporary Add-on
2. Select `pet/extension/manifest.json`
3. Open tabs: claude.ai, chatgpt.com, chat.deepseek.com, kimi.com, chat.qwen.ai

## Modes

| Tab | Description |
|-----|-------------|
| Оркестратор | Local AI answers with web search + validation |
| Интернет чат | Multi-model relay: Claude → GPT → DeepSeek → Kimi → Qwen |
| YANDI Помощник | Private chat with local Ollama model (not saved) |

## Node (Rust)

```bash
cd node
cargo build --release
./target/release/yandi-node
```

## Environment

No `.env` needed. All config via `pet/council_config.json` (created on first run).
