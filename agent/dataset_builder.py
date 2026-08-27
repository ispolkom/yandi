"""
agent/dataset_builder.py — Структурированное логирование опыта.
"""
import json
import time
import uuid
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime

DATASET_DIR = Path(__file__).parent / "dataset"
DATASET_DIR.mkdir(parents=True, exist_ok=True)

class DatasetBuilder:
    def __init__(self):
        self.episodes = []
        self.file_path = DATASET_DIR / f"episodes_{datetime.now().strftime('%Y%m%d')}.jsonl"
    
    def record_episode(self, data: Dict[str, Any]) -> str:
        """Сохраняет эпизод в dataset.

        Foundation Repair (episode<->trace identity): the episode id is now
        generated BEFORE the record is built and embedded into it as
        "episode_id", instead of being computed after _save() had already
        written the record and returned but never persisted. Callers that
        already know the producing trace's id should pass it through
        `data["trace_id"]` (reusing the existing Trace identity — see
        orch_tracer.py — rather than inventing a parallel identity system);
        this method does not require it, it just passes it through like any
        other field if present.
        """
        episode_id = f"ep_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        episode = {
            "episode_id": episode_id,
            "timestamp": time.time(),
            "datetime": datetime.now().isoformat(),
            **data
        }
        self.episodes.append(episode)
        self._save()
        return episode_id
    
    def _save(self):
        """Сохраняет все эпизоды в JSONL файл."""
        with open(self.file_path, "a", encoding="utf-8") as f:
            for episode in self.episodes:
                f.write(json.dumps(episode, ensure_ascii=False) + "\n")
        self.episodes = []
    
    def get_stats(self) -> Dict[str, Any]:
        """Возвращает статистику по датасету."""
        count = 0
        for f in DATASET_DIR.glob("episodes_*.jsonl"):
            with open(f, "r", encoding="utf-8") as file:
                count += sum(1 for _ in file)
        return {"total_episodes": count}

def get_dataset_builder() -> DatasetBuilder:
    return DatasetBuilder()
