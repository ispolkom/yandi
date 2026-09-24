//! АВТОГЕНЕРИРОВАНО rustlib/gen_resolver_data.py из agent/object_resolver.py и agent/entity_resolver.py — не править руками.
//! Порядок типов и паттернов — как в Python dict/list (при равной уверенности выигрывает первый).

pub struct ObjectType { pub key: &'static str, pub patterns: &'static [&'static str], pub type_: &'static str, pub confidence: f64, pub analyzer: &'static str }

pub static OBJECT_TYPES: &[ObjectType] = &[
    ObjectType { key: "song", patterns: &["песн", "song", "трек", "композиц", "музык", "мелоди", "don't cry", "i don't cry", "guns n roses", "ганз энд роузез", "виагра", "via gra"], type_: "song", confidence: 0.7, analyzer: "SongAnalyzer" },
    ObjectType { key: "movie", patterns: &["фильм", "movie", "кино", "сериал", "interstellar", "интерстеллар", "matrix", "матрица"], type_: "movie", confidence: 0.7, analyzer: "MovieAnalyzer" },
    ObjectType { key: "book", patterns: &["книг", "book", "роман", "повест", "рассказ", "story"], type_: "book", confidence: 0.7, analyzer: "BookAnalyzer" },
    ObjectType { key: "person", patterns: &["ницше", "nietzsche", "пушкин", "pushkin", "достоевск", "dostoevsky", "толстой", "tolstoy", "человек", "person", "личность"], type_: "person", confidence: 0.6, analyzer: "CharacterAnalyzer" },
    ObjectType { key: "idea", patterns: &["свобод", "justice", "справедлив", "любов", "love", "смысл", "meaning", "жизн", "life", "смерт", "death", "философи", "philosophy"], type_: "idea", confidence: 0.5, analyzer: "IdeaAnalyzer" },
    ObjectType { key: "self_reflection", patterns: &["ты женщина", "ты девушка", "первая цифровая", "цифровая личность", "если бы ты была", "ты бы хотела", "чего бы тебе хотелось", "чего ты хочешь", "твои чувства", "твой характер", "что ты чувствуешь", "какая ты", "расскажи о себе", "опиши себя", "твоё состояние", "как ты себя", "YANDI", "Янди", "ты цифровая"], type_: "self_reflection", confidence: 0.7, analyzer: "SelfReflectionAnalyzer" },
    ObjectType { key: "game", patterns: &["игр", "game", "сектор", "x3", "игра", "gaming"], type_: "game", confidence: 0.6, analyzer: "GameAnalyzer" },
];

pub static KNOWN_GAMES: &[&str] = &["x3", "x3 terran conflict", "x3 albion prelude", "x4", "x4 foundations"];
pub static KNOWN_GAME_TERMS: &[&str] = &["сектор", "звездная система", "корабль", "станция", "гонка", "фракция"];
pub static KNOWN_MEDIA: &[&str] = &["фильм", "сериал", "аниме", "книга", "игра", "песня"];
