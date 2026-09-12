"""
llm_gateway.vector_space — идентичность embedding-пространства и
проверка совместимости векторов перед сравнением.

Главный принцип этого модуля: одинаковая размерность НЕ означает
одинаковое векторное пространство. Два вектора можно сравнивать
(cosine/dot) ТОЛЬКО если доказано, что оба получены из совместимого
embedding-пространства — иначе результат сравнения (число между -1 и
1) технически валиден, но семантически бессмыслен: он ничего не
говорит о реальной похожести исходных текстов.

VectorSpaceId — минимально достаточный набор фактов о том, ЧЕМ и КАК
был создан вектор, чтобы решить вопрос совместимости:
- backend (llamacpp / remote / ollama-compat) — какой класс движка;
- protocol — конкретный wire-протокол/эндпоинт, использованный внутри
  этого backend'а (у Ollama их два, у remote — минимум один на
  провайдера);
- model — имя модели ТОЧНО как оно было передано backend'у;
- dimension — фактическая длина вектора, как её вернул backend;
- normalized — применял ли САМ llm_gateway L2-нормализацию (сейчас
  всегда False — гейтвей не трогает содержимое вектора, нормализация,
  если она нужна вызывающему коду, остаётся его собственной
  ответственностью, как и было до этого модуля);
- schema_version — версия самой схемы fingerprint'а (на случай, если
  позже понадобится добавить ещё поле, влияющее на совместимость).

Имя модели НЕ считается само по себе достаточным признаком — два
разных backend'а/протокола с одинаковым именем модели дают РАЗНЫЙ
fingerprint, и наоборот, разные строки-имена одной и той же реальной
модели (опечатка, алиас) дадут разные fingerprint'ы — это осознанный
компромисс: fingerprint отражает то, что backend РЕАЛЬНО заявил о себе
в момент вызова, а не то, что "предположительно" совпадает.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

GATEWAY_EMBED_SCHEMA_VERSION = 1

# Специальное значение для векторов, чьё происхождение доказать нельзя
# (например, найдены в старом файле без разметки). Никогда не считается
# совместимым ни с чем — даже с самим собой (см. compatible()).
UNKNOWN_VECTOR_SPACE = "UNKNOWN_VECTOR_SPACE"


@dataclass(frozen=True)
class VectorSpaceId:
    backend: str
    protocol: str
    model: str
    dimension: int
    normalized: bool
    schema_version: int = GATEWAY_EMBED_SCHEMA_VERSION

    def fingerprint(self) -> str:
        """Стабильный, компактный идентификатор пространства — то, что
        реально сравнивается на равенство. Не криптографический секрет,
        просто удобный для хранения/сравнения хэш от всех полей."""
        raw = (
            f"{self.schema_version}|{self.backend}|{self.protocol}|"
            f"{self.model}|{self.dimension}|{'norm' if self.normalized else 'raw'}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "protocol": self.protocol,
            "model": self.model,
            "dimension": self.dimension,
            "normalized": self.normalized,
            "schema_version": self.schema_version,
            "fingerprint": self.fingerprint(),
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "VectorSpaceId":
        return VectorSpaceId(
            backend=d["backend"],
            protocol=d["protocol"],
            model=d["model"],
            dimension=int(d["dimension"]),
            normalized=bool(d["normalized"]),
            schema_version=int(d.get("schema_version", GATEWAY_EMBED_SCHEMA_VERSION)),
        )


def compatible(a: "VectorSpaceId | str | None", b: "VectorSpaceId | str | None") -> bool:
    """Единственная точка принятия решения "можно ли сравнивать эти два
    вектора". None или UNKNOWN_VECTOR_SPACE на ЛЮБОЙ стороне — всегда
    False, даже если обе стороны UNKNOWN: "неизвестно" на одной стороне
    и "неизвестно" на другой не доказывает, что это одно и то же
    неизвестное происхождение — это могут быть два РАЗНЫХ старых
    вектора от двух разных моделей, каждый без метки."""
    if a is None or b is None:
        return False
    if a == UNKNOWN_VECTOR_SPACE or b == UNKNOWN_VECTOR_SPACE:
        return False
    fp_a = a.fingerprint() if isinstance(a, VectorSpaceId) else a
    fp_b = b.fingerprint() if isinstance(b, VectorSpaceId) else b
    return fp_a == fp_b
