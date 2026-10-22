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

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

# palavra da consulta → site da documentação oficial (a primeira que aparecer vale)
PRODUCTS = [
    (("oracle", "rman", "awr", "asm", "sqlplus", "spfile", "dataguard", "plsql"), "docs.oracle.com"),
    (("zabbix",), "zabbix.com/documentation"),
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
RESULTS = 3
SNIPPET_CHARS = 250


@dataclass(frozen=True)
class SearchConfig:
    url: str = "http://127.0.0.1:8888"  # SearXNG no pod (deploy/quadlet/agatha-search.container)
    enabled: bool = True


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


class SearchClient:
    def __init__(self, config: SearchConfig = SearchConfig(), transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        # os buscadores do SearXNG têm 3 s cada; a ferramenta inteira tem 10 s (base.TOOL_TIMEOUT_S)
        self._client = httpx.AsyncClient(base_url=config.url, timeout=httpx.Timeout(6.0, connect=1.0),
                                         transport=transport)

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
        return {"consulta": query, "resultados": [
            f"{urlsplit(f['url']).hostname.removeprefix('www.')}, {' '.join(f.get('title', '').split())}: "
            f"{_snippet(f['content'])}" for f in found]}

    async def aclose(self) -> None:
        await self._client.aclose()


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
