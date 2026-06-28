#!/usr/bin/env python3
"""
assistant/dataset_versioning.py — семантическое версионирование датасетов.

Для каждого финального датасета создаёт manifest.json:
  version     — инкрементный номер версии
  hash        — SHA256 содержимого файла
  rows        — кол-во строк
  topics      — распределение тем
  diff_from   — имя предыдущего файла
  diff_added  — новые строки (по хешу)
  diff_removed— удалённые строки
  fine_tuned  — дата файн-тюна (null пока не обучены)
  validator   — имя файла отчёта валидации

API:
  vm = DatasetVersionManager()
  vm.stamp(jsonl_path)          → создать/обновить manifest
  vm.diff(v1_path, v2_path)     → список добавленных/удалённых строк
  vm.list()                     → все версии с метадатой
  vm.latest()                   → последняя версия

Команды:
  python3 assistant/dataset_versioning.py stamp [path]
  python3 assistant/dataset_versioning.py list
  python3 assistant/dataset_versioning.py diff v1.jsonl v2.jsonl
  python3 assistant/dataset_versioning.py latest
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

import redis

BASE        = Path(__file__).parent.parent
FINAL_DIR   = BASE / "registry" / "dataset" / "final"
MANIFEST    = BASE / "registry" / "dataset" / "manifest.json"
REPORT_KEY  = "council:skill:reports"
REPORT_CH   = "council:skill:report"


def _r() -> redis.Redis:
    return redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)


def _publish(r: redis.Redis, payload: dict):
    data = json.dumps(payload, ensure_ascii=False)
    r.lpush(REPORT_KEY, data)
    r.ltrim(REPORT_KEY, 0, 49)
    r.publish(REPORT_CH, data)


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _row_hashes(path: Path) -> set[str]:
    hashes = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                hashes.add(hashlib.md5(line.encode()).hexdigest())
    return hashes


def _load_rows(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows


def _load_manifest() -> dict:
    if MANIFEST.exists():
        try:
            return json.loads(MANIFEST.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"versions": [], "next_version": 1}


def _save_manifest(data: dict):
    MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=2))


class DatasetVersionManager:

    def __init__(self, r: Optional[redis.Redis] = None):
        self.r = r or _r()

    def stamp(self, path: Optional[Path] = None, val_report: str = "") -> dict:
        """
        Создать или обновить manifest-запись для датасета.
        path=None → берёт последний *_hf.jsonl из FINAL_DIR.
        """
        if path is None:
            files = sorted(FINAL_DIR.glob("*_hf.jsonl"))
            if not files:
                return {"error": "нет финальных датасетов"}
            path = files[-1]

        path = Path(path)
        if not path.exists():
            return {"error": f"файл не найден: {path}"}

        manifest = _load_manifest()
        versions = manifest["versions"]

        # Проверяем — уже есть запись для этого файла?
        existing = next((v for v in versions if v["file"] == path.name), None)

        rows    = _load_rows(path)
        topics  = dict(Counter(r.get("topic", "?") for r in rows))
        fhash   = _file_hash(path)

        # diff от предыдущей версии
        diff_added   = 0
        diff_removed = 0
        diff_from    = ""
        if versions:
            prev = versions[-1]
            prev_path = FINAL_DIR / prev["file"]
            if prev_path.exists() and prev_path != path:
                prev_hashes = _row_hashes(prev_path)
                cur_hashes  = _row_hashes(path)
                diff_added   = len(cur_hashes - prev_hashes)
                diff_removed = len(prev_hashes - cur_hashes)
                diff_from    = prev["file"]

        entry = {
            "version"     : existing["version"] if existing else manifest["next_version"],
            "file"        : path.name,
            "hash"        : fhash,
            "rows"        : len(rows),
            "topics"      : topics,
            "diff_from"   : diff_from,
            "diff_added"  : diff_added,
            "diff_removed": diff_removed,
            "fine_tuned"  : existing.get("fine_tuned") if existing else None,
            "val_report"  : val_report,
            "stamped_at"  : datetime.now().isoformat(),
        }

        if existing:
            idx = versions.index(existing)
            versions[idx] = entry
        else:
            versions.append(entry)
            manifest["next_version"] += 1

        _save_manifest(manifest)

        _publish(self.r, {
            "skill"       : "dataset_versioning",
            "action"      : "stamp",
            "version"     : entry["version"],
            "file"        : entry["file"],
            "rows"        : entry["rows"],
            "diff_added"  : diff_added,
            "diff_removed": diff_removed,
            "timestamp"   : entry["stamped_at"],
        })

        return entry

    def stamp_all(self) -> list[dict]:
        """Проставить версии всем существующим финальным датасетам."""
        results = []
        for f in sorted(FINAL_DIR.glob("*_hf.jsonl")):
            results.append(self.stamp(f))
        return results

    def diff(self, path1: Path, path2: Path) -> dict:
        """Построчный diff двух версий датасета."""
        path1, path2 = Path(path1), Path(path2)
        h1 = _row_hashes(path1)
        h2 = _row_hashes(path2)

        rows1 = {hashlib.md5(line.strip().encode()).hexdigest(): line.strip()
                 for line in open(path1, encoding="utf-8") if line.strip()}
        rows2 = {hashlib.md5(line.strip().encode()).hexdigest(): line.strip()
                 for line in open(path2, encoding="utf-8") if line.strip()}

        added   = [json.loads(rows2[h]) for h in (h2 - h1) if h in rows2]
        removed = [json.loads(rows1[h]) for h in (h1 - h2) if h in rows1]

        return {
            "file1"       : path1.name,
            "file2"       : path2.name,
            "added_count" : len(added),
            "removed_count": len(removed),
            "added"       : added[:10],
            "removed"     : removed[:10],
        }

    def list(self) -> list[dict]:
        return _load_manifest().get("versions", [])

    def latest(self) -> Optional[dict]:
        versions = self.list()
        return versions[-1] if versions else None

    def mark_finetuned(self, file_name: str, run_id: str):
        """Пометить версию как использованную для файн-тюна."""
        manifest = _load_manifest()
        for v in manifest["versions"]:
            if v["file"] == file_name:
                v["fine_tuned"] = {"run_id": run_id, "at": datetime.now().isoformat()}
                _save_manifest(manifest)
                return True
        return False


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    vm  = DatasetVersionManager()

    if cmd == "stamp":
        path   = Path(sys.argv[2]) if len(sys.argv) > 2 else None
        result = vm.stamp(path)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "stamp_all":
        results = vm.stamp_all()
        for r in results:
            print(f"  v{r['version']} {r['file']} rows={r['rows']} +{r['diff_added']} -{r['diff_removed']}")

    elif cmd == "list":
        versions = vm.list()
        if not versions:
            print("Нет версий. Запусти: python3 dataset_versioning.py stamp_all")
        for v in versions:
            ft = "✅ fine-tuned" if v.get("fine_tuned") else ""
            print(f"  v{v['version']} {v['file']}  rows={v['rows']}  "
                  f"+{v['diff_added']} -{v['diff_removed']}  {ft}")

    elif cmd == "diff":
        if len(sys.argv) < 4:
            print("Использование: diff <file1> <file2>")
            sys.exit(1)
        result = vm.diff(Path(sys.argv[2]), Path(sys.argv[3]))
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "latest":
        v = vm.latest()
        print(json.dumps(v, ensure_ascii=False, indent=2) if v else "Нет версий")

    else:
        print(f"Неизвестная команда: {cmd}")
        sys.exit(1)
