"""Busca na documentação oficial com o SearXNG simulado (sem rede)."""

import asyncio

import httpx
import pytest

from agatha_server.tools.search import SearchClient, SearchError, pick, search_request, site_for


def result(url, content="Trecho da documentação.", title="Página"):
    return {"url": url, "title": title, "content": content}


def tool(api):
    return SearchClient(transport=httpx.MockTransport(api))


def ask(client, consulta):
    return asyncio.run(client.search(consulta))


def test_produto_vira_site_e_resultado_compacto():
    queries = []

    def api(request):
        queries.append(request.url.params["q"])
        assert request.url.params["format"] == "json"
        return httpx.Response(200, json={"results": [
            result("https://docs.oracle.com/en/database/oracle/oracle-database/23/admin/archived.html",
                   "4 de jun. de 2026 · To switch a database's archiving mode, use the ALTER DATABASE statement...",
                   "Managing Archived Redo Log Files"),
            result("https://www.zabbix.com/documentation/7.0/en/manual/x", "Fora do assunto."),
        ]})

    assert ask(tool(api), "oracle como ativar o archivelog") == {
        "consulta": "oracle como ativar o archivelog",
        "resultados": ["docs.oracle.com, Managing Archived Redo Log Files: To switch a database's archiving mode, "
                       "use the ALTER DATABASE statement"],
    }
    assert queries == ["oracle como ativar o archivelog site:docs.oracle.com"]


def test_sem_produto_so_dominio_oficial():
    def api(request):
        assert "site:" not in request.url.params["q"]
        return httpx.Response(200, json={"results": [
            result("https://brainly.com.br/tarefa/1", "A porta é 10050."),
            result("https://medium.com/@alguem/zabbix", "Blog."),
        ]})

    assert ask(tool(api), "diferença entre TCP e UDP") == {
        "consulta": "diferença entre TCP e UDP", "resultados": [], "aviso": "nada encontrado na documentação oficial"}


def test_mesma_pagina_em_versoes_fica_a_mais_nova_na_posicao_da_primeira():
    found = pick([
        result("https://www.zabbix.com/documentation/6.0/pt/manual/appliance", "Versão 6.0"),
        result("https://docs.podman.io/en/v5.4.0/markdown/podman-systemd.unit.5.html", "Podman 5.4"),
        result("https://www.zabbix.com/documentation/7.4/en/manual/appliance", "Versão 7.4"),
        result("https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html", "Podman latest"),
        result("https://v1-32.docs.kubernetes.io/docs/reference/kubectl/rollout_restart/", "Antiga"),
        result("https://kubernetes.io/docs/reference/kubectl/rollout_restart/", "Atual"),
        result("https://docs.oracle.com/en/database/x.html", ""),  # sem trecho: não serve
    ])
    assert [r["content"] for r in found] == ["Versão 7.4", "Podman latest", "Atual"]


def test_site_for():
    assert site_for("Zabbix agent porta padrão") == "zabbix.com/documentation"
    assert site_for("apps, como funciona o housekeeping") == "zabbix.com/documentation"  # STT trocou "do Zabbix"
    assert site_for("como configurar o Data Guard") == "docs.oracle.com"
    assert site_for("github actions cache") == "docs.github.com"
    assert site_for("git rebase") == "git-scm.com/docs"
    assert site_for("diferença entre TCP e UDP") is None


def test_busca_fora_do_ar():
    def api(request):
        raise httpx.ConnectError("recusada")

    with pytest.raises(SearchError, match="busca fora do ar"):
        ask(tool(api), "zabbix proxy")


def test_pedido_de_busca_na_fala():
    assert search_request("Agatha, pesquisa na documentação do Zabbix como funciona o housekeeping.") == \
        "Zabbix como funciona o housekeeping"
    assert search_request("Procura na documentação do Oracle o comando para ativar o archivelog.") == \
        "Oracle o comando para ativar o archivelog"
    assert search_request("O que mudou no Zabbix 7.0?") == "O que mudou no Zabbix 7.0"
    assert search_request("Agatha, você pode pesquisar como configurar o DNSOption no Podman?") == \
        "como configurar o DNSOption no Podman"
    assert search_request("Busca na internet o comando kubectl rollout restart") == "o comando kubectl rollout restart"
    assert search_request("Pesquisa isso na documentação.", "Agatha, o que é o housekeeping do Zabbix?") == \
        "o que é o housekeeping do Zabbix"


def test_fala_sem_pedido_de_busca():
    for text in ["O que diz a pesquisa Datafolha?", "Busca os problemas do Zabbix.", "Qual a porta padrão do Zabbix agent?",
                 "Agatha consegue me explicar para que serve o RMAN?", "Novidades do Tecnoblog",
                 "Procura um filme de ação pra mim.", "Quais as novidades da pesquisa?"]:
        assert search_request(text) is None, text
