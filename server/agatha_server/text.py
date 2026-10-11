"""Texto da resposta: divisão em trechos falados, limpeza e normalização para o TTS.

- `Chunker`: recebe o texto do LLM em streaming e libera trechos assim que fecham uma frase,
  ou numa pausa natural (vírgula, dois-pontos...) quando o trecho já está longo. O primeiro
  trecho é liberado mais cedo, para o áudio começar logo (ADR-004).
- `clean`: remove markdown, emoji e símbolos (vai para a tela e para o TTS).
- `normalize_for_speech`: números, porcentagens, IPs, versões, horas e siglas por extenso
  (só para o TTS; ver ADR-005).
"""

import re
import unicodedata

from num2words import num2words

_SENTENCE_END = re.compile(r"[.!?…]+[\"')\]]*\s")
_CLAUSE_END = re.compile(r"[,;:—–]\s")


class Chunker:
    def __init__(self, first_min_chars: int = 30, clause_min_chars: int = 100):
        self.first_min_chars = first_min_chars
        self.clause_min_chars = clause_min_chars
        self._buffer = ""
        self._emitted = 0

    def feed(self, delta: str) -> list[str]:
        self._buffer += delta
        chunks = []
        while (cut := self._find_cut()) is not None:
            chunk, self._buffer = self._buffer[:cut].strip(), self._buffer[cut:]
            if chunk:
                chunks.append(chunk)
                self._emitted += 1
        return chunks

    def flush(self) -> list[str]:
        chunk, self._buffer = self._buffer.strip(), ""
        return [chunk] if chunk else []

    def _find_cut(self) -> int | None:
        if m := _SENTENCE_END.search(self._buffer):
            return m.end()
        if "\n" in self._buffer.strip():
            return self._buffer.index("\n", len(self._buffer) - len(self._buffer.lstrip())) + 1
        min_chars = self.first_min_chars if self._emitted == 0 else self.clause_min_chars
        for m in _CLAUSE_END.finditer(self._buffer):
            if m.start() >= min_chars:
                return m.end()
        return None


# --- Limpeza -------------------------------------------------------------------------

_MARKDOWN = [
    (re.compile(r"```.*?```", re.DOTALL), " "),          # blocos de código
    (re.compile(r"`([^`]*)`"), r"\1"),                   # código inline
    (re.compile(r"\*\*|__|~~"), ""),                     # negrito, tachado
    (re.compile(r"(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)"), ""),  # itálico
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE), ""),     # títulos
    (re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.MULTILINE), ""),  # itens de lista
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),       # links markdown
    (re.compile(r"https?://\S+"), "link"),               # URLs
]


def clean(text: str) -> str:
    for pattern, repl in _MARKDOWN:
        text = pattern.sub(repl, text)
    # emoji e pictogramas (categoria So) e caracteres de controle
    text = "".join(c for c in text if unicodedata.category(c) not in ("So", "Cs", "Co", "Cc") or c in "\n\t")
    text = re.sub(r"[()\[\]{}<>|\\^\"]", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:!?…])", r"\1", text)
    return text.strip()


# --- Normalização para a fala --------------------------------------------------------

def _int(n: int) -> str:
    return num2words(n, lang="pt_BR").replace(",", "")  # "mil, quinhentos": a vírgula viraria pausa


def _digits_words(s: str) -> str:
    return _int(int(s))


# Pronúncia de siglas comuns (ajustável de ouvido; o eSpeak soletra mal algumas)
ACRONYMS = {
    "CPU": "cê pê u",
    "GPU": "gê pê u",
    "VRAM": "vê ram",
    "RAM": "ram",
    "SQL": "ésse quê éle",
    "IP": "i pê",
    "TI": "tê i",
    "DBA": "dê bê á",
    "API": "a pê i",
    "URL": "u érre éle",
    "SSH": "ésse ésse agá",
    "HTTP": "agá tê tê pê",
    "HTTPS": "agá tê tê pê ésse",
    "LLM": "éle éle ême",
    "VPN": "vê pê ene",
    "DNS": "dê ene ésse",
    "SO": "ésse ó",
}

UNITS = {
    "TB": "terabytes", "GB": "gigabytes", "MB": "megabytes", "KB": "kilobytes",
    "GHz": "gigahertz", "MHz": "megahertz", "ms": "milissegundos",
}

_IP = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")
_TIME = re.compile(r"\b([01]?\d|2[0-3])(?::|h)([0-5]\d)\b")
_HOURS = re.compile(r"\b([01]?\d|2[0-3])h\b")
_PERCENT = re.compile(r"(\d+(?:,\d+)?)\s?%")
_MONEY = re.compile(r"R\$\s?(\d{1,3}(?:\.\d{3})*|\d+)(?:,(\d{2}))?")
_ORDINAL = re.compile(r"\b(\d+)\s?[ºª°]")
_UNIT = re.compile(r"\b(\d+(?:[.,]\d+)?)\s?(" + "|".join(UNITS) + r")\b")
_THOUSANDS = re.compile(r"\b\d{1,3}(?:\.\d{3})+\b")
_DECIMAL = re.compile(r"\b(\d+),(\d+)\b")
_VERSION = re.compile(r"\b(\d+)((?:\.\d+)+)\b")
_INTEGER = re.compile(r"\b\d+\b")
_ACRONYM = re.compile(r"\b(" + "|".join(sorted(ACRONYMS, key=len, reverse=True)) + r")\b")


def _number(token: str) -> str:
    """Número em notação brasileira (1.575 / 2,5 / 42) por extenso."""
    if _THOUSANDS.fullmatch(token):
        return _int(int(token.replace(".", "")))
    if m := _DECIMAL.fullmatch(token):
        return f"{_int(int(m[1]))} vírgula {' '.join(_digits_words(d) for d in m[2]) if m[2].startswith('0') else _int(int(m[2]))}"
    if m := _VERSION.fullmatch(token):
        parts = [m[1]] + m[2].lstrip(".").split(".")
        return " ponto ".join(_int(int(p)) for p in parts)
    return _int(int(token))


def normalize_for_speech(text: str) -> str:
    text = _IP.sub(lambda m: " ponto ".join(_int(int(g)) for g in m.groups()), text)
    text = _MONEY.sub(
        lambda m: _int(int(m[1].replace(".", ""))) + " reais"
        + (f" e {_int(int(m[2]))} centavos" if m[2] and int(m[2]) else ""),
        text,
    )
    text = _TIME.sub(lambda m: f"{_int(int(m[1]))} e {_int(int(m[2]))}" if int(m[2]) else f"{_int(int(m[1]))} horas", text)
    text = _HOURS.sub(lambda m: f"{_int(int(m[1]))} horas", text)
    text = _PERCENT.sub(lambda m: f"{_number(m[1])} por cento", text)
    text = _ORDINAL.sub(lambda m: num2words(int(m[1]), lang="pt_BR", to="ordinal"), text)
    text = _UNIT.sub(lambda m: f"{_number(m[1])} {UNITS[m[2]]}", text)
    text = _THOUSANDS.sub(lambda m: _number(m[0]), text)
    text = _DECIMAL.sub(lambda m: _number(m[0]), text)
    text = _VERSION.sub(lambda m: _number(m[0]), text)
    text = _INTEGER.sub(lambda m: _int(int(m[0])), text)
    text = _ACRONYM.sub(lambda m: ACRONYMS[m[1]], text)
    text = text.replace("&", " e ").replace("+", " mais ").replace("=", " igual a ")
    return re.sub(r"\s+", " ", text).strip()
