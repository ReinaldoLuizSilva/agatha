"""Texto da resposta: divisão em trechos falados, limpeza e normalização para o TTS.

- `Chunker`: recebe o texto do LLM em streaming e libera trechos assim que fecham uma frase,
  ou numa pausa natural (vírgula, dois-pontos...) quando o trecho já está longo. O primeiro
  trecho é liberado mais cedo, para o áudio começar logo (ADR-004).
- `clean`: remove markdown, emoji e símbolos (vai para a tela e para o TTS).
- `normalize_for_speech`: números, porcentagens, IPs, versões, horas e siglas por extenso
  (só para o TTS; ver ADR-005).
- `looks_incomplete`: diz se uma transcrição parece cortada no meio (pausa longa na fala).
"""

import re
import unicodedata

from num2words import num2words

_SENTENCE_END = re.compile(r"[.!?…]+[\"')\]]*\s")
_CLAUSE_END = re.compile(r"[,;:—–]\s")


class Chunker:
    def __init__(self, first_min_chars: int = 15, clause_min_chars: int = 100):
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


# --- Fala incompleta ----------------------------------------------------------------

# Palavras que quase nunca terminam uma frase em português: se a transcrição acaba numa delas,
# a pessoa provavelmente fez uma pausa no meio da frase. "isso" fica de fora: fecha muitas frases
# ("esquece isso", "o que é isso").
_DANGLING = {
    "e", "ou", "mas", "porque", "que", "se", "como", "quando", "onde", "qual", "quais",
    "de", "do", "da", "dos", "das", "em", "no", "na", "nos", "nas", "para", "pra", "pro", "com",
    "sem", "por", "pelo", "pela", "ao", "à", "aos", "às", "entre", "sobre", "até",
    "o", "a", "os", "as", "um", "uma", "uns", "umas", "meu", "minha", "seu", "sua", "esse", "essa",
    "este", "esta", "é", "tipo", "então",
}


def looks_incomplete(text: str) -> bool:
    text = text.strip()
    if not text:
        return False
    if text.endswith(("...", "…", ",", ":", ";", "-", "—")):
        return True
    last = re.sub(r"[^\w]", "", text.split()[-1].lower())
    return last in _DANGLING


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


# carimbo de data/hora que o modelo às vezes repete no começo da resposta ("[domingo, 11/10/2026, 21h30]")
_TIMESTAMP = re.compile(r"\[[^\]]*\d{1,2}h\d{2}[^\]]*\]")


class LeadingTimestampFilter:
    """Tira o carimbo de data/hora do começo de uma resposta em streaming.

    Precisa agir antes do Chunker: ele cortaria na vírgula de dentro do carimbo
    ("[domingo, 11/10/2026, ...") e a limpeza por trecho não o reconheceria.
    """

    def __init__(self, max_hold: int = 60):
        self._buffer = ""
        self._done = False
        self._max_hold = max_hold

    def feed(self, delta: str) -> str:
        if self._done:
            return delta
        self._buffer += delta
        head = self._buffer.lstrip()
        if not head:
            return ""
        if not head.startswith("["):
            return self._release(self._buffer)
        if "]" in head:
            return self._release(_TIMESTAMP.sub("", self._buffer, count=1).lstrip())
        if len(head) > self._max_hold:  # colchete que não é carimbo
            return self._release(self._buffer)
        return ""

    def flush(self) -> str:
        return "" if self._done else self._release(self._buffer)

    def _release(self, text: str) -> str:
        self._done = True
        self._buffer = ""
        return text


def clean(text: str) -> str:
    text = _TIMESTAMP.sub(" ", text)
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

_MONTHS = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
           "setembro", "outubro", "novembro", "dezembro"]

_IP = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")
_DATE = re.compile(r"\b(0?[1-9]|[12]\d|3[01])/(0?[1-9]|1[0-2])(?:/(\d{4}|\d{2}))?\b")
_TIME = re.compile(r"\b([01]?\d|2[0-3])(?::|h)([0-5]\d)\b")
_HOURS = re.compile(r"\b([01]?\d|2[0-3])h\b")
_PERCENT = re.compile(r"(\d+(?:[.,]\d+)?)\s?%")
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


def _date(m: re.Match) -> str:
    day = "primeiro" if int(m[1]) == 1 else _int(int(m[1]))
    words = f"{day} de {_MONTHS[int(m[2]) - 1]}"
    if m[3]:
        year = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
        words += f" de {_int(year)}"
    return words


def _measure(token: str) -> str:
    """Número de uma medida (porcentagem, unidade): ponto é decimal, como nos resultados das
    ferramentas ("25.1%"), e ",0" no fim some ("4.0%" → "quatro")."""
    token = token.replace(".", ",") if token.count(".") == 1 else token
    if "," in token:
        whole, frac = token.split(",", 1)
        if not frac.strip("0"):
            token = whole
    return _number(token)


def normalize_for_speech(text: str) -> str:
    text = _IP.sub(lambda m: " ponto ".join(_int(int(g)) for g in m.groups()), text)
    text = _DATE.sub(_date, text)
    text = _MONEY.sub(
        lambda m: _int(int(m[1].replace(".", ""))) + " reais"
        + (f" e {_int(int(m[2]))} centavos" if m[2] and int(m[2]) else ""),
        text,
    )
    text = _TIME.sub(lambda m: f"{_int(int(m[1]))} e {_int(int(m[2]))}" if int(m[2]) else f"{_int(int(m[1]))} horas", text)
    text = _HOURS.sub(lambda m: f"{_int(int(m[1]))} horas", text)
    text = _PERCENT.sub(lambda m: f"{_measure(m[1])} por cento", text)
    text = _ORDINAL.sub(lambda m: num2words(int(m[1]), lang="pt_BR", to="ordinal"), text)
    text = _UNIT.sub(lambda m: f"{_measure(m[1])} {UNITS[m[2]]}", text)
    text = _THOUSANDS.sub(lambda m: _number(m[0]), text)
    text = _DECIMAL.sub(lambda m: _number(m[0]), text)
    text = _VERSION.sub(lambda m: _number(m[0]), text)
    text = _INTEGER.sub(lambda m: _int(int(m[0])), text)
    text = _ACRONYM.sub(lambda m: ACRONYMS[m[1]], text)
    text = text.replace("&", " e ").replace("+", " mais ").replace("=", " igual a ")
    return re.sub(r"\s+", " ", text).strip()
