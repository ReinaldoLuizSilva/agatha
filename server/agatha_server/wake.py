"""Wake word pela transcrição (ADR-008): a Agatha só responde quando é chamada pelo nome.

Também reconhece os comandos de parar do barge-in ("Agatha, para", "chega"; ADR-016).

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
    conversation_s: float = 20.0  # 10 s foi curto no teste real (pausa de ~11 s para pensar)
    first_words: int = 4  # o nome conta se estiver entre as primeiras N palavras...
    last_words: int = 2   # ...ou entre as últimas N


def _normalize(word: str) -> str:
    word = unicodedata.normalize("NFKD", word.lower())
    return re.sub(r"[^a-z]", "", "".join(c for c in word if not unicodedata.combining(c)))


def is_addressed(text: str, config: WakeConfig) -> bool:
    # hífen separa: com "agatha-srv" no vocabulário (AGATHA_STT_NAMES), "Agatha, chega" já saiu
    # do Whisper como "Agatha-srv" (benchmark da wake word, Fase 5)
    words = [w for w in (_normalize(w) for w in re.split(r"[\s\-‐–—]+", text)) if w]
    if not words:
        return False
    names = {_normalize(w) for w in config.words}
    candidates = words[: config.first_words] + words[-config.last_words:]
    return any(w in names for w in candidates)


# Barge-in: "Agatha, para", "chega", "cancela"... Curtos de propósito: "Agatha, para que serve um
# backup?" é pergunta, não comando.
STOP_WORDS = {"para", "pare", "parar", "chega", "cancela", "cancelar", "silencio", "cala", "quieta",
              "basta", "esquece", "deixa", "stop"}
STOP_MAX_WORDS = 3
LEADING_FILLERS = {"pode", "ok", "ta", "ja", "ei", "oi", "ah", "olha", "agora"}  # "Agatha, pode parar"


def interruption_kind(text: str, config: WakeConfig) -> str:
    """O que uma fala por cima da Agatha pede: "stop" (parar e ouvir) ou "question" (nova pergunta).

    Só o nome ("Agatha!") também é "stop": quem chama quer que ela pare e ouça.
    """
    names = {_normalize(w) for w in config.words}
    words = [w for w in (_normalize(w) for w in re.split(r"[\s\-‐–—]+", text)) if w and w not in names]
    while words and words[0] in LEADING_FILLERS:
        words = words[1:]
    if not words or (len(words) <= STOP_MAX_WORDS and words[0] in STOP_WORDS):
        return "stop"
    return "question"
