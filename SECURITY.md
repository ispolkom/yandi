# Security policy

## Project status

YANDI is experimental and has not been independently audited. Do not rely on it to protect
sensitive data, and do not expose its servers to untrusted networks.

## Reporting a vulnerability

- If the repository's **Security** tab offers **Report a vulnerability**, use it. That keeps the
  report private.
- Otherwise open a GitHub issue that says you have a security report and ask for a private way to
  send details. **Do not put exploit details, credentials, or personal data in a public issue.**

Please include what is affected, how to reproduce it, and the impact you expect. Reports are handled
on a best-effort basis; there is no service-level commitment.

## Do not publish secrets or private data

Never commit or attach to issues: API keys, tokens, cookies, private keys, database or proxy
credentials, conversation history, memory or database dumps, logs from a real installation, or
model weights. If you find a secret in the repository or its history, report it privately and treat
it as compromised: it must be rotated, because deleting it from the latest commit does not remove
it from Git history.

## Deployment notes

- The personal chat server (`pet/`) binds to `127.0.0.1` by default. Keep it that way unless you
  add authentication and transport security in front of it.
- The intelligence bridge (`llm_gateway/intelligence_bridge.py`) is loopback-only by design.
- Remote-model API keys are referenced by environment-variable name in the config store; supply the
  key through the environment, not through files in the repository.
- Local runtime state (`registry/`, episode exports, `pet/council_config.json`, `proxy.txt`) is
  ignored by Git on purpose. Keep it that way.
