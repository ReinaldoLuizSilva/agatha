"""Busca na documentação oficial (Fase 10): SearXNG no próprio pod (ADR-025).

Não é uma ferramenta do LLM: com ela no tool calling, o modelo de 4B buscava em ~1 de 3 perguntas
de conceito ("pra que serve o RMAN?"), mesmo com regra e descrição dizendo "só quando ele pedir"
(ADR-025). A conversa detecta o pedido na fala (`search_request`: "pesquisa...", "procura na
documentação...", "o que mudou no Zabbix 7.0?"), busca e entrega até 3 trechos junto com a pergunta.

O produto citado na consulta escolhe o site ("site:docs.oracle.com"); sem produto conhecido, a busca
é aberta e só ficam os resultados de domínios oficiais (sem isso vinham Brainly e Medium). A mesma
página em várias versões ("/documentation/6.0/" e "/7.4/") fica só uma vez, a da versão mais nova.

Sai para a internet só a consulta, pelos buscadores do SearXNG (o Reinaldo autorizou em 21/10).
"""

import asyncio
import logging
import re
import unicodedata
from html.parser import HTMLParser
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

# palavra da consulta → site da documentação oficial (a primeira que aparecer vale)
PRODUCTS = [
    (("oracle", "rman", "awr", "asm", "sqlplus", "spfile", "dataguard", "plsql"), "docs.oracle.com"),
    (("zabbix", "housekeeping", "housekeeper"), "zabbix.com/documentation"),  # termo do Zabbix no glossário
    (("podman", "quadlet"), "docs.podman.io"),
    (("docker", "dockerfile"), "docs.docker.com"),
    (("kubernetes", "k8s", "kubectl"), "kubernetes.io/docs"),
    (("postgres", "postgresql", "psql"), "postgresql.org/docs"),
    (("mysql",), "dev.mysql.com/doc"),
    (("grafana",), "grafana.com/docs"),
    (("prometheus",), "prometheus.io/docs"),
    (("systemd", "systemctl", "journalctl"), "freedesktop.org/software/systemd"),
    (("rhel", "selinux", "firewalld", "dnf"), "docs.redhat.com"),
    (("python",), "docs.python.org"),
    (("github",), "docs.github.com"),
    (("git",), "git-scm.com/docs"),
    (("aws",), "docs.aws.amazon.com"),
    (("azure", "powershell"), "learn.microsoft.com"),
    (("gcp",), "cloud.google.com"),
    (("terraform",), "developer.hashicorp.com"),
    (("ansible",), "docs.ansible.com"),
    (("nginx",), "nginx.org"),
    (("linux", "bash"), "man7.org"),
]
OFFICIAL = {site.split("/")[0] for _, site in PRODUCTS}
# escolhem o site, mas não ajudam a achar o trecho: estão na página inteira
PRODUCT_WORDS = {k for keys, _ in PRODUCTS for k in keys}
RESULTS = 3
SNIPPET_CHARS = 250


@dataclass(frozen=True)
class SearchConfig:
    url: str = "http://127.0.0.1:8888"  # SearXNG no pod (deploy/quadlet/agatha-search.container)
    enabled: bool = True
    pages: bool = True  # ler as páginas dos resultados (ADR-026); False: só o resumo do buscador


class SearchError(Exception):
    pass


def site_for(query: str) -> str | None:
    words = set(re.findall(r"[a-z0-9]+", query.lower().replace("data guard", "dataguard")))
    for keys, site in PRODUCTS:
        if words & set(keys):
            return site
    return None


def _within(url: str, sites) -> bool:
    """O endereço é de um dos sites ("docs.oracle.com", "zabbix.com/documentation"), subdomínio incluído?"""
    parts = urlsplit(url)
    host, path = parts.hostname or "", parts.path
    for site in sites:
        site_host, _, site_path = site.partition("/")
        if (host == site_host or host.endswith("." + site_host)) and path.startswith("/" + site_path):
            return True
    return False


_VERSION = re.compile(r"/(current|latest|stable|v?(\d+(?:\.\d+)?)(?:\.\d+)*)/")


_OLD_HOST = re.compile(r"^v(\d+)-(\d+)\.")  # v1-32.docs.kubernetes.io: versão antiga, fora do ar


def _version(url: str) -> float:
    """Versão no caminho ("/documentation/7.4/", "/23/", "/v5.4.0/") ou no host (v1-32.docs...); current,
    latest, stable ou sem versão contam como a mais nova."""
    parts = urlsplit(url)
    if m := _OLD_HOST.match(parts.hostname or ""):
        return float(f"{m[1]}.{m[2]}")
    m = _VERSION.search(parts.path)
    return float(m[2]) if m and m[2] else float("inf")


def _page(url: str) -> str:
    """A página sem versão nem idioma: "zabbix.com/documentation/6.0/pt/manual/x" = ".../7.4/en/manual/x"."""
    parts = urlsplit(url)
    path = _VERSION.sub("/", parts.path)
    path = re.sub(r"/(?:en|pt|pt-br|pt_br)/", "/", path, flags=re.IGNORECASE)
    host = _OLD_HOST.sub("", parts.hostname or "").removeprefix("www.").removeprefix("docs.")
    return host + path.rstrip("/")


def _snippet(text: str) -> str:
    text = " ".join((text or "").replace("\xa0", " ").split()).removesuffix("...").rstrip(" .")
    text = re.sub(r"^\d{1,2} de \w+\.? de \d{4} · ", "", text)  # data da página, do Bing
    return text if len(text) <= SNIPPET_CHARS else text[:SNIPPET_CHARS].rsplit(" ", 1)[0] + "…"


def pick(results: list[dict], site: str | None = None) -> list[dict]:
    """Só documentação oficial (do site pedido, se houver: nem todo buscador respeita o "site:"), na
    ordem do SearXNG; de cada página, a versão mais nova."""
    best: dict[str, dict] = {}
    for r in results:
        url = r.get("url") or ""
        if not _within(url, [site] if site else OFFICIAL) or not (r.get("content") or "").strip():
            continue
        page = _page(url)
        if page not in best or _version(url) > _version(best[page]["url"]):
            best[page] = {**r, "_order": best.get(page, r).get("_order", len(best))}
    return sorted(best.values(), key=lambda r: r["_order"])


# --- Trecho da página (Fase 11) ---------------------------------------------------------------
# O resumo do buscador trazia a resposta em 5 de 14 perguntas de teste; o trecho da página, em volta
# do ponto que o resumo e a consulta indicam, traz mais (bench/search_bench.py, ADR-026).

_SKIP = {"script", "style", "nav", "header", "footer", "aside", "form", "noscript", "svg", "button", "select", "template"}
_BLOCK = {"p", "li", "pre", "tr", "dd", "dt", "h1", "h2", "h3", "h4", "h5", "h6", "div", "section", "article",
          "table", "ul", "ol", "dl", "blockquote", "br", "main", "body"}
_CELL = {"td", "th"}
# termos técnicos da consulta em português → como aparecem na documentação em inglês
_EN = {"porta": "port", "padrao": "default", "parametro": "parameter", "tamanho": "size", "ativar": "enable",
       "habilitar": "enable", "desativar": "disable", "mudou": "new", "novidades": "new", "novidade": "new",
       "historico": "history", "periodo": "period", "comando": "command", "reiniciar": "restart", "modo": "mode",
       "ativo": "active", "passivo": "passive", "diferencial": "differential", "cumulativo": "cumulative",
       "configurar": "configure", "senha": "password", "usuario": "user", "memoria": "memory", "versao": "version",
       "bloco": "block", "politica": "policy", "limite": "limit", "arquivo": "file", "tempo": "time"}
_STOP = set("a o as os de da do das dos e em no na nos nas um uma para com que como qual quais se por pelo pela "
            "the of and to in is for on with by an be or this that are as it at from".split())
# Medido em 14 perguntas (ADR-026): 3 trechos de 300 caracteres acertam 9 (o resumo, 5) com ~890
# caracteres no total; o LLM processa ~200 tokens/s, e trechos maiores não acertaram mais.
PASSAGE_CHARS = 300
QUERY_WEIGHT, SNIPPET_WEIGHT = 3, 4  # pesar mais o resumo piorou (10 → 6–9 de 14)
PAGE_TIMEOUT_S = 1.5  # o DNS do provedor perde ~5% das consultas (+1 s cada, ADR-022); a busca fica em ~2 s
PAGE_MAX_BYTES = 1_500_000


class _PageText(HTMLParser):
    """Blocos de texto da página (parágrafos, itens, linhas de tabela, código), sem menus nem scripts."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._current: list[str] = []
        self._skip = 0

    def _flush(self):
        text = " ".join(" ".join(self._current).split())
        if text:
            self.blocks.append(text)
        self._current = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
        elif tag in _BLOCK:
            self._flush()
        elif tag in _CELL:
            self._current.append("·")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK:
            self._flush()

    def handle_data(self, data):
        if not self._skip:
            self._current.append(data)

    def close(self):
        super().close()
        self._flush()


def _terms(text: str) -> set[str]:
    words = [w for w in re.findall(r"[a-z0-9_.=-]+", _plain(text)) if w not in _STOP and len(w) > 1]
    return {w.strip(".-") for w in words} | {_EN[w] for w in words if w in _EN}


def _plain(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def _has(term: str, words: set[str]) -> bool:
    return term in words or (len(term) >= 5 and any(w.startswith(term[:max(5, len(term) - 2)]) for w in words))


def passage(html_text: str, query: str, snippet: str = "") -> str | None:
    """O pedaço da página (~300 caracteres) em volta do bloco que mais tem os termos da consulta (peso 3)
    e do resumo do buscador (peso 4 × a fração dos termos dele: aponta onde o assunto está, na língua da
    página). None se a
    página não tem texto ou nada casa com a consulta."""
    parser = _PageText()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        return None
    blocks = [b for b in parser.blocks if len(b) > 1]
    query_terms, snippet_terms = _terms(query) - PRODUCT_WORDS, _terms(snippet)
    best, best_score = -1, 0.0
    for i, block in enumerate(blocks):
        words = _terms(block)
        q = sum(_has(t, words) for t in query_terms)
        if not q:
            continue
        score = QUERY_WEIGHT * q + SNIPPET_WEIGHT * len(snippet_terms & words) / max(1, len(snippet_terms)) \
            - len(block) / 4000
        if score > best_score:
            best, best_score = i, score
    if best < 0:
        return None
    # cresce para os dois lados: a resposta pode estar no bloco de código antes do parágrafo ("(PORT=1521)")
    text, i, j = blocks[best], best, best
    while len(text) < PASSAGE_CHARS and (i > 0 or j < len(blocks) - 1):
        if j < len(blocks) - 1:
            j += 1
            text += " " + blocks[j]
        if len(text) < PASSAGE_CHARS and i > 0:
            i -= 1
            text = blocks[i] + " " + text
    if len(text) > PASSAGE_CHARS:
        text = _window(text, query_terms)
    return text


def _window(text: str, terms: set[str]) -> str:
    """Corta em PASSAGE_CHARS começando um pouco antes do 1º termo da consulta."""
    low = _plain(text)  # mesmo tamanho do texto: só tira os acentos
    found = [low.find(t[:max(5, len(t) - 2)] if len(t) >= 5 else t) for t in terms]
    start = max(0, min((i for i in found if i >= 0), default=0) - 60)
    start = text.rfind(" ", 0, start) + 1 if start else 0
    cut = text[start:start + PASSAGE_CHARS]
    return ("…" if start else "") + (cut.rsplit(" ", 1)[0] + "…" if len(text) > start + PASSAGE_CHARS else cut)


class SearchClient:
    def __init__(self, config: SearchConfig = SearchConfig(), transport: httpx.AsyncBaseTransport | None = None,
                 pages_transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        # os buscadores do SearXNG têm 3 s cada; a busca inteira tem 10 s (base.TOOL_TIMEOUT_S)
        self._client = httpx.AsyncClient(base_url=config.url, timeout=httpx.Timeout(6.0, connect=1.0),
                                         transport=transport)
        # páginas da documentação: em paralelo, até PAGE_TIMEOUT_S cada; a que não vier fica com o resumo do buscador.
        # A conexão (com o DNS) pode usar o prazo todo: com 1 s, uma consulta perdida pelo DNS derrubava as páginas
        self._pages = httpx.AsyncClient(timeout=httpx.Timeout(PAGE_TIMEOUT_S), follow_redirects=True,
                                        transport=pages_transport, headers={"User-Agent": "Mozilla/5.0 agatha-srv"})

    async def _passage(self, result: dict, query: str) -> str | None:
        try:
            # o timeout do httpx é por operação (conectar, cada leitura): o prazo da página inteira é este
            text = await asyncio.wait_for(self._download(result["url"]), PAGE_TIMEOUT_S)
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            log.info("busca: página %s não veio (%s)", result["url"], type(e).__name__)
            return None
        if text is None:
            return None
        # o parse de 500 KB leva ~50 ms: numa thread, para não atrasar o áudio
        return await asyncio.to_thread(passage, text, query, result.get("content") or "")

    async def _download(self, url: str) -> str | None:
        async with self._pages.stream("GET", url) as r:
            if r.status_code >= 400 or "html" not in r.headers.get("content-type", "html"):
                return None
            body = b""
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > PAGE_MAX_BYTES:
                    break
            return body.decode(r.encoding or "utf-8", errors="replace")

    async def search(self, query: str) -> dict:
        site = site_for(query)
        try:
            r = await self._client.get("/search", params={
                "q": f"{query} site:{site}" if site else query, "format": "json", "language": "pt-BR"})
        except httpx.HTTPError as e:
            raise SearchError(f"busca fora do ar ({type(e).__name__})") from e
        if r.status_code >= 400:
            raise SearchError(f"a busca respondeu {r.status_code}")
        found = pick(r.json().get("results") or [], site)[:RESULTS]
        if not found:
            return {"consulta": query, "resultados": [], "aviso": "nada encontrado na documentação oficial"}
        texts = await asyncio.gather(*(self._passage(f, query) for f in found)) if self.config.pages else [None] * len(found)
        return {"consulta": query, "resultados": [
            f"{urlsplit(f['url']).hostname.removeprefix('www.')}, {' '.join(f.get('title', '').split())}: "
            f"{text or _snippet(f['content'])}" for f, text in zip(found, texts)]}

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._pages.aclose()


# Pedido de busca na fala. "Pesquisar" basta; "procurar" e "buscar" só com documentação, internet ou web
# ("busca os problemas do Zabbix" é da ferramenta do Zabbix); o substantivo não conta ("a pesquisa
# Datafolha"); "o que mudou" e "novidades" só com número de versão ("o que mudou no Zabbix 7.0").
_SEARCH_VERB = re.compile(r"(?<![\w-])(?<!a )(?<!uma )(?<!na )(?<!da )(?<!essa )(?<!nessa )(?<!dessa )(?<!pela )"
                          r"pesquis(?:a|e|ar|ando)\b", re.IGNORECASE)
_WHERE = r"(?:n[ao]s?|pel[ao]) (?:documenta[çc][ãa]o(?: oficial)?|internet|web)"
_FETCH = re.compile(r"\b(?:procur|busc|olh)(?:a|e|ar|que)\b.*\b" + _WHERE + r"|\b" + _WHERE
                    + r".*\b(?:procur|busc|olh)(?:a|e|ar|que)\b|\bdocumenta[çc][ãa]o\b.*\b(?:diz|fala)\b",
                    re.IGNORECASE)
_CHANGED = re.compile(r"\b(?:o que mudou|novidades?)\b.*\d", re.IGNORECASE)
_NOISE = [r"^\s*agatha\b[\s,.!]*", r"\b(?:você |voce )?(?:pode|consegue)\s+",
          r"\b(?:pesquis|procur|busc|olh)(?:a|e|ar|ando|que)\b(?:\s+(?:aí|ai|pra mim|para mim))?",
          r"\b(?:o que )?(?:a )?" + _WHERE + r"(?:\s+(?:diz|fala))?", r"\bpor favor\b", r"\b(?:pra|para) mim\b"]
_LEADING = re.compile(r"^(?:(?:d[oa]s?|de|sobre|se|e|aí|ai)\s+)+", re.IGNORECASE)
_VAGUE = {"isso", "isto", "aquilo", "essa", "esse", "ela", "ele", "aí", "ai", "sobre", "o", "a"}


def _strip(text: str) -> str:
    return " ".join(text.strip(" ?.!,;:").split()).strip(" ?.!,;:")


def search_request(text: str, previous: str | None = None) -> str | None:
    """Consulta a buscar se a fala pede busca; None se não pede. "Pesquisa isso na documentação"
    (sem assunto) busca a fala anterior."""
    if not (_SEARCH_VERB.search(text) or _FETCH.search(text) or _CHANGED.search(text)):
        return None
    query = text
    for pattern in _NOISE:
        query = re.sub(pattern, " ", query, flags=re.IGNORECASE)
    query = _strip(_LEADING.sub("", _strip(query)))
    if len([w for w in re.findall(r"\w+", query.lower()) if w not in _VAGUE]) < 2 and previous:
        query = _strip(re.sub(_NOISE[0], " ", previous, flags=re.IGNORECASE))
    return query or None
