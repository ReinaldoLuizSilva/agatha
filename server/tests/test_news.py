"""Ferramenta de notícias com os feeds simulados (sem rede)."""

import asyncio
import gzip
from datetime import datetime, timezone

import httpx

from agatha_server.tools.news import G1, TECNOBLOG, NewsClient, news_tools, section_of

NOW = datetime(2026, 10, 22, 22, 0, tzinfo=timezone.utc)


def g1_item(title, link, date, subtitle=""):
    return (f"<item><title><![CDATA[{title}]]></title><link>{link}</link><pubDate>{date}</pubDate>"
            f'<atom:subtitle>{subtitle}</atom:subtitle><description><![CDATA[<img src="x.jpg" />]]></description></item>')


G1_FEED = ('<?xml version="1.0" encoding="UTF-8"?><rss xmlns:atom="http://www.w3.org/2005/Atom"><channel>'
           + g1_item("Flávio x Lula: veja quem apoia quem no 2º turno", "https://g1.globo.com/politica/noticia/a.ghtml",
                     "Thu, 22 Oct 2026 21:23:53 -0000", "Ex-presidenciáveis se dividem.")
           + g1_item("AO VIVO: assista à programação da NSC TV", "https://g1.globo.com/sc/ao-vivo.ghtml",
                     "Thu, 22 Oct 2026 21:22:35 -0000")
           + g1_item("Suspeitos são detidos no litoral de SP; VÍDEO", "https://g1.globo.com/sp/santos/noticia/b.ghtml",
                     "Thu, 22 Oct 2026 21:21:57 -0000")
           + g1_item("Estátua de João Goulart é vandalizada em Porto Alegre; VÍDEO",
                     "https://g1.globo.com/rs/rio-grande-do-sul/noticia/c.ghtml", "Thu, 22 Oct 2026 21:00:00 -0000")
           + g1_item("Falhas no PHP levam tribunais a suspender sistemas",
                     "https://g1.globo.com/tecnologia/noticia/d.ghtml", "Thu, 22 Oct 2026 13:27:04 -0000",
                     "Vulnerabilidade permite controlar servidores sem senha. Já há atualização.")
           + "</channel></rss>").encode()

FOLHA_FEED = ('<?xml version="1.0" encoding="ISO-8859-1"?><rss version="0.91"><channel>'
              '<item><title>Bradesco segura despesas e lucro sobe para R$ 7,1 bilhões</title>'
              '<link>https://redir.folha.com.br/x</link><pubDate>22 Oct 2026 18:24:00 -0300</pubDate>'
              '<description><![CDATA[Em um cenário de cautela, o <a href="y">Bradesco</a> , segurou despesas. '
              'Segunda frase.]]></description></item>'
              '<item><title>Oração no Vaticano marca a volta do papa</title><link>https://redir.folha.com.br/z</link>'
              '<pubDate>22 Oct 2026 10:00:00 -0300</pubDate><description>Papa reza.</description></item>'
              '<item><title>Eleição de 2018 em retrospectiva</title><link>https://redir.folha.com.br/old</link>'
              '<pubDate>01 Oct 2026 10:00:00 -0300</pubDate><description>Antiga.</description></item>'
              '</channel></rss>').encode("iso-8859-1")

TECNOBLOG_FEED = ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
                  '<item><title>Hackers tentaram explorar falhas no PHP antes do alerta</title>'
                  '<link>https://tecnoblog.net/noticias/hackers-php/</link>'
                  '<pubDate>Thu, 22 Oct 2026 19:15:47 +0000</pubDate>'
                  '<description><![CDATA[<p>AWS detectou a primeira tentativa antes.</p> <p><a href="l">Leia</a></p>]]>'
                  '</description></item>'
                  '<item><title>iPhone 15 tem oferta na Amazon</title><link>https://tecnoblog.net/achados/iphone/</link>'
                  '<pubDate>Thu, 22 Oct 2026 20:19:54 +0000</pubDate><description>Oferta.</description></item>'
                  '</channel></rss>').encode()


def fake_feeds(calls, fail=()):
    def api(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if any(f in url for f in fail):
            raise httpx.ConnectError("sem rota")
        if url.startswith(G1):  # o G1 manda gzip sempre
            return httpx.Response(200, content=gzip.compress(G1_FEED), headers={"Content-Encoding": "gzip"})
        if url == TECNOBLOG:
            return httpx.Response(200, content=TECNOBLOG_FEED)
        return httpx.Response(200, content=FOLHA_FEED)
    return api


def tool(api, clock=lambda: 0.0):
    (noticias,) = news_tools(NewsClient(transport=httpx.MockTransport(api), clock=clock, now=lambda: NOW))
    return noticias


def ask(noticias, **args):
    return asyncio.run(noticias.handler(args))


def test_principais_intercala_fontes_e_filtra_regional():
    result = ask(tool(fake_feeds([])))
    assert result == {"assunto": "geral", "manchetes": [
        "G1: Flávio x Lula: veja quem apoia quem no 2º turno",
        "Folha: Bradesco segura despesas e lucro sobe para R$ 7,1 bilhões",
        "G1: Estátua de João Goulart é vandalizada em Porto Alegre",  # RS fica; SP e "ao vivo" saem
        "Folha: Oração no Vaticano marca a volta do papa",
    ]}


def test_uma_fonte_sem_prefixo_e_tecnoblog_sem_ofertas():
    result = ask(tool(fake_feeds([])), fonte="Tecnoblog")
    assert result == {"assunto": "tecnologia", "fonte": "Tecnoblog",
                      "manchetes": ["Hackers tentaram explorar falhas no PHP antes do alerta"]}


def test_secao_do_rs_so_com_noticias_do_rs():
    calls = []
    result = ask(tool(fake_feeds(calls)), assunto="notícias do Rio Grande do Sul")
    assert calls == [G1 + "rs/rio-grande-do-sul/"]
    assert result["manchetes"] == ["Estátua de João Goulart é vandalizada em Porto Alegre"]


def test_secao_que_a_fonte_nao_tem():
    assert ask(tool(fake_feeds([])), assunto="esporte", fonte="G1") == {"erro": "esporte só vem da Folha"}


def test_tema_busca_local_com_resumo():
    result = ask(tool(fake_feeds([])), assunto="a falha do PHP")
    assert result == {"tema": "a falha do PHP", "noticias": [  # mesmas palavras: a mais recente primeiro
        "Tecnoblog: Hackers tentaram explorar falhas no PHP antes do alerta. AWS detectou a primeira tentativa antes",
        "G1: Falhas no PHP levam tribunais a suspender sistemas. Vulnerabilidade permite controlar servidores sem senha",
    ]}


def test_tema_nao_casa_por_prefixo_curto_nem_noticia_antiga():
    noticias = tool(fake_feeds([]))
    assert ask(noticias, assunto="Oracle")["noticias"] == []  # não é "Oração"
    assert ask(noticias, assunto="eleição")["noticias"] == []  # a de 2018 tem 3 semanas
    assert ask(noticias, assunto="Bradesco")["noticias"] == [
        "Folha: Bradesco segura despesas e lucro sobe para R$ 7,1 bilhões. Em um cenário de cautela, o Bradesco, "
        "segurou despesas"]


def test_feeds_em_cache_por_5_minutos():
    calls, now = [], [0.0]
    noticias = tool(fake_feeds(calls), clock=lambda: now[0])
    ask(noticias)
    ask(noticias, fonte="Folha")
    assert len(calls) == 2
    now[0] = 301
    ask(noticias)
    assert len(calls) == 4


def test_fonte_fora_do_ar_vira_aviso():
    result = ask(tool(fake_feeds([], fail=("folha",))))
    assert result["aviso"] == "Folha não respondeu"
    assert result["fonte"] == "G1" and len(result["manchetes"]) == 3


def test_tudo_fora_do_ar():
    assert ask(tool(fake_feeds([], fail=("http",)))) == {"erro": "os sites de notícias não responderam"}


def test_section_of():
    assert section_of("") == "geral"
    assert section_of("as últimas notícias de hoje") == "geral"
    assert section_of("Tecnologia") == "tecnologia"
    assert section_of("política") == section_of("politica") == "política"
    assert section_of("RS") == "Rio Grande do Sul"
    assert section_of("eleição presidencial") is None
