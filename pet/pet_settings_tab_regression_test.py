"""
pet/pet_settings_tab_regression_test.py — ВКЛАДКА «⚙ YANDI» (настройки): хранение, охрана, обзор папок, страница.

    ОДИН ГОЛОС, ЛЮБОЕ ЧИСЛО СОВЕТНИКОВ.   КЛЮЧ API НЕ ПОПАДАЕТ В ФАЙЛ.
    СТРАНИЦА ЧУЖОГО САЙТА НЕ ЧИТАЕТ ДИСК И НЕ МЕНЯЕТ НАСТРОЙКИ.   ТЕКСТ С ДИСКА — НЕ HTML.

Хранилище — файл во временной папке (YANDI_WEB_SETTINGS), сервер — только нужные роутеры на FastAPI TestClient;
ни Redis, ни модели, ни сеть не нужны.

Run: python -m pet.pet_settings_tab_regression_test
"""
from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


GOOD = {"voice": "local", "advisors": ["local", "api"], "local": {"path": "/mnt/d/models/qwen.gguf"},
        "remote": {"address": "", "model": ""}, "api": {"service": "openai", "model": "gpt-4o-mini"}}


def main() -> int:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import pet.ui_settings as us
    import pet.settings_api as settings_api
    import pet.chat_models as chat_models
    import pet.local_guard as local_guard

    tmp = Path(tempfile.mkdtemp(prefix="yandi-settings-"))
    settings_file = tmp / "cfg" / "web_settings.json"
    os.environ["YANDI_WEB_SETTINGS"] = str(settings_file)
    # «Применить» регистрирует локальную модель в шлюзе узла: настоящий файл модели и своё, временное хранилище шлюза
    (tmp / "models").mkdir()
    gguf = tmp / "models" / "q.gguf"
    gguf.write_bytes(b"GGUF" + b"\0" * 60)
    GOOD["local"]["path"] = str(gguf)
    os.environ["YANDI_NODE_DB"] = str(tmp / "node.db")
    os.environ["YANDI_KEK_PATH"] = str(tmp / "keys" / "kek.bin")
    try:
        # ── 1. валидация и файл ──
        check("1: до первого сохранения настройки «не сохранены»", us.load() == us.defaults() and us.load()["saved"] is False)
        bad = {
            "нет Голоса": {**GOOD, "voice": ""},
            "Голос — неизвестный вид": {**GOOD, "voice": "cloud"},
            "Голос выбран, блок пуст": {**GOOD, "voice": "remote"},
            "советник выбран, блок пуст": {**GOOD, "advisors": ["remote"]},
            "неизвестное поле": {**GOOD, "extra": 1},
            "ключ API в документе": {**GOOD, "api": {"service": "openai", "model": "m", "key": "sk-secret"}},
            "ключ под другим именем": {**GOOD, "api": {"service": "openai", "model": "m", "api_key": "sk-secret"}},
            "адрес не http": {**GOOD, "remote": {"address": "ftp://x", "model": "m"}},
            "сервис неизвестен": {**GOOD, "api": {"service": "evil", "model": "m"}},
            "имя модели с пробелами": {**GOOD, "api": {"service": "openai", "model": "a b; rm"}},
            "путь не строка": {**GOOD, "local": {"path": 5}},
            "советники не список": {**GOOD, "advisors": "local"},
        }
        for label, doc in bad.items():
            try:
                us.validate(doc)
                ok = False
            except us.SettingsError:
                ok = True
            check(f"1: {label} -> отказ", ok)
        saved = us.save({**GOOD, "api": {"service": "openai", "model": "gpt-4o-mini"}})
        text = settings_file.read_text(encoding="utf-8")
        check("1: сохранение пишет файл, помечает «сохранено» и возвращает проверенный документ", saved["saved"] is True and json.loads(text)["voice"] == "local")
        check("1: файл доступен только владельцу (0600)", stat.S_IMODE(settings_file.stat().st_mode) == 0o600)
        check("1: в файле нет ни слова про ключ", not re.search(r"key|secret|token", text, re.I))
        check("1: загрузка возвращает то же самое", us.load() == saved)
        check("1: не остаётся временных файлов", [p.name for p in settings_file.parent.iterdir()] == ["web_settings.json"])
        settings_file.write_text("{ не json", encoding="utf-8")
        check("1: повреждённый файл = как первый запуск (не падаем)", us.load() == us.defaults())
        settings_file.write_text(json.dumps({"saved": True, "voice": "remote", "advisors": [], "local": {}, "remote": {}, "api": {}}), encoding="utf-8")
        check("1: файл, не проходящий проверку (Голос без настройки), тоже как первый запуск", us.load()["saved"] is False)
        us.save(GOOD)

        # ── 2. API и охрана ──
        app = FastAPI()
        app.include_router(settings_api.router)
        app.include_router(chat_models.router)
        c = TestClient(app, base_url="http://127.0.0.1:9010")
        settings_file.unlink()
        d = c.get("/api/ui/settings").json()
        check("2: GET без файла: ok, не сохранено", d["ok"] and d["saved"] is False)
        d = c.post("/api/ui/settings", json=GOOD).json()
        check("2: POST верного документа сохраняет и отвечает документом", d["ok"] and d["saved"] and d["voice"] == "local" and settings_file.is_file())
        check("2: GET после POST возвращает сохранённое", c.get("/api/ui/settings").json()["advisors"] == ["local", "api"])
        before = settings_file.read_bytes()
        d = c.post("/api/ui/settings", json={**GOOD, "voice": "remote"}).json()
        check("2: неверный документ -> ok=false с причиной, файл не тронут", d["ok"] is False and d["error"] and settings_file.read_bytes() == before)
        d = c.post("/api/ui/settings", json={**GOOD, "api": {"service": "openai", "model": "m", "key": "sk-x"}}).json()
        check("2: документ с ключом отвергнут, в файле ключа нет", d["ok"] is False and b"sk-x" not in settings_file.read_bytes())

        evil = {
            "чужой Origin": {"Origin": "http://evil.example"},
            "Origin с другим портом": {"Origin": "http://127.0.0.1:9999"},
            "Origin: null": {"Origin": "null"},
            "Sec-Fetch-Site: cross-site": {"Sec-Fetch-Site": "cross-site"},
            "Sec-Fetch-Site: same-site": {"Sec-Fetch-Site": "same-site"},
            "Host не локальный (DNS-rebinding)": {"Host": "evil.example:9010"},
        }
        endpoints = [("GET", "/api/ui/settings", None), ("POST", "/api/ui/settings", GOOD), ("GET", "/api/models/browse?path=/", None),
                     ("POST", "/api/models/check", {"path": "/etc/passwd"})]
        for label, headers in evil.items():
            codes = [c.request(m, p, json=b, headers=headers).status_code for m, p, b in endpoints]
            check(f"2: {label}: все четыре эндпоинта закрыты (403)", codes == [403] * 4, repr(codes))
        good_headers = [{"Origin": "http://127.0.0.1:9010", "Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"}, {}]
        check("2: своя страница (тот же Origin) и локальные программы (без заголовков) проходят",
              all(c.get("/api/ui/settings", headers=h).status_code == 200 for h in good_headers))
        check("2: и localhost как хост", TestClient(app, base_url="http://localhost:9010").get("/api/ui/settings").status_code == 200)
        check("2: охрана висит на самом роутере (нельзя забыть на новом эндпоинте)",
              any(dep.dependency is local_guard.require_local_origin for dep in settings_api.router.dependencies)
              and any(dep.dependency is local_guard.require_local_origin for dep in chat_models.router.dependencies))

        # ── 3. обзор папок и проверка файла ──
        disk = tmp / "disk2"
        (disk / "models" / "big").mkdir(parents=True)
        (disk / ".hidden").mkdir()
        (disk / "models" / "b.gguf").write_bytes(b"GGUF" + b"\0" * 64)
        (disk / "models" / "A.GGUF").write_bytes(b"GGUF" + b"\0" * 8)
        (disk / "models" / "notes.txt").write_text("x")
        (disk / "models" / "<img src=x onerror=alert(1)>.gguf").write_bytes(b"GGUF")
        (disk / "models" / "fake.gguf").write_bytes(b"NOPE" + b"\0" * 8)
        (disk / "models" / "dangling.gguf").symlink_to(disk / "nowhere.gguf")
        d = c.get("/api/models/browse", params={"path": str(disk / "models")}).json()
        names = [(e["kind"], e["name"]) for e in d["entries"]]
        check("3: обзор любой папки: сначала подпапки, потом файлы .gguf (без регистра), чужие файлы и битые ссылки не показаны",
              d["ok"] and names[0] == ("dir", "big") and {n for _, n in names} == {"big", "A.GGUF", "b.gguf", "fake.gguf", "<img src=x onerror=alert(1)>.gguf"}, repr(names))
        check("3: у файла есть размер, у папки нет; есть родитель и ярлыки", next(e for e in d["entries"] if e["name"] == "b.gguf")["size"] == 68
              and next(e for e in d["entries"] if e["name"] == "big")["size"] is None and d["parent"] == str(disk) and d["shortcuts"])
        check("3: скрытые папки не видны, пока не попросят", ".hidden" not in [e["name"] for e in c.get("/api/models/browse", params={"path": str(disk)}).json()["entries"]]
              and ".hidden" in [e["name"] for e in c.get("/api/models/browse", params={"path": str(disk), "hidden": "true"}).json()["entries"]])
        check("3: путь к файлу открывает его папку", c.get("/api/models/browse", params={"path": str(disk / "models" / "b.gguf")}).json()["path"] == str(disk / "models"))
        check("3: корень диска открывается, у корня нет родителя", c.get("/api/models/browse", params={"path": "/"}).json()["parent"] is None)
        check("3: относительный путь и несуществующая папка — понятная ошибка",
              c.get("/api/models/browse", params={"path": "models"}).json()["ok"] is False
              and c.get("/api/models/browse", params={"path": str(disk / "нет")}).json()["ok"] is False)
        d = c.get("/api/models/browse").json()
        check("3: без пути открывается домашняя папка", d["ok"] and d["path"] == str(Path.home().resolve()))
        chk = lambda p: c.post("/api/models/check", json={"path": p}).json()
        check("3: проверка: настоящий GGUF — ok с именем и размером", chk(str(disk / "models" / "b.gguf"))["ok"] and chk(str(disk / "models" / "b.gguf"))["file"] == "b.gguf")
        for label, path in (("не тот заголовок", disk / "models" / "fake.gguf"), ("не .gguf", disk / "models" / "notes.txt"), ("нет файла", disk / "нет.gguf"),
                            ("папка", disk / "models"), ("пусто", ""), ("относительный путь", "b.gguf")):
            r = chk(str(path))
            check(f"3: проверка: {label} -> ошибка с причиной", r["ok"] is False and r["error"], repr(r))
        cm_src = inspect.getsource(chat_models)
        check("3: обзор и проверка только читают: в модуле нет записи в хранилище моделей и нет open(..., 'w')",
              "set_model_entry" not in cm_src and "secure_store" not in cm_src and not re.search(r"open\([^)]*['\"][wa]", cm_src))

        # ── 4. страница: вкладка, порядок, без HTML из данных ──
        html = (ROOT / "pet" / "council_chat_server.py").read_text(encoding="utf-8")
        nav = html[html.index('<div class="nav">'):html.index('</div>', html.index('<div class="nav">'))]
        check("4: вкладка «⚙ YANDI» есть в шапке", 'id="tab-settings"' in nav and "switchMode('settings')" in nav)
        check("4: у вкладки есть своя панель без чата и подключены её файлы",
              'id="msgs-settings"' in html and '/media/settings_tab.css' in html and '/media/settings_tab.js' in html)
        switch = html[html.index("async function switchMode"):html.index("async function _loadTabHistory")]
        check("4: switchMode знает про вкладку: класс mode-settings, панель, вход в вкладку",
              'mode-settings' in switch and '"settings"' in switch and "SettingsTab.onEnter" in switch)
        css = (ROOT / "pet" / "media" / "settings_tab.css").read_text(encoding="utf-8")
        js = (ROOT / "pet" / "media" / "settings_tab.js").read_text(encoding="utf-8")
        for hidden in ("#input-area", ".right-panel", "#status-bar"):
            check(f"4: в этой вкладке скрыто {hidden} (чата нет)", re.search(rf"body\.mode-settings\s+{re.escape(hidden)}", css) is not None)
        check("4: до сохранения вкладка ПЕРВАЯ, после — в конец (order)", 'saved ? "100" : "-1"' in js)
        code = re.sub(r"\s//[^\n]*", "", re.sub(r"/\*.*?\*/", "", js, flags=re.S))
        check("4: скрипт вкладки не присваивает HTML (имена файлов и ответы сервера — только текст)",
              not re.search(r"\.innerHTML\s*=|\.outerHTML\s*=|insertAdjacentHTML|document\.write|\beval\(", code))
        check("4: ключ API читается только чтобы стереть: в отправляемый документ он не входит",
              code.count("st-api-key") == 3 and "api: { service: $(\"st-api-service\").value, model: $(\"st-api-model\").value.trim() }" in code
              and 'value = ""' in code)
        node = shutil.which("node")
        check("4: скрипт вкладки проходит проверку синтаксиса", node is None or subprocess.run([node, "--check", str(ROOT / "pet/media/settings_tab.js")], capture_output=True).returncode == 0)
        check("4: автопереход при первом запуске не зацикливается (только если вкладка ещё не открыта)", 'currentMode !== "settings"' in js)

        # ── 5. МУТАНТЫ ──
        src = inspect.getsource(us)

        def mutated(old, new):
            assert src.count(old) == 1, old
            mod = types.ModuleType("pet.ui_settings__mutant")
            mod.__file__ = us.__file__
            sys.modules[mod.__name__] = mod
            exec(compile(src.replace(old, new), us.__file__, "exec"), mod.__dict__)
            return mod
        m = mutated('if any(k in api_in for k in ("key", "api_key", "token", "secret")):', "if False:")
        try:
            m.validate({**GOOD, "api": {"service": "openai", "model": "m", "key": "sk"}})
            caught = False
        except m.SettingsError:
            caught = True
        check("M1: МУТАНТ «ключ API принимается в документ» ЛОВИТСЯ (нужен отказ)", not caught)
        m = mutated('    if not out["voice"]:\n        raise SettingsError("выберите Голос: тот, кто будет отвечать")\n', "")
        try:
            m.validate({**GOOD, "voice": ""})
            caught = False
        except m.SettingsError:
            caught = True
        check("M2: МУТАНТ «можно сохранить без Голоса» ЛОВИТСЯ", not caught)
        from fastapi import APIRouter
        unguarded = APIRouter()
        unguarded.add_api_route("/api/ui/settings", settings_api.get_settings, methods=["GET"])
        bad_app = FastAPI()
        bad_app.include_router(unguarded)
        leaked = TestClient(bad_app, base_url="http://127.0.0.1:9010").get("/api/ui/settings", headers={"Origin": "http://evil.example"}).status_code
        check("M3: МУТАНТ «охрана снята с роутера» ЛОВИТСЯ: без неё чужой Origin получает ответ, а проверки выше требуют 403", leaked == 200)
    finally:
        os.environ.pop("YANDI_WEB_SETTINGS", None)
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 72)
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}")
        return 1
    print("РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
