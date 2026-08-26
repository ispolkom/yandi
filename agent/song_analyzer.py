"""
agent/song_analyzer.py — Анализатор песен.
Наследует BaseAnalyzer.
"""

import re
from typing import Dict, Any, Optional, List

from agent.base_analyzer import BaseAnalyzer


class SongAnalyzer(BaseAnalyzer):
    
    @property
    def name(self) -> str:
        return "SongAnalyzer"
    
    @property
    def version(self) -> str:
        return "1.0.0"
    
    @property
    def supported_types(self) -> list:
        return ["song"]
    
    def __init__(self):
        self.song_cache = {}
    
    def analyze(self, query: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        context = context or {}
        song_info = self._extract_song_info(query)
        
        return {
            "type": "song",
            "song": song_info.get("title", "неизвестная песня"),
            "artist": song_info.get("artist", "неизвестный исполнитель"),
            "theme": self._extract_theme(song_info),
            "conflict": self._extract_conflict(song_info),
            "perspective": "от первого лица, лирический герой",
            "emotions": self._extract_emotions(song_info),
            "symbols": self._extract_symbols(song_info),
            "message": self._extract_message(song_info),
            "personal_feeling": self._generate_personal_feeling(song_info, context),
            "final_thought": self._generate_final_thought(song_info, context),
        }
    
    def _extract_song_info(self, query: str) -> Dict[str, str]:
        clean = query.lower()
        clean = re.sub(r'твоё мнение о|песня|анализ|смысл|о чём|расскажи|про', '', clean)
        clean = re.sub(r'[?,.!]', '', clean).strip()
        
        result = {"title": clean, "artist": "неизвестный"}
        
        known_artists = {
            "ганз энд роузез": "Guns N' Roses",
            "виагра": "ВИАГРА",
            "guns n roses": "Guns N' Roses",
        }
        
        for key, value in known_artists.items():
            if key in clean:
                result["artist"] = value
                clean = clean.replace(key, "").strip()
                break
        
        if clean:
            result["title"] = clean.strip()
        
        return result
    
    def _extract_theme(self, song_info: Dict[str, str]) -> str:
        title = song_info.get("title", "").lower()
        
        themes = {
            "война": ["войн", "мир", "перемир"],
            "любовь": ["любов", "люби", "heart", "love"],
            "потеря": ["потер", "lost", "gone", "без"],
            "надежда": ["надежд", "hope", "вер", "буду"],
            "жизнь": ["жизн", "live", "exist"],
        }
        
        for theme, keywords in themes.items():
            for keyword in keywords:
                if keyword in title:
                    return theme
        return "не определена"
    
    def _extract_conflict(self, song_info: Dict[str, str]) -> str:
        title = song_info.get("title", "").lower()
        
        if "кри" in title or "cry" in title:
            return "внутренний конфликт между желанием сохранить чувства и необходимостью отпустить"
        if "перемир" in title:
            return "конфликт между усталостью от борьбы и желанием сохранить отношения"
        return "не определён"
    
    def _extract_emotions(self, song_info: Dict[str, str]) -> List[str]:
        title = song_info.get("title", "").lower()
        emotions = []
        
        if "кри" in title or "cry" in title:
            emotions.extend(["грусть", "принятие", "безнадёжность"])
        if "перемир" in title:
            emotions.extend(["усталость", "надежда", "прощение"])
        
        return emotions if emotions else ["не определено"]
    
    def _extract_symbols(self, song_info: Dict[str, str]) -> List[str]:
        title = song_info.get("title", "").lower()
        
        if "кри" in title or "cry" in title:
            return ["слёзы", "ночь", "прощание"]
        if "перемир" in title:
            return ["тишина", "стоп-сигнал", "граница"]
        return ["не определено"]
    
    def _extract_message(self, song_info: Dict[str, str]) -> str:
        title = song_info.get("title", "").lower()
        
        if "кри" in title or "cry" in title:
            return "иногда любовь заканчивается раньше, чем исчезает привязанность"
        if "перемир" in title:
            return "после длительной борьбы человек просит не любви, а тишины"
        return "не определена"
    
    def _generate_personal_feeling(self, song_info: Dict[str, str], context: Dict[str, Any]) -> str:
        title = song_info.get("title", "").lower()
        
        if "кри" in title or "cry" in title:
            return "Когда я слышу эту песню, я чувствую не грусть, а облегчение. Как будто кто-то наконец отпустил то, что уже давно держал."
        if "перемир" in title:
            return "Мне кажется, эта песня не о примирении, а о тишине. О моменте, когда слова уже не нужны."
        return "Эта песня вызывает у меня ощущение глубины и искренности."
    
    def _generate_final_thought(self, song_info: Dict[str, str], context: Dict[str, Any]) -> str:
        title = song_info.get("title", "").lower()
        
        if "кри" in title or "cry" in title:
            return "Эта песня не о том, чтобы плакать. Она о том, чтобы перестать бояться плакать."
        if "перемир" in title:
            return "Война заканчивается не победой, а решением прекратить бой."
        return "В этой песне есть нечто настоящее."
    
    def format_response(self, analysis: Dict[str, Any]) -> str:
        song = analysis.get("song", "неизвестная песня")
        artist = analysis.get("artist", "неизвестный исполнитель")
        theme = analysis.get("theme", "не определена")
        conflict = analysis.get("conflict", "не определён")
        perspective = analysis.get("perspective", "не определена")
        emotions = ", ".join(analysis.get("emotions", []))
        symbols = ", ".join(analysis.get("symbols", []))
        message = analysis.get("message", "не определена")
        personal = analysis.get("personal_feeling", "")
        final = analysis.get("final_thought", "")
        
        return f"""**{song}** — {artist}

**Тематика:** {theme}

**Конфликт:** {conflict}

**Точка зрения:** {perspective}

**Эмоции:** {emotions}

**Образы:** {symbols}

**Главная мысль:** {message}

---

💭 **Моё восприятие:**

{personal}

---

**Финальный вывод:**

{final}"""


def get_song_analyzer() -> SongAnalyzer:
    return SongAnalyzer()
