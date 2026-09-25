"""
agent/state_native_parity_test.py — доказательство, что ВСТРОЕННОЕ хранилище состояния (rustlib/yandi_state, замена внешнему Redis в едином бинарнике) отвечает так же,
как НАСТОЯЩИЙ redis-server, на реально используемом подмножестве команд.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК REDIS, СЕЙЧАС».

Поднимается ПРИВАТНЫЙ redis-server (свободный порт, без сохранения на диск, свой процесс — живой Redis владельца и его ключи НЕ затрагиваются), и одна и та же
последовательность команд подаётся ему и встроенному хранилищу; сравнивается КАЖДЫЙ ответ (байты, целые, nil, массивы, простые строки, тексты ошибок).
Команды: строки (GET/SET со всеми опциями EX/PX/EXAT/PXAT/NX/XX/GET/KEEPTTL, SETEX/PSETEX/SETNX/GETSET/MGET/MSET/APPEND/STRLEN/INCR*/DECR*), срок жизни (EXPIRE/PEXPIRE
с NX/XX/GT/LT, TTL/PTTL/PERSIST), DEL/EXISTS/TYPE/RENAME/DBSIZE/FLUSHDB/KEYS/SCAN, списки (LPUSH/RPUSH/LPUSHX/RPUSHX/LPOP/RPOP со счётчиком/LLEN/LRANGE/LTRIM/LINDEX/LSET/LREM/LINSERT),
множества (SADD/SREM/SCARD/SISMEMBER/SMEMBERS), PING, неизвестные команды и неверное число аргументов, WRONGTYPE между всеми видами; PUBLISH/SUBSCRIBE/PSUBSCRIBE (число получателей и доставленные сообщения).
Случайные последовательности над малым набором ключей (чтобы команды часто «сталкивались») + направленные случаи. Порядок элементов сравнивается там, где Redis его гарантирует (списки,
LRANGE), и как множество там, где не гарантирует (KEYS, SMEMBERS, SCAN). TTL/PTTL — с допуском на время выполнения.

Требует собранного моста yandi_state и redis-server — иначе SKIP.

Run: python -m agent.state_native_parity_test
"""
from __future__ import annotations

import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
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
        if len(FAILURES) < 25:
            print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main() -> int:
    try:
        import yandi_state
        import redis
    except ImportError as e:
        print(f"SKIP: нет yandi_state / redis-py ({e})")
        return 0
    exe = shutil.which("redis-server")
    if not exe:
        print("SKIP: redis-server не найден")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="yandi-redis-parity-"))
    port = free_port()
    proc = subprocess.Popen([exe, "--port", str(port), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no", "--dir", str(tmp), "--daemonize", "no"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        r = redis.Redis(host="127.0.0.1", port=port)
        for _ in range(100):
            try:
                if r.ping():
                    break
            except redis.ConnectionError:
                time.sleep(0.05)
        conn = r.connection_pool.get_connection()

        def real(*argv):
            try:
                conn.send_command(*argv)
                return conn.read_response()
            except redis.ResponseError as e:
                return ("err", str(e))

        def norm_err(t: str) -> str:
            return t[4:] if t.startswith("ERR ") else t

        def norm(x):
            """Приводим ответ Redis и ответ Rust к одному виду."""
            if isinstance(x, tuple) and x and x[0] == "ok":
                return b"OK" if x[1] == "OK" else x[1].encode()
            if isinstance(x, tuple) and x and x[0] == "err":
                return ("err", norm_err(x[1]))
            if isinstance(x, list):
                return [norm(i) for i in x]
            return x

        UNORDERED = {"KEYS", "SMEMBERS"}
        TTLS = {"TTL", "PTTL"}

        def same(cmd: str, a, b) -> bool:
            a, b = norm(a), norm(b)
            if cmd == "SCAN" and isinstance(a, list) and isinstance(b, list) and cur_cursor[0] not in (b"0", b""):
                return True     # ненулевой курсор: раскладка таблицы у Redis — деталь реализации (полный обход допустим), сравниваем только «не ошибка»
            if cmd in UNORDERED and isinstance(a, list) and isinstance(b, list):
                return sorted(a) == sorted(b)
            if cmd == "SCAN" and isinstance(a, list) and isinstance(b, list) and len(a) == 2 and len(b) == 2:
                # полный обход: курсор 0 и то же множество ключей (Redis может вернуть курсор != 0 при большом объёме — здесь ключей мало)
                return a[0] == b[0] == b"0" and sorted(a[1]) == sorted(b[1])
            if cmd in TTLS and isinstance(a, int) and isinstance(b, int):
                if a < 0 or b < 0:
                    return a == b                        # -1 (нет срока) и -2 (нет ключа) — ТОЧНО
                # оставшееся время: допуск только на время выполнения (мс); TTL в секундах округляется как в Redis ((мс+500)/1000) — допуска нет
                return abs(a - b) <= 60 if cmd == "PTTL" else a == b
            return a == b

        cur_cursor = [b"0"]
        store_holder = {"s": yandi_state.PyStore()}
        n = [0]

        def scan_real(argv):
            """SCAN у Redis отдаёт ключи порциями (курсор ≠ 0) — обходим до курсора 0 и собираем всё; встроенное хранилище отдаёт всё за один шаг (это допустимо: COUNT — лишь подсказка)."""
            cursor, keys = argv[1], []
            first = True
            for _ in range(1000):
                rep = real(argv[0], cursor, *argv[2:])
                if isinstance(rep, tuple) or not isinstance(rep, list):
                    return rep
                cursor, ks = rep
                keys.extend(ks)
                if cursor == b"0" or not first and cursor == b"0":
                    break
                first = False
            return [b"0", sorted(set(keys))]

        def both(*argv):
            argv = tuple(x if isinstance(x, bytes) else str(x).encode() for x in argv)
            cur_cursor[0] = argv[1] if argv[0].upper() == b"SCAN" and len(argv) >= 2 else b"0"
            a = scan_real(argv) if argv[0].upper() == b"SCAN" and len(argv) >= 2 else real(*argv)
            b = store_holder["s"].execute(*argv)
            n[0] += 1
            cmd = argv[0].decode("latin-1").upper()
            ok = same(cmd, a, b)
            check(f"{cmd}", ok, f"\n argv={[x[:40] for x in argv]}\n redis={a!r}\n rust ={norm(b)!r}")
            return a

        def reset():
            real("FLUSHALL")
            store_holder["s"] = yandi_state.PyStore()

        # ---- A. направленные случаи ------------------------------------------------------------------------------------------------
        DIRECTED = [
            ("SET", "a", "1"), ("GET", "a"), ("SET", "a", "2", "NX"), ("SET", "b", "2", "NX"), ("SET", "c", "3", "XX"), ("SET", "b", "3", "XX"), ("SET", "b", "x", "GET"), ("SET", "zz", "x", "GET"),
            ("SET", "a", "1", "EX", "100"), ("TTL", "a"), ("SET", "a", "2", "KEEPTTL"), ("TTL", "a"), ("SET", "a", "3"), ("TTL", "a"), ("SET", "a", "1", "EX", "0"), ("SET", "a", "1", "EX", "-1"),
            ("SET", "a", "1", "PX", "100000"), ("PTTL", "a"), ("SET", "a", "1", "EX", "1", "PX", "10"), ("SET", "a", "1", "NX", "XX"), ("SET", "a", "1", "EX"), ("SET", "a", "1", "BOGUS"),
            ("SET", "a", "1", "EX", "abc"), ("SET", "a"), ("SET"), ("GET"), ("GET", "a", "b"), ("SETEX", "s", "100", "v"), ("SETEX", "s", "0", "v"), ("SETEX", "s", "x", "v"), ("PSETEX", "s", "100000", "v"),
            ("SETNX", "s", "1"), ("SETNX", "s2", "1"), ("GETSET", "s2", "9"), ("GETSET", "nokey", "9"), ("MSET", "m1", "1", "m2", "2"), ("MSET", "m1"), ("MGET", "m1", "m2", "nokey"), ("MGET"),
            ("APPEND", "ap", "x"), ("APPEND", "ap", "yz"), ("STRLEN", "ap"), ("STRLEN", "none"),
            ("SET", "n", "10"), ("INCR", "n"), ("DECR", "n"), ("INCRBY", "n", "5"), ("DECRBY", "n", "20"), ("INCRBY", "n", "x"), ("INCR", "ap"), ("INCR", "fresh"), ("SET", "big", "9223372036854775807"),
            ("INCR", "big"), ("DECR", "big"), ("SET", "small", "-9223372036854775808"), ("DECR", "small"), ("DECRBY", "small", "-9223372036854775808"), ("INCRBY", "n", "9223372036854775807"),
            ("SET", "sp", " 1"), ("INCR", "sp"), ("SET", "lz", "01"), ("INCR", "lz"), ("SET", "pl", "+1"), ("INCR", "pl"), ("SET", "neg0", "-0"), ("INCR", "neg0"),
            ("EXPIRE", "n", "100"), ("TTL", "n"), ("EXPIRE", "n", "100", "NX"), ("EXPIRE", "n", "200", "XX"), ("EXPIRE", "n", "300", "GT"), ("EXPIRE", "n", "10", "GT"), ("EXPIRE", "n", "10", "LT"), ("EXPIRE", "n", "5", "LT"),
            ("EXPIRE", "n", "10", "NX", "XX"), ("EXPIRE", "n", "10", "GT", "LT"), ("EXPIRE", "n", "10", "BOGUS"), ("EXPIRE", "nokey", "10"), ("EXPIRE", "n", "x"), ("PERSIST", "n"), ("PERSIST", "n"),
            ("TTL", "n"), ("TTL", "nokey"), ("PEXPIRE", "n", "100000"), ("PTTL", "n"), ("EXPIRE", "n", "0"), ("EXISTS", "n"), ("SET", "n", "1"), ("EXPIRE", "n", "-5"), ("EXISTS", "n"),
            ("SET", "e1", "1"), ("EXPIRE", "e1", "100", "LT"), ("EXPIRE", "e1", "300", "GT"), ("EXPIRE", "e1", "300", "XX"), ("EXPIRE", "e1", "200", "LT"), ("EXPIRE", "e1", "200", "GT"),
            ("DEL", "a", "b", "nokey"), ("DEL"), ("EXISTS", "a", "a", "m1"), ("EXISTS"), ("TYPE", "m1"), ("TYPE", "nokey"),
            ("LPUSH", "l", "a", "b", "c"), ("RPUSH", "l", "x", "y"), ("LRANGE", "l", "0", "-1"), ("LRANGE", "l", "-2", "-1"), ("LRANGE", "l", "2", "1"), ("LRANGE", "l", "-100", "100"), ("LRANGE", "l", "10", "20"),
            ("LRANGE", "l", "a", "1"), ("LLEN", "l"), ("LINDEX", "l", "0"), ("LINDEX", "l", "-1"), ("LINDEX", "l", "99"), ("LINDEX", "l", "-99"), ("LINDEX", "l", "x"), ("LSET", "l", "0", "Z"), ("LSET", "l", "-1", "W"),
            ("LSET", "l", "99", "X"), ("LSET", "nokey", "0", "X"), ("LSET", "l", "x", "X"), ("LREM", "l", "0", "Z"), ("LREM", "l", "1", "b"), ("LREM", "l", "-1", "y"), ("LRANGE", "l", "0", "-1"),
            ("LINSERT", "l", "BEFORE", "c", "in"), ("LINSERT", "l", "AFTER", "nopivot", "in"), ("LINSERT", "nokey", "BEFORE", "c", "in"), ("LINSERT", "l", "MIDDLE", "c", "in"), ("LRANGE", "l", "0", "-1"),
            ("LPOP", "l"), ("RPOP", "l"), ("LPOP", "l", "2"), ("RPOP", "l", "0"), ("LPOP", "nokey"), ("LPOP", "nokey", "2"), ("RPOP", "nokey", "0"), ("LPOP", "l", "-1"), ("LPOP", "l", "x"), ("LPOP", "l", "1", "2"),
            ("LPUSHX", "nokey", "a"), ("RPUSHX", "nokey", "a"), ("LPUSH", "l2", "a"), ("LPUSHX", "l2", "b"), ("RPUSHX", "l2", "c"), ("LRANGE", "l2", "0", "-1"), ("LPUSH", "l2"), ("LPUSH"),
            ("LTRIM", "l2", "0", "0"), ("LRANGE", "l2", "0", "-1"), ("LTRIM", "l2", "5", "10"), ("EXISTS", "l2"), ("LTRIM", "nokey", "0", "1"), ("LTRIM", "l", "x", "1"),
            ("SADD", "s1", "a", "b", "a"), ("SADD", "s1", "c"), ("SCARD", "s1"), ("SISMEMBER", "s1", "a"), ("SISMEMBER", "s1", "z"), ("SMEMBERS", "s1"), ("SREM", "s1", "a", "zz"), ("SMEMBERS", "s1"),
            ("SREM", "s1", "b", "c"), ("EXISTS", "s1"), ("SMEMBERS", "s1"), ("SCARD", "s1"), ("SREM", "nokey", "a"), ("SADD", "s1"), ("SADD"),
            ("SET", "str", "v"), ("LPUSH", "str", "a"), ("SADD", "str", "a"), ("LLEN", "str"), ("SMEMBERS", "str"), ("LPUSH", "l3", "a"), ("GET", "l3"), ("SADD", "l3", "a"), ("INCR", "l3"), ("APPEND", "l3", "x"),
            ("STRLEN", "l3"), ("SADD", "s3", "a"), ("LPUSH", "s3", "a"), ("GET", "s3"), ("LRANGE", "s3", "0", "-1"), ("SET", "l3", "over"), ("TYPE", "l3"), ("LPOP", "str"), ("LINDEX", "str", "0"),
            ("RENAME", "str", "str2"), ("GET", "str2"), ("RENAME", "nokey", "x"), ("RENAME", "str2", "str2"), ("SET", "t", "1", "EX", "100"), ("RENAME", "t", "t2"), ("TTL", "t2"), ("SET", "t3", "1"), ("RENAME", "t3", "t2"), ("TTL", "t2"),
            ("KEYS", "*"), ("KEYS", "m?"), ("KEYS", "[ms]*"), ("KEYS", "[^m]*"), ("KEYS", "nomatch*"), ("KEYS", "l[0-9]"), ("KEYS"), ("DBSIZE"), ("DBSIZE", "x"),
            ("SCAN", "0"), ("SCAN", "0", "MATCH", "m*"), ("SCAN", "0", "COUNT", "100"), ("SCAN", "0", "MATCH", "m*", "COUNT", "10"), ("SCAN", "x"), ("SCAN", "0", "COUNT", "0"), ("SCAN", "0", "BOGUS"), ("SCAN"),
            ("PING"), ("PING", "hi"), ("PING", "a", "b"), ("NOSUCHCMD"), ("NOSUCHCMD", "a", "b"), ("get", "m1"), ("SeT", "case", "1"), ("gEt", "case"),
            ("RPUSH", "li", "a", "b", "c"), ("LINSERT", "li", "BEFORE", "b", "X"), ("LRANGE", "li", "0", "-1"), ("LINSERT", "li", "AFTER", "b", "Y"), ("LRANGE", "li", "0", "-1"),
            ("LINSERT", "li", "AFTER", "c", "Z"), ("LINSERT", "li", "BEFORE", "a", "W"), ("LRANGE", "li", "0", "-1"), ("RPUSH", "li", "a"), ("LINSERT", "li", "BEFORE", "a", "first-a"), ("LRANGE", "li", "0", "-1"),
            ("RPUSH", "dup", "x", "y", "x", "z", "x", "x"), ("LREM", "dup", "2", "x"), ("LRANGE", "dup", "0", "-1"), ("LREM", "dup", "-1", "x"), ("LRANGE", "dup", "0", "-1"), ("LREM", "dup", "0", "x"),
            ("LRANGE", "dup", "0", "-1"), ("RPUSH", "dup2", "x", "x", "x"), ("LREM", "dup2", "0", "x"), ("EXISTS", "dup2"), ("RPUSH", "dup3", "x", "y", "x", "y", "x"), ("LREM", "dup3", "-2", "x"),
            ("LRANGE", "dup3", "0", "-1"), ("LREM", "dup3", "5", "y"), ("LRANGE", "dup3", "0", "-1"),
            ("NOPE", "a" * 100, "b" * 100, "c" * 50), ("NOPE", "x" * 200), ("NOPE", *[str(i) for i in range(60)]), ("NOPE", "a\r\nb", "c\x00d"), ("N" * 200, "arg"), ("NOPE", "a" * 127, "b"), ("NOPE", "a" * 128, "b"),
            ("FLUSHDB"), ("DBSIZE"), ("SET", "\x00bin\xff", "\x00\x01"), ("GET", "\x00bin\xff"), ("SET", "", "empty-key"), ("GET", ""), ("SET", "k", ""), ("GET", "k"), ("STRLEN", "k"), ("APPEND", "k", ""),
        ]
        reset()
        for cmd in DIRECTED:
            both(*[c.encode("latin-1") if isinstance(c, str) else c for c in cmd])

        # ---- B. случайные последовательности ----------------------------------------------------------------------------------------------
        rnd = random.Random(20260925)
        cnt = [0]
        KEYS = [b"a", b"b", b"c", b"d", b"l", b"s", b"n"]
        VALS = [b"1", b"0", b"-1", b"42", b"x", b"", b"hello", b"9223372036854775807", b"-9223372036854775808", b"1.5", b" 7", b"\x00\xff", "привет".encode(), b"a b", b"007"]
        INTS = [b"0", b"1", b"-1", b"2", b"-2", b"3", b"5", b"100", b"-100", b"x", b"", b"1.0", b"9223372036854775807", b"99999999999999999999"]
        OPTS = [b"NX", b"XX", b"GET", b"KEEPTTL", b"EX", b"PX", b"BOGUS", b"ex", b"nx"]

        def gen():
            k = lambda: rnd.choice(KEYS)
            v = lambda: rnd.choice(VALS)
            i = lambda: rnd.choice(INTS)
            c = rnd.randrange(60)
            if c == 0: return (b"GET", k())
            if c == 1: return (b"SET", k(), v())
            if c == 2:
                a = [b"SET", k(), v()]
                for _ in range(rnd.randrange(0, 4)):
                    o = rnd.choice(OPTS); a.append(o)
                    if o.upper() in (b"EX", b"PX") and rnd.random() < 0.9: a.append(rnd.choice([b"100", b"100000", b"0", b"-5", b"x", b"1"]))
                return tuple(a)
            if c == 3: return (b"SETEX", k(), rnd.choice([b"100", b"0", b"-1", b"x"]), v())
            if c == 4: return (b"SETNX", k(), v())
            if c == 5: return (b"GETSET", k(), v())
            if c == 6: return (b"MGET", *[k() for _ in range(rnd.randrange(0, 4))])
            if c == 7: return (b"MSET", *[x for _ in range(rnd.randrange(0, 3)) for x in (k(), v())])
            if c == 8: return (b"APPEND", k(), v())
            if c == 9: return (b"STRLEN", k())
            if c == 10: return (b"INCR", k())
            if c == 11: return (b"DECR", k())
            if c == 12: return (b"INCRBY", k(), i())
            if c == 13: return (b"DECRBY", k(), i())
            if c == 14:
                opt = rnd.choice([b"NX", b"XX", b"GT", b"LT", None, None])
                cnt[0] += 1
                if opt == b"GT": val = str(3000 + cnt[0]).encode()          # всегда больше любого прежнего срока (без «почти равных»)
                elif opt == b"LT": val = str(40 - cnt[0] % 30).encode()
                else: val = rnd.choice([b"100", b"0", b"-1", b"x", b"1000"])
                return (b"EXPIRE", k(), val, *([opt] if opt else []))
            if c == 15: return (b"TTL", k())
            if c == 16: return (b"PTTL", k())
            if c == 17: return (b"PERSIST", k())
            if c == 18: return (b"DEL", *[k() for _ in range(rnd.randrange(0, 4))])
            if c == 19: return (b"EXISTS", *[k() for _ in range(rnd.randrange(0, 4))])
            if c == 20: return (b"TYPE", k())
            if c == 21: return (b"LPUSH", k(), *[v() for _ in range(rnd.randrange(0, 4))])
            if c == 22: return (b"RPUSH", k(), *[v() for _ in range(rnd.randrange(0, 4))])
            if c == 23: return (rnd.choice([b"LPUSHX", b"RPUSHX"]), k(), v())
            if c == 24: return (rnd.choice([b"LPOP", b"RPOP"]), k(), *([i()] if rnd.random() < 0.4 else []))
            if c == 25: return (b"LLEN", k())
            if c == 26: return (b"LRANGE", k(), i(), i())
            if c == 27: return (b"LTRIM", k(), i(), i())
            if c == 28: return (b"LINDEX", k(), i())
            if c == 29: return (b"LSET", k(), i(), v())
            if c == 30: return (b"LREM", k(), i(), v())
            if c == 31: return (b"LINSERT", k(), rnd.choice([b"BEFORE", b"AFTER", b"before", b"X"]), v(), v())
            if c == 32: return (b"SADD", k(), *[v() for _ in range(rnd.randrange(0, 4))])
            if c == 33: return (b"SREM", k(), *[v() for _ in range(rnd.randrange(0, 4))])
            if c == 34: return (b"SCARD", k())
            if c == 35: return (b"SISMEMBER", k(), v())
            if c == 36: return (b"SMEMBERS", k())
            if c == 37: return (b"KEYS", rnd.choice([b"*", b"?", b"[ab]", b"[^a]", b"[a-c]", b"[c-a]", b"*a*", b"a*", b"\\a", b"[a", b"[]", b"**", b"?*", b"[a-]", b"[^]", b"\\", b""]))
            if c == 38: return (b"RENAME", k(), k())
            if c == 39: return (b"DBSIZE",)
            if c == 40: return (b"SCAN", b"0", *([b"MATCH", rnd.choice([b"*", b"a*", b"[ab]"])] if rnd.random() < 0.5 else []))
            if c == 41: return (b"PING", *([v()] if rnd.random() < 0.3 else []))
            if c == 42: return (b"PSETEX", k(), rnd.choice([b"100000", b"0", b"x"]), v())
            if c == 43: return (b"SET", k(), v(), b"GET")
            if c == 44:
                cnt[0] += 1
                return (b"EXPIRE", k(), str(5000 + cnt[0]).encode(), b"GT")
            if c == 45: return (b"PEXPIRE", k(), rnd.choice([b"100000", b"0", b"-1"]))
            # неверное число аргументов / неизвестное
            if c == 46: return (rnd.choice([b"GET", b"SET", b"LPUSH", b"LRANGE", b"SADD", b"EXPIRE", b"INCR", b"LSET", b"SCAN"]), *[v() for _ in range(rnd.randrange(0, 2))])
            if c == 47: return (b"NOPE", k(), v())
            # часто «сталкиваем» типы: строка/список/множество на одном ключе
            if c == 48: return (b"LPUSH", b"a", v())
            if c == 49: return (b"SADD", b"a", v())
            if c == 50: return (b"SET", b"a", v())
            return (rnd.choice([b"GET", b"LLEN", b"SCARD", b"TYPE", b"SMEMBERS", b"LRANGE", b"STRLEN"]), rnd.choice(KEYS), *([b"0", b"-1"] if rnd.random() < 0.3 else []))

        for seq in range(150):
            reset()
            for _ in range(rnd.randrange(30, 120)):
                both(*gen())
        # ---- C. PUBLISH / SUBSCRIBE / PSUBSCRIBE ------------------------------------------------------------------------------------------
        reset()
        ps = r.pubsub(ignore_subscribe_messages=True)
        sub = store_holder["s"].pubsub()
        for ch in (b"chan:1", b"chan:2"):
            ps.subscribe(ch)
            sub.subscribe(ch)
        for pat in (b"chan:*", b"other:[ab]", b"x?z"):
            ps.psubscribe(pat)
            sub.psubscribe(pat)
        time.sleep(0.2)
        for ch, msg in ((b"chan:1", b"hello"), (b"chan:2", "привет".encode()), (b"chan:3", b"only-pattern"), (b"other:a", b"p"), (b"other:c", b"none"), (b"xyz", b"q"), (b"nobody", b"x"), (b"chan:1", b"")):
            a = real(b"PUBLISH", ch, msg)
            b = store_holder["s"].execute(b"PUBLISH", ch, msg)
            n[0] += 1
            check("PUBLISH число получателей", a == b, f"{ch!r}: {a} vs {b}")
        time.sleep(0.3)
        got_real = []
        t_end = time.time() + 1.2
        while time.time() < t_end:          # подтверждения подписки redis-py отдаёт отдельными «пустыми» вызовами — читаем всё окно, а не до первого None
            m = ps.get_message(timeout=0.1)
            if m is not None:
                got_real.append((m["type"], m["pattern"], m["channel"], m["data"]))
        got_rust = []
        while True:
            m = sub.get_message()
            if m is None:
                break
            got_rust.append(m)
        check("SUBSCRIBE: доставленные сообщения (как множество)", sorted(got_real, key=repr) == sorted(got_rust, key=repr), f"\n redis={sorted(got_real, key=repr)}\n rust ={sorted(got_rust, key=repr)}")
        ps.unsubscribe(b"chan:1"); sub.unsubscribe(b"chan:1")
        ps.punsubscribe(b"chan:*"); sub.punsubscribe(b"chan:*")
        time.sleep(0.2)
        for ch in (b"chan:1", b"chan:2", b"chan:3"):
            a = real(b"PUBLISH", ch, b"z")
            b = store_holder["s"].execute(b"PUBLISH", ch, b"z")
            check("PUBLISH после отписки", a == b, f"{ch!r}: {a} vs {b}")
        ps.close()
        # подписчик отброшен без явной отписки — Redis: закрытие соединения; здесь: Drop
        ps2 = r.pubsub(ignore_subscribe_messages=True)
        sub2 = store_holder["s"].pubsub()
        ps2.subscribe(b"drop:1"); sub2.subscribe(b"drop:1")
        ps2.psubscribe(b"dr*"); sub2.psubscribe(b"dr*")
        time.sleep(0.2)
        a = real(b"PUBLISH", b"drop:1", b"x"); b = store_holder["s"].execute(b"PUBLISH", b"drop:1", b"x")
        check("PUBLISH до закрытия подписчика", a == b == 2, f"{a} {b}")
        ps2.close()
        del sub2
        time.sleep(0.3)
        a = real(b"PUBLISH", b"drop:1", b"x"); b = store_holder["s"].execute(b"PUBLISH", b"drop:1", b"x")
        n[0] += 1
        check("PUBLISH после закрытия/Drop подписчика", a == b == 0, f"{a} {b}")
        # ---- D. сроки жизни в реальном времени (DBSIZE/KEYS — ПЕРВЫМИ: они сами обязаны вычистить просроченное, до ленивого удаления при обращении к ключу) ----
        for first in ((b"DBSIZE",), (b"KEYS", b"*"), (b"SCAN", b"0"), (b"EXISTS", b"t", b"t2"), (b"TYPE", b"t"), (b"GET", b"t"), (b"TTL", b"t2")):
            reset()
            both(b"SET", b"t", b"v", b"PX", b"300")
            both(b"SETEX", b"t2", b"100", b"v")
            both(b"PEXPIRE", b"t2", b"300")
            both(b"SET", b"keep", b"1")
            both(b"SET", b"ttl100", b"1", b"EX", b"100")
            time.sleep(0.7)      # 0.7 с, а не 0.5: TTL округляется ((мс+500)/1000), граница округления — ровно 0.5 с (гонка между процессами)
            both(*first)
            for cmd in ((b"GET", b"t"), (b"EXISTS", b"t", b"t2"), (b"TTL", b"t2"), (b"TTL", b"keep"), (b"TTL", b"ttl100"), (b"PTTL", b"ttl100"), (b"KEYS", b"*"), (b"DBSIZE"), (b"TYPE", b"t"), (b"GET", b"keep")):
                both(*cmd)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n(сравнений команд: {n[0]}; успешных проверок: {_OK})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    rc = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {sorted(set(FAILURES))[:8]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(rc)
