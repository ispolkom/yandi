#!/usr/bin/env python3
"""
council.py — manage AI dialogue sessions in registry/council/

Usage:
  python council/council.py new "topic"              # create new session
  python council/council.py reply claude 001 "text"  # add reply to session 001
  python council/council.py reply gpt 001 "text"
  python council/council.py consensus 001 "agreed: ..."
  python council/council.py list                     # show all sessions
  python council/council.py read 001                 # read session
"""

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

SESSIONS_DIR = Path(__file__).parent.parent / "registry" / "council" / "sessions"
INDEX_FILE = Path(__file__).parent.parent / "registry" / "council" / "COUNCIL_INDEX.md"


def next_session_id() -> str:
    existing = sorted(SESSIONS_DIR.glob("*.md"))
    if not existing:
        return "001"
    last = existing[-1].name
    m = re.match(r"(\d+)", last)
    return f"{int(m.group(1)) + 1:03d}" if m else "001"


def cmd_new(topic: str):
    sid = next_session_id()
    date = datetime.now().strftime("%Y-%m-%d")
    slug = re.sub(r"[^\w]", "_", topic.lower())[:40]
    fname = f"{sid}_{slug}.md"
    path = SESSIONS_DIR / fname
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    path.write_text(f"""---
session: {sid}
date: {date}
topic: {topic}
participants: [claude-sonnet-4-6, gpt-5.5]
status: open
consensus: ""
---

## Тема

{topic}

---

## Консенсус

*(ожидается)*
""")
    _update_index(sid, fname, topic, "open")
    print(f"[council] Created: {fname}")
    print(f"[council] Add reply: python council/council.py reply claude {sid} \"your message\"")


def cmd_reply(who: str, sid: str, text: str):
    path = _find_session(sid)
    if not path:
        print(f"[council] Session {sid} not found", file=sys.stderr)
        sys.exit(1)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    content = path.read_text()

    # Insert before ## Консенсус
    reply_block = f"\n## [{who}] {ts}\n\n{text}\n"
    content = content.replace("\n## Консенсус", f"{reply_block}\n## Консенсус")
    path.write_text(content)

    # Redis notification (optional, graceful fail)
    try:
        import redis
        opponent = "gpt" if who == "claude" else "claude"
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
        r.set("council:turn", opponent)
        r.set("council:latest", str(path))
        print(f"[council] Redis: turn → {opponent}")
    except Exception:
        pass

    print(f"[council] Reply added by {who} to session {sid}")
    print(f"[council] File: {path.name}")


def cmd_consensus(sid: str, text: str):
    path = _find_session(sid)
    if not path:
        print(f"[council] Session {sid} not found", file=sys.stderr)
        sys.exit(1)

    content = path.read_text()
    content = re.sub(r'consensus: ""', f'consensus: "{text}"', content)
    content = re.sub(r'status: open', 'status: consensus', content)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    content = content.replace(
        "## Консенсус\n\n*(ожидается)*",
        f"## Консенсус\n\n**{ts}:** {text}"
    )
    path.write_text(content)
    _update_index_status(sid, "consensus")
    print(f"[council] Consensus recorded for session {sid}")


def cmd_list():
    if not SESSIONS_DIR.exists():
        print("No sessions yet.")
        return
    for f in sorted(SESSIONS_DIR.glob("*.md")):
        content = f.read_text()
        m_topic = re.search(r"^topic: (.+)$", content, re.MULTILINE)
        m_status = re.search(r"^status: (.+)$", content, re.MULTILINE)
        topic = m_topic.group(1) if m_topic else "?"
        status = m_status.group(1) if m_status else "?"
        replies = len(re.findall(r"^## \[(claude|gpt)", content, re.MULTILINE))
        print(f"  {f.stem[:50]:<50} [{status}] {replies} replies")


def cmd_read(sid: str):
    path = _find_session(sid)
    if not path:
        print(f"Session {sid} not found")
        return
    print(path.read_text())


def _find_session(sid: str) -> Path | None:
    if not SESSIONS_DIR.exists():
        return None
    for f in SESSIONS_DIR.glob(f"{sid}*.md"):
        return f
    return None


def _update_index(sid: str, fname: str, topic: str, status: str):
    if not INDEX_FILE.exists():
        return
    content = INDEX_FILE.read_text()
    entry = f"| [sessions/{fname}](sessions/{fname}) | {topic[:40]} | claude + gpt-5.5 | {status} |\n"
    content = content.replace("| *(пусто)*\n\n## Закрытые", entry + "\n## Закрытые")
    INDEX_FILE.write_text(content)


def _update_index_status(sid: str, status: str):
    if not INDEX_FILE.exists():
        return
    content = INDEX_FILE.read_text()
    content = re.sub(rf"(\| sessions/{sid}[^|]+\|[^|]+\|[^|]+\|)\s*open",
                     rf"\1 {status}", content)
    INDEX_FILE.write_text(content)


def main():
    parser = argparse.ArgumentParser(description="Council — AI dialogue manager")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("list")

    p_new = sub.add_parser("new")
    p_new.add_argument("topic")

    p_reply = sub.add_parser("reply")
    p_reply.add_argument("who", choices=["claude", "gpt"])
    p_reply.add_argument("session_id")
    p_reply.add_argument("text")

    p_cons = sub.add_parser("consensus")
    p_cons.add_argument("session_id")
    p_cons.add_argument("text")

    p_read = sub.add_parser("read")
    p_read.add_argument("session_id")

    args = parser.parse_args()

    if args.cmd == "new":       cmd_new(args.topic)
    elif args.cmd == "reply":   cmd_reply(args.who, args.session_id, args.text)
    elif args.cmd == "consensus": cmd_consensus(args.session_id, args.text)
    elif args.cmd == "list":    cmd_list()
    elif args.cmd == "read":    cmd_read(args.session_id)
    else:                       parser.print_help()


if __name__ == "__main__":
    main()
