"""Ferramenta do GitHub (ADR-020): somente leitura, com um token fine-grained do Reinaldo.

- github_repo(repo?): últimos commits do branch principal, PRs e issues abertos e a última CI.

O token (`AGATHA_GITHUB_TOKEN`) só tem leitura de Actions, Commit statuses, Contents, Issues e Pull
requests nos repositórios que ele escolheu; `AGATHA_GITHUB_REPOS` lista esses mesmos repositórios
("dono/nome", separados por vírgula). O nome curto basta na pergunta ("o agatha").
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from .base import Tool

log = logging.getLogger(__name__)

API = "https://api.github.com"
CONCLUSIONS = {"success": "passou", "failure": "falhou", "cancelled": "cancelada", "skipped": "pulada",
               "timed_out": "estourou o tempo", "action_required": "esperando aprovação"}


@dataclass(frozen=True)
class GitHubConfig:
    token: str = ""
    repos: tuple[str, ...] = field(default_factory=tuple)  # "dono/nome"

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.repos)


class GitHubError(Exception):
    pass


class GitHubClient:
    """Cliente HTTP síncrono, numa thread (`asyncio.to_thread`), com uma conexão reaproveitada
    (keep-alive). Medido em 19/10: o ping não perde nada, mas conexões TCP novas para o GitHub às vezes
    não abrem (mais com várias ao mesmo tempo); a assíncrona do httpx estourava o prazo 6 vezes em 30.
    Por isso: uma conexão só, consultas em sequência, prazo curto e até 3 tentativas."""

    def __init__(self, config: GitHubConfig, transport: httpx.BaseTransport | None = None):
        self.config = config
        self._client = httpx.Client(
            base_url=API, timeout=httpx.Timeout(6.0, connect=2.5), transport=transport,
            headers={"Authorization": f"Bearer {config.token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "agatha-srv"},
        )

    async def get(self, path: str, **params):
        return await asyncio.to_thread(self._get, path, params)

    def _get(self, path: str, params: dict):
        # GET é idempotente: repetir em falha de conexão ou de leitura é seguro
        for attempt in (1, 2, 3):
            try:
                r = self._client.get(path, params=params)
                break
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                if attempt == 3:
                    raise GitHubError(f"sem conexão com o GitHub ({type(e).__name__})") from e
                time.sleep(0.3)
            except httpx.HTTPError as e:
                raise GitHubError(f"sem conexão com o GitHub ({type(e).__name__})") from e
        if r.status_code == 401:
            raise GitHubError("o token do GitHub foi recusado (inválido ou expirado)")
        if r.status_code in (403, 404):
            raise GitHubError("repositório não encontrado ou o token não tem acesso a ele")
        r.raise_for_status()
        return r.json()

    async def aclose(self) -> None:
        self._client.close()


def _ago(iso: str, now: datetime) -> str:
    """"há 5 minutos", "há 3 horas", "há 2 dias": pronto para a fala."""
    seconds = (now - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds()
    for unit, size in (("dia", 86400), ("hora", 3600), ("minuto", 60)):
        if seconds >= size:
            n = int(seconds // size)
            return f"há {n} {unit}{'s' if n > 1 else ''}"
    return "agora há pouco"


def _resolve(name: str, repos: tuple[str, ...]) -> str | None:
    if not name:
        return repos[0]
    name = name.strip().lower().removeprefix("o ").removeprefix("repositório ").strip()
    for repo in repos:
        if name in (repo.lower(), repo.split("/")[-1].lower()):
            return repo
    matches = [r for r in repos if name in r.lower()]
    return matches[0] if len(matches) == 1 else None


def github_tools(client: GitHubClient) -> list[Tool]:
    repos = client.config.repos

    async def repo_status(args: dict) -> dict:
        repo = _resolve(args.get("repo") or "", repos)
        if repo is None:
            return {"erro": f"não conheço o repositório {args.get('repo')!r}",
                    "repositorios": [r.split("/")[-1] for r in repos]}
        try:
            info = await client.get(f"/repos/{repo}")
            branch = info["default_branch"]
            # em sequência, na mesma conexão: abrir várias ao mesmo tempo aumentava as falhas
            commits = await client.get(f"/repos/{repo}/commits", sha=branch, per_page=3)
            # a API de issues devolve os PRs abertos junto: uma consulta a menos
            items = await client.get(f"/repos/{repo}/issues", state="open", per_page=20)
            runs = await client.get(f"/repos/{repo}/actions/runs", branch=branch, per_page=1)
        except GitHubError as e:
            log.warning("github_repo falhou: %s", e)
            return {"erro": str(e)}
        now = datetime.now(timezone.utc)
        pulls = [i for i in items if "pull_request" in i]
        issues = [i for i in items if "pull_request" not in i]
        result = {
            "repositorio": repo.split("/")[-1],
            "branch": branch,
            "ultimos_commits": [
                {"mensagem": c["commit"]["message"].splitlines()[0][:100], "autor": c["commit"]["author"]["name"],
                 "quando": _ago(c["commit"]["author"]["date"], now)}
                for c in commits
            ],
            "prs_abertos": [{"titulo": p["title"][:100], "de": p["user"]["login"]} for p in pulls[:5]],
            "issues_abertas": [i["title"][:100] for i in issues[:5]],
        }
        if runs.get("workflow_runs"):
            run = runs["workflow_runs"][0]
            status = (CONCLUSIONS.get(run["conclusion"], run["conclusion"]) if run["status"] == "completed"
                      else "rodando agora")
            result["ultima_ci"] = {"workflow": run["name"], "resultado": status, "quando": _ago(run["created_at"], now)}
        return result

    names = ", ".join(r.split("/")[-1] for r in repos)
    return [Tool(
        name="github_repo",
        description=f"Mostra um repositório dele no GitHub ({names}): últimos commits, PRs e issues abertos e a "
                    "última execução da CI.",
        handler=repo_status,
        rule="perguntas sobre os repositórios dele no GitHub, como commits, PRs, issues ou se a CI passou: use "
             "github_repo",
        properties={"repo": {"type": "string", "description": f"nome do repositório ({names}); omita para o "
                                                              f"{repos[0].split('/')[-1]}"}},
    )]
