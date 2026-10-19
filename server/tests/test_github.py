"""Ferramenta do GitHub com a API simulada (sem rede)."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from agatha_server.tools.github import GitHubClient, GitHubConfig, _resolve, github_tools

NOW = datetime.now(timezone.utc)
REPOS = ("ReinaldoLuizSilva/agatha", "ReinaldoLuizSilva/k8s-lab")


def iso(**ago) -> str:
    return (NOW - timedelta(**ago)).isoformat().replace("+00:00", "Z")


def fake_api(request: httpx.Request) -> httpx.Response:
    assert request.headers["Authorization"] == "Bearer t0k3n"
    path = request.url.path
    if path.endswith("/xpto"):
        return httpx.Response(404, json={"message": "Not Found"})
    if path == "/repos/ReinaldoLuizSilva/agatha":
        return httpx.Response(200, json={"default_branch": "main"})
    if path.endswith("/commits"):
        assert request.url.params["sha"] == "main"
        return httpx.Response(200, json=[{"commit": {"message": "fix(server): archivelog as text\n\ncorpo",
                                                     "author": {"name": "Reinaldo", "date": iso(hours=3)}}}])
    if path.endswith("/pulls"):
        return httpx.Response(200, json=[{"title": "feat: github tool", "user": {"login": "reinaldo"}}])
    if path.endswith("/issues"):
        return httpx.Response(200, json=[{"title": "voz a 48 kHz"}, {"title": "PR", "pull_request": {}}])
    if path.endswith("/actions/runs"):
        return httpx.Response(200, json={"workflow_runs": [
            {"name": "tests", "status": "completed", "conclusion": "failure", "created_at": iso(minutes=20)}]})
    raise AssertionError(path)


def tool(api=fake_api):
    client = GitHubClient(GitHubConfig(token="t0k3n", repos=REPOS), transport=httpx.MockTransport(api))
    (repo,) = github_tools(client)
    return repo


def test_resumo_do_repositorio():
    result = asyncio.run(tool().handler({"repo": "o agatha"}))
    assert result == {
        "repositorio": "agatha", "branch": "main",
        "ultimos_commits": [{"mensagem": "fix(server): archivelog as text", "autor": "Reinaldo", "quando": "há 3 horas"}],
        "prs_abertos": [{"titulo": "feat: github tool", "de": "reinaldo"}],
        "issues_abertas": ["voz a 48 kHz"],  # o PR que a API de issues devolve fica de fora
        "ultima_ci": {"workflow": "tests", "resultado": "falhou", "quando": "há 20 minutos"},
    }


def test_repositorio_padrao_e_nome_curto():
    assert _resolve("", REPOS) == "ReinaldoLuizSilva/agatha"
    assert _resolve("K8S-LAB", REPOS) == "ReinaldoLuizSilva/k8s-lab"
    assert _resolve("k8s", REPOS) == "ReinaldoLuizSilva/k8s-lab"
    assert _resolve("outro", REPOS) is None


def test_repositorio_desconhecido_lista_os_que_existem():
    result = asyncio.run(tool().handler({"repo": "outro"}))
    assert "não conheço" in result["erro"] and result["repositorios"] == ["agatha", "k8s-lab"]


def test_token_recusado_vira_erro_explicavel():
    result = asyncio.run(tool(lambda r: httpx.Response(401, json={})).handler({}))
    assert result == {"erro": "o token do GitHub foi recusado (inválido ou expirado)"}


def test_desligado_sem_token():
    assert not GitHubConfig().enabled
    assert not GitHubConfig(token="x").enabled
    assert GitHubConfig(token="x", repos=REPOS).enabled
