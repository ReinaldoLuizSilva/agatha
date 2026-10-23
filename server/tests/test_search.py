"""Busca na documentação oficial com o SearXNG simulado (sem rede)."""

import asyncio

import httpx
import pytest

from agatha_server.tools.search import SearchClient, SearchError, passage, pick, search_request, site_for


def result(url, content="Trecho da documentação.", title="Página"):
    return {"url": url, "title": title, "content": content}


def no_pages(request):
    return httpx.Response(404)


def tool(api, pages=no_pages):
    return SearchClient(transport=httpx.MockTransport(api), pages_transport=httpx.MockTransport(pages))


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


LISTENER_PAGE = """<html><head><script>var x = "1521 no script";</script></head><body>
<nav><a href="/">Home</a> port menu</nav>
<h2>Configuring Oracle Net Listener During Installation</h2>
<p>Oracle Universal Installer launches Oracle Net Configuration Assistant during software installation.</p>
<h2>About the Default Listener</h2>
<p>By default, the listener is configured with the name LISTENER and listens on the following TCP/IP protocol address:</p>
<pre><code>(ADDRESS=(PROTOCOL=tcp)(HOST=host_name)(PORT=1521))</code></pre>
<p>The listener forwards client requests to supported services.</p>
<footer>Copyright Oracle port default</footer></body></html>"""


def test_trecho_da_pagina_em_volta_do_assunto():
    text = passage(LISTENER_PAGE, "Oracle listener porta padrão", "By default, the listener is configured with the name LISTENER")
    assert "(PORT=1521)" in text and "By default, the listener" in text
    assert "no script" not in text and "Home" not in text and "Copyright" not in text


def test_pagina_sem_os_termos_da_consulta():
    assert passage("<p>Nada a ver com o assunto.</p>", "zabbix housekeeping") is None
    assert passage("", "zabbix housekeeping") is None


def test_resultado_usa_o_trecho_da_pagina_e_o_resumo_quando_a_pagina_falha():
    def api(request):
        return httpx.Response(200, json={"results": [
            result("https://docs.oracle.com/en/database/23/netag/listener.html", "By default, the listener is configured",
                   "Configuring Oracle Net Listener"),
            result("https://docs.oracle.com/en/database/23/netag/other.html", "Resumo do buscador.", "Outra"),
        ]})

    def pages(request):
        if request.url.path.endswith("listener.html"):
            return httpx.Response(200, text=LISTENER_PAGE, headers={"Content-Type": "text/html"})
        raise httpx.ConnectTimeout("lenta")

    found = ask(tool(api, pages), "Oracle listener porta padrão")["resultados"]
    assert found[0].startswith("docs.oracle.com, Configuring Oracle Net Listener: ") and "(PORT=1521)" in found[0]
    assert found[1] == "docs.oracle.com, Outra: Resumo do buscador"
