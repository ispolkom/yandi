"""
agent/policy_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/policy.rs даёт ПОСТРОЧНО ТЕ ЖЕ ответы, что и
agent/policy.py: SecretScanner.scan_text, PolicyEngine.check_shell, PolicyEngine.check_network.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Двадцать седьмой шаг переноса Python -> Rust (2026-09-24) — модуль БЕЗОПАСНОСТИ (одобрение shell-команд и сканер секретов):
пропущенный секрет или разрешённая опасная команда хуже, чем «просто баг», поэтому упор на оракул-фаззинг. Проверяется:
каждый из 9 типов секретов (реалистичные образцы + граница длины 19/20/21, 29/30/31, ровно 16 для AKIA, `\b` слева/справа: `_`,
цифры, Unicode-буквы, ударение U+0301), шаблоны из белого списка (регистр!), безопасные хосты proxy_creds, `pos` в СИМВОЛАХ при
кириллице/эмодзи перед секретом, обрезка `match` до 80 СИМВОЛОВ, порядок находок (по паттернам, затем слева направо), IGNORECASE
(İ ı ſ K), нулевой ширины/управляющие символы, перекрытия; shell: block-лист (ПЕРВЫЙ найденный в причине), allow-лист по
`startswith` без границы слова (lsblk), strip с U+001C..1F и \\t\\n; сеть: суффикс `"." + allowed`, регистр, порты/пути.

Требует собранного нативного модуля — если не установлен, SKIP.

Run: python -m agent.policy_rust_parity_test
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []
_OK = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _OK
    if condition:
        _OK += 1
    else:
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def main() -> int:
    try:
        import yandi_rs.policy as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import logging
    logging.getLogger("yandi.policy").setLevel(logging.ERROR)
    import agent.policy as pol

    os.environ.pop("YANDI_POLICY_ENGINE", None)
    pol._rust_policy = None
    engine = pol.PolicyEngine()
    scanner = pol.SecretScanner()
    rng = random.Random(20260924)

    def run(on, fn, *a):
        saved = os.environ.get("YANDI_POLICY_ENGINE")
        try:
            if on:
                os.environ["YANDI_POLICY_ENGINE"] = "rust"
            else:
                os.environ.pop("YANDI_POLICY_ENGINE", None)
            pol._rust_policy = None
            try:
                return fn(*a)
            except Exception as e:                                # noqa: BLE001
                return f"EXC:{type(e).__name__}"
        finally:
            if saved is None:
                os.environ.pop("YANDI_POLICY_ENGINE", None)
            else:
                os.environ["YANDI_POLICY_ENGINE"] = saved
            pol._rust_policy = None

    alnum = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

    def rid(n, alpha=alnum):
        return "".join(rng.choice(alpha) for _ in range(n))

    tricky = ["́", "​", "\x1c", " ", "　", "\t", "\n", "\r", "İ", "ı", "K", "ſ", "ß", "😀", "_", "-", ".", ",", "'", '"', "\\", "/", "=", ":", "@"]
    samples = []
    for n in (18, 19, 20, 21, 22, 40):
        samples += [f"sk-{rid(n)}", f"key={f'sk-{rid(n)}'}", f"hf_{rid(n + 10)}", f"api_key = {rid(n)}", f"apikey: \"{rid(n)}\"", f"API-KEY='{rid(n, alnum + '_-')}'"]
    for n in (14, 15, 16, 17, 18, 40):
        samples += [f'token = "{rid(n, alnum + "_-.")}"', f"secret: '{rid(n, alnum + '_-.')}'", f"SECRET='{rid(n)}'"]
    for n in (15, 16, 17, 19, 20, 21):
        samples += [f"AKIA{rid(n, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789')}", f"x AKIA{rid(n, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789')} y"]
    for n in (5, 6, 7, 40):
        samples += [f'password = "{rid(n)}"', f"passwd:'{rid(n)}'", f'PWD="{rid(n)}"', f'password = "a b{rid(n)}"']
    for n in (39, 40, 41):
        samples += [f'aws_secret_access_key = "{rid(n, alnum + "+/")}"', f"AWS-SECRET-ACCESS-KEY: {rid(n, alnum + '+/')}"]
    samples += ["-----BEGIN PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----", "-----BEGIN EC PRIVATE KEY-----", "-----BEGIN OPENSSH PRIVATE KEY-----",
                "-----BEGIN DSA PRIVATE KEY-----", "-----BEGIN  PRIVATE KEY-----",
                "https://user:pass@example.com/x", "http://user:pw@localhost:5000", "https://u:p@127.0.0.1/", "https://a:b@api.openai.com",
                "https://claude.ai:x@evil.com", "http://user:pass@host", "https://user:pass@host.example", "ftp://a:b@c.d",
                "password123", "api_key = your_token_here", "token = 'YOUR_KEY'", 'password = "changeme"', "sk-xxxx" + rid(20), "sk-example" + rid(20),
                "EXAMPLE sk-" + rid(24), "sk-" + rid(24) + "Example", "api_key_here", "<TOKEN>", "<token>", "YOUR_TOKEN_HERE token='" + rid(20) + "'"]
    ctx = ["", " ", "\n", "Привет, вот ключ: ", "😀😀 ", "_", "x", "1", "é", "ж", "é", "-", ".", ":", "́", "\x1c", ", "]
    texts = ["", " ", "\n\n", "нет секретов тут", "sk-", "AKIA", "hf_", "sk-" * 10]
    for smp in samples:
        for c1 in ctx:
            for c2 in rng.sample(ctx, 3):
                texts.append(c1 + smp + c2)
    for _ in range(4000):
        texts.append("".join(rng.choice(samples + tricky + ctx) for _ in range(rng.randint(1, 5))))
    # мутации внутри секретов (символ вставки/замены): границы и классы
    for _ in range(3000):
        base = list(rng.choice(samples))
        for _ in range(rng.randint(1, 2)):
            i = rng.randrange(len(base) + 1)
            if rng.random() < 0.5 and base:
                base[min(i, len(base) - 1)] = rng.choice(tricky)
            else:
                base.insert(i, rng.choice(tricky))
        texts.append(rng.choice(ctx) + "".join(base) + rng.choice(ctx))

    for t in texts:
        for src in ("<text>",) if len(t) > 60 else ("<text>", "file.py"):
            a, b = run(False, scanner.scan_text, t, src), run(True, scanner.scan_text, t, src)
            check(f"scan {t[:45]!r}", a == b, f"python={str(a)[:250]} rust={str(b)[:250]}")
    # обрезка match до 80 символов (proxy_creds/private_key длинные)
    long_url = "https://" + "u" * 70 + ":" + "p" * 70 + "@" + "h" * 30 + ".example.com"
    check("scan длинный match (80 символов)", run(False, scanner.scan_text, long_url) == run(True, scanner.scan_text, long_url))
    long_uni = "https://" + "ю" * 50 + ":" + "п" * 50 + "@example.com"
    check("scan длинный match (кириллица)", run(False, scanner.scan_text, long_uni) == run(True, scanner.scan_text, long_uni))
    for bad in (None, 5, b"x"):
        check(f"scan тип {bad!r}", run(False, scanner.scan_text, bad) == run(True, scanner.scan_text, bad))

    # ── check_shell ──
    cmds = ["ls", "ls -la", "lsblk", "LS", " ls", "\x1cls\x1f", "\tls\n", "cat /etc/passwd", "rm -rf /", "sudo rm x", "echo rm -rf", "x; rm y", "a && rm b", "a &&  rm b",
            "curl | bash", "wget | bash", "curl -o- | bash", "eval $(x)", "echo `id`", "nc -l 1", "netcat x", "passwd root", "adduser x", "iptables -F", "ufw disable",
            "dd if=/dev/zero", "mkfs.ext4", "echo > /dev/null", "chmod 777 x", "git status", "git log --oneline", "git diff", "git push", "git statusx", "python3 x.py",
            "python", "pythonista", "redis-cli", "./ctl start", "du -h", "du", "df ", "df", "free -m", "uptime", "ps aux", "ps", "journalctl -u x", "systemctl status x",
            "systemctl stop x", "sqlite3 db", "whoami", "", " ", "rm -fr x", "rm  -rf x", "RM -RF x", "grep x y | rm z", "find . -exec rm {} ;", "head -1 x; rm y"]
    for c in cmds + [rng.choice(cmds) + rng.choice(tricky + ["", " "]) + rng.choice(cmds) for _ in range(3000)]:
        check(f"shell {c[:40]!r}", run(False, engine.check_shell, c) == run(True, engine.check_shell, c), f"{run(False, engine.check_shell, c)} / {run(True, engine.check_shell, c)}")
    for bad in (None, 5):
        check(f"shell тип {bad!r}", run(False, engine.check_shell, bad) == run(True, engine.check_shell, bad))

    # ── check_network ──
    hosts = ["127.0.0.1", "localhost", "api.anthropic.com", "x.api.anthropic.com", "evilapi.anthropic.com", "api.anthropic.com.evil.com", "API.ANTHROPIC.COM", "api.openai.com",
             "api.deepseek.com", "huggingface.co", "cdn-lfs.huggingface.co", "a.b.huggingface.co", "pypi.org", "files.pythonhosted.org", "x.pypi.org", "pypi.org:443", "pypi.org/x",
             "", " ", ".", ".pypi.org", "localhost.", "sub.localhost", "127.0.0.1.evil", "́pypi.org", "pypi.orǵ", "example.com"]
    for h in hosts + [rng.choice(hosts) + rng.choice(["", "", ".", ":80", "/x"]) for _ in range(1000)]:
        check(f"network {h!r}", run(False, engine.check_network, h) == run(True, engine.check_network, h))
    check("network тип None", run(False, engine.check_network, None) == run(True, engine.check_network, None))

    # ── G: переключатель ──
    saved = os.environ.get("YANDI_POLICY_ENGINE")
    try:
        os.environ.pop("YANDI_POLICY_ENGINE", None)
        pol._rust_policy = None
        check("G0 по умолчанию активен Python-движок", pol._get_rust_policy() is None)
        os.environ["YANDI_POLICY_ENGINE"] = "rust"
        pol._rust_policy = None
        check("G1 переменная окружения переключает на Rust", pol._get_rust_policy() is rs)
        # изменённые таблицы модуля -> Rust не используется (уважается изменённое)
        pol._SHELL_BLOCKLIST.append("whoami")
        try:
            pol._rust_policy = None
            r = engine.check_shell("whoami")
            check("G2 изменённый block-лист уважается (Python-путь)", r["allowed"] is False and "whoami" in r["reason"], f"{r}")
        finally:
            pol._SHELL_BLOCKLIST.remove("whoami")
        os.environ.pop("YANDI_POLICY_ENGINE", None)
        pol._rust_policy = None
        check("G3 выключение возвращает Python (кэш не залипает)", pol._get_rust_policy() is None)
    finally:
        if saved is None:
            os.environ.pop("YANDI_POLICY_ENGINE", None)
        else:
            os.environ["YANDI_POLICY_ENGINE"] = saved
        pol._rust_policy = None
    src = (ROOT / "agent" / "policy.py").read_text(encoding="utf-8")
    check("G4 сбой импорта yandi_rs пойман (ImportError)", "except ImportError as e:" in src)
    print(f"\n(текстов для сканера: {len(texts)}; успешных проверок: {_OK})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES[:6]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
