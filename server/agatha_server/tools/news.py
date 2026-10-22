"""Ferramenta de notícias (Fase 10): manchetes dos feeds RSS do G1, da Folha e do Tecnoblog.

- noticias(assunto?, fonte?): sem assunto, as manchetes mais recentes; com uma seção (tecnologia,
  economia, política, mundo, esporte, Rio Grande do Sul), as dela; com outro tema ("a falha do PHP"),
  as notícias recentes que falam dele, com um resumo de uma frase.

Fontes escolhidas pelo Reinaldo em 21/10. Sai para a internet só o pedido dos feeds: a busca por tema
é feita aqui, nos feeds baixados, e o tema não vai para nenhum site.
"""

import asyncio
import html
import logging
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from itertools import zip_longest

import httpx

from .base import Tool

log = logging.getLogger(__name__)

G1 = "https://g1.globo.com/rss/g1/"
FOLHA = "https://feeds.folha.uol.com.br/{}/rss091.xml"
TECNOBLOG = "https://tecnoblog.net/feed/"
RS = "Rio Grande do Sul"
SOURCES = ("G1", "Folha", "Tecnoblog")
FROM = {"G1": "do G1", "Folha": "da Folha", "Tecnoblog": "do Tecnoblog"}

# Seção → feed de cada fonte. Ficaram de fora o G1 ciência (itens de 2023 misturados) e o ge
# (quase tudo página de jogo, "acompanhe tudo sobre X x Y").
SECTIONS = {
    "geral": {"G1": G1, "Folha": FOLHA.format("emcimadahora")},
    "tecnologia": {"Tecnoblog": TECNOBLOG, "G1": G1 + "tecnologia/", "Folha": FOLHA.format("tec")},
    "economia": {"G1": G1 + "economia/", "Folha": FOLHA.format("mercado")},
    "política": {"G1": G1 + "politica/", "Folha": FOLHA.format("poder")},
    "mundo": {"G1": G1 + "mundo/", "Folha": FOLHA.format("mundo")},
    "esporte": {"Folha": FOLHA.format("esporte")},
    RS: {"G1": G1 + "rs/rio-grande-do-sul/"},
}
ALIASES = {"tec": "tecnologia", "ti": "tecnologia", "mercado": "economia", "financas": "economia",
           "esportes": "esporte", "futebol": "esporte", "internacional": "mundo", "rs": RS, "gaucho": RS,
           "gauchas": RS, "brasil": "geral", "nacional": "geral"}
# palavras do pedido que não são tema ("as últimas notícias de hoje" = sem assunto)
STOPWORDS = set("a o as os de da do das dos e em no na nos nas um uma sobre para com que se por pelo pela "
                "noticia noticias manchete manchetes ultimas ultima principais principal novidades novidade "
                "hoje agora dia aconteceu acontecendo tem teve alguma algum".split())
UFS = set("ac al ap am ba ce df es go ma mt ms mg pa pb pr pe pi rj rn rs ro rr sc sp se to".split())

FEED_TTL_S = 300
HEADLINES = 4  # por pedido; o histórico compacta o resultado depois (ADR-023)
FOUND = 3  # notícias por tema, com resumo
SEARCH_DAYS = 7


@dataclass(frozen=True)
class NewsConfig:
    enabled: bool = True


@dataclass(frozen=True)
class Item:
    source: str
    title: str
    summary: str
    link: str
    published: datetime


class NewsError(Exception):
    pass


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _plain(text))


def _text(html_text: str) -> str:
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", html_text or "")).split())
    return re.sub(r"\s+([,.;:!?])", r"\1", text)  # "Meta , Mark": sobra dos links da Folha


def _sentence(text: str, limit: int = 200) -> str:
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return first if len(first) <= limit else first[:limit].rsplit(" ", 1)[0] + "…"


def _title(title: str) -> str:
    """Sem os penduricalhos do G1: "VÍDEO:", "; VÍDEOS", "; FOTOS"."""
    title = re.sub(r"^\s*(?:VÍDEOS?|FOTOS?)\s*:\s*", "", " ".join(title.split()))
    return re.sub(r"\s*;\s*(?:VÍDEOS?|FOTOS?|VEJA)\s*$", "", title)


def _keep(source: str, feed: str, title: str, link: str) -> bool:
    path = re.sub(r"^https?://[^/]+/", "", link).split("/")
    if source == "Tecnoblog":
        return path[0] == "noticias"  # "achados" são ofertas
    if source == "G1":
        if "ao vivo" in title.lower():  # programação da TV ("EPTV 2 Campinas ao vivo")
            return False
        region = path[0] if path[0] in UFS else None
        return region == "rs" if feed == SECTIONS[RS]["G1"] else region in (None, "rs")
    return True


def parse(source: str, feed: str, content: bytes) -> list[Item]:
    root = ET.fromstring(content)
    items = []
    for el in root.iter("item"):
        title, link = _title(el.findtext("title") or ""), (el.findtext("link") or "").strip()
        if not title or not _keep(source, feed, title, link):
            continue
        try:
            published = parsedate_to_datetime(el.findtext("pubDate") or "")
        except (TypeError, ValueError):
            continue
        if published.tzinfo is None:  # o G1 usa "-0000"
            published = published.replace(tzinfo=timezone.utc)
        summary = el.findtext("{http://www.w3.org/2005/Atom}subtitle") or _text(el.findtext("description") or "")
        items.append(Item(source, title, _sentence(summary.strip()), link, published))
    return items


def section_of(subject: str) -> str | None:
    """"tecnologia", "notícias do RS", "" → a seção; um tema qualquer → None."""
    words = [w for w in _words(subject) if w not in STOPWORDS]
    if not words:
        return "geral"
    phrase = " ".join(words)
    for name in SECTIONS:
        if words == [w for w in _words(name) if w not in STOPWORDS]:
            return name
    return ALIASES.get(phrase)


def _match(query: str, word: str) -> bool:
    """Igual, plural ("falha" acha "falhas") ou, em palavra longa, o mesmo começo ("eleição" acha
    "eleições"). Com prefixo de 4 letras, "Oracle" achava "orações"."""
    if word in (query, query + "s", query + "es"):
        return True
    return len(query) >= 6 and word.startswith(query[:max(5, len(query) - 2)])


def search(items: list[Item], subject: str, now: datetime) -> list[Item]:
    """Notícias dos últimos dias que citam mais palavras do tema (no título vale o dobro)."""
    query = [w for w in _words(subject) if w not in STOPWORDS]
    scored = []
    for item in items:
        if now - item.published > timedelta(days=SEARCH_DAYS):
            continue
        title, summary = _words(item.title), _words(item.summary)
        hits = [2 * any(_match(q, w) for w in title) or any(_match(q, w) for w in summary) for q in query]
        if all(hits):
            scored.append((sum(hits), item.published, item))
    return [item for *_, item in sorted(scored, key=lambda s: (s[0], s[1]), reverse=True)]


def _dedup(items: list[Item]) -> list[Item]:
    seen, unique = set(), []
    for item in items:
        key = item.link or _plain(item.title)
        if key not in seen and _plain(item.title) not in seen:
            seen.update((key, _plain(item.title)))
            unique.append(item)
    return unique


def _interleave(lists: list[list[Item]]) -> list[Item]:
    """Uma de cada fonte por vez, na ordem das fontes da seção."""
    return [item for group in zip_longest(*lists) for item in group if item is not None]


class NewsClient:
    """Baixa os feeds em paralelo, com até 3 tentativas por conexão (como o clima, ADR-022) e cache de
    5 min por feed. O parse vai para uma thread: o feed do Tecnoblog tem ~1 MB."""

    def __init__(self, config: NewsConfig = NewsConfig(), transport: httpx.AsyncBaseTransport | None = None,
                 clock=time.monotonic, now=lambda: datetime.now(timezone.utc)):
        self.config = config
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(4.0, connect=2.5), transport=transport,
                                         follow_redirects=True, headers={"User-Agent": "agatha-srv"})
        self._clock = clock
        self._now = now
        self._feeds: dict[str, tuple[float, list[Item]]] = {}

    async def _fetch(self, source: str, url: str) -> list[Item]:
        cached = self._feeds.get(url)
        if cached and self._clock() - cached[0] < FEED_TTL_S:
            return cached[1]
        for attempt in (1, 2, 3):
            try:
                r = await self._client.get(url)
                break
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                if attempt == 3:
                    raise NewsError(f"{source} sem conexão ({type(e).__name__})") from e
                await asyncio.sleep(0.3)
            except httpx.HTTPError as e:
                raise NewsError(f"{source} sem conexão ({type(e).__name__})") from e
        if r.status_code >= 400:
            raise NewsError(f"{source} respondeu {r.status_code}")
        try:
            items = await asyncio.to_thread(parse, source, url, r.content)
        except ET.ParseError as e:
            raise NewsError(f"{source} mandou um feed inválido") from e
        self._feeds[url] = (self._clock(), items)
        return items

    async def _gather(self, feeds: list[tuple[str, str]]) -> tuple[dict[str, list[Item]], list[str]]:
        """Itens por fonte e as fontes que falharam (as outras seguem)."""
        results = await asyncio.gather(*(self._fetch(s, u) for s, u in feeds), return_exceptions=True)
        by_source: dict[str, list[Item]] = {}
        failed = []
        for (source, url), result in zip(feeds, results):
            if isinstance(result, Exception):
                log.warning("notícias: %s (%s): %s", source, url, result)
                failed.append(source)
            else:
                by_source.setdefault(source, []).extend(result)
        return by_source, sorted(set(failed) - set(by_source))

    async def news(self, subject: str = "", source: str | None = None) -> dict:
        section = section_of(subject)
        if section is None:
            return await self._search(subject, source)
        if source == "Tecnoblog" and section == "geral":
            section = "tecnologia"
        feeds = [(s, url) for s, url in SECTIONS[section].items() if source in (None, s)]
        if not feeds:
            return {"erro": f"{section} só vem {' e '.join(FROM[s] for s in SECTIONS[section])}"}
        by_source, failed = await self._gather(feeds)
        if not by_source:
            return {"erro": "os sites de notícias não responderam"}
        lists = [_dedup(sorted(items, key=lambda i: i.published, reverse=True)) for items in by_source.values()]
        chosen = _dedup(_interleave(lists))[:HEADLINES]
        result: dict = {"assunto": section}
        if len(by_source) == 1:
            result["fonte"] = next(iter(by_source))
            result["manchetes"] = [i.title for i in chosen]
        else:
            result["manchetes"] = [f"{i.source}: {i.title}" for i in chosen]
        if failed:
            result["aviso"] = f"{' e '.join(failed)} não respondeu"
        return result

    async def _search(self, subject: str, source: str | None) -> dict:
        feeds = sorted({(s, url) for sec in SECTIONS.values() for s, url in sec.items() if source in (None, s)})
        by_source, failed = await self._gather(feeds)
        if not by_source:
            return {"erro": "os sites de notícias não responderam"}
        found = _dedup(search([i for items in by_source.values() for i in items], subject, self._now()))
        result: dict = {"tema": subject, "noticias": [f"{i.source}: {i.title}. {i.summary}".rstrip(". ")
                                                       for i in found[:FOUND]]}
        if not found:
            result["aviso"] = f"nenhuma notícia dos últimos {SEARCH_DAYS} dias fala disso"
        if failed:
            result["aviso_fontes"] = f"{' e '.join(failed)} não respondeu"
        return result

    async def aclose(self) -> None:
        await self._client.aclose()


def news_tools(client: NewsClient) -> list[Tool]:
    async def noticias(args: dict) -> dict:
        subject = (args.get("assunto") or "").strip()
        source = next((s for s in SOURCES if _plain(s) == _plain(args.get("fonte") or "")), None)
        try:
            return await client.news(subject, source)
        except NewsError as e:
            log.warning("notícias falharam: %s", e)
            return {"erro": str(e)}

    return [Tool(
        name="noticias",
        description="Manchetes recentes do G1, da Folha e do Tecnoblog, de uma seção ou sobre um tema.",
        handler=noticias,
        rule="notícias e manchetes de agora: use noticias, com o assunto e a fonte só se ele disser; fatos passados, "
             "como quem ganhou uma Copa, você responde direto",
        properties={
            "assunto": {"type": "string", "description": "tecnologia, economia, política, mundo, esporte, Rio Grande "
                                                         "do Sul ou um tema, como a falha do PHP; vazio para as principais"},
            "fonte": {"type": "string", "enum": list(SOURCES)},
        },
    )]
