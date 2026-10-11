"""Wake word pela transcrição (ADR-008): a Agatha só responde quando é chamada pelo nome.

O nome precisa aparecer no começo da frase ("Agatha, que horas são?", "Ok, Agatha, ...") ou no
fim ("Obrigado, Agatha"). Menções no meio de uma frase longa (a TV falando de "Agatha Christie")
não contam. Depois de cada resposta há um "modo conversa": por `conversation_s` segundos, falas
sem o nome também são aceitas.
"""

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class WakeConfig:
    enabled: bool = True
    # comparadas sem acento e em minúsculas ("Ágata" vira "agata")
    words: tuple[str, ...] = ("agatha", "agata", "agatah")
    conversation_s: float = 10.0
    first_words: int = 4  # o nome conta se estiver entre as primeiras N palavras...
    last_words: int = 2   # ...ou entre as últimas N


def _normalize(word: str) -> str:
    word = unicodedata.normalize("NFKD", word.lower())
    return re.sub(r"[^a-z]", "", "".join(c for c in word if not unicodedata.combining(c)))


def is_addressed(text: str, config: WakeConfig) -> bool:
    words = [w for w in (_normalize(w) for w in text.split()) if w]
    if not words:
        return False
    names = {_normalize(w) for w in config.words}
    candidates = words[: config.first_words] + words[-config.last_words:]
    return any(w in names for w in candidates)
