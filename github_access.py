"""
JARVIS GitHub Access — read/write repos, PRs, issues, CI status via GitHub REST API.
"""

import asyncio
import logging
import os
from typing import Optional

import httpx

log = logging.getLogger("jarvis.github")

GITHUB_API = "https://api.github.com"


def _headers() -> dict:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GITHUB_TOKEN", "")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _resolve_repo(repo: str) -> str:
    if "/" not in repo:
        user = os.getenv("GITHUB_USER", "")
        if user:
            return f"{user}/{repo}"
    return repo


async def get_prs(repo: str, state: str = "open") -> list[dict]:
    repo = _resolve_repo(repo)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{GITHUB_API}/repos/{repo}/pulls",
                params={"state": state, "per_page": 10},
                headers=_headers(),
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        log.warning(f"get_prs({repo}) failed: {e}")
    return []


async def get_issues(repo: str, state: str = "open") -> list[dict]:
    repo = _resolve_repo(repo)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{GITHUB_API}/repos/{repo}/issues",
                params={"state": state, "per_page": 10},
                headers=_headers(),
            )
            if resp.status_code == 200:
                # GitHub returns PRs in the issues endpoint too — filter them out
                return [i for i in resp.json() if "pull_request" not in i]
    except Exception as e:
        log.warning(f"get_issues({repo}) failed: {e}")
    return []


async def get_ci_status(repo: str, branch: str = "main") -> Optional[dict]:
    """Latest CI check run status for a branch."""
    repo = _resolve_repo(repo)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{GITHUB_API}/repos/{repo}/commits/{branch}/check-runs",
                headers=_headers(),
            )
            if resp.status_code == 200:
                runs = resp.json().get("check_runs", [])
                if runs:
                    r = runs[0]
                    return {
                        "name": r.get("name"),
                        "status": r.get("status"),
                        "conclusion": r.get("conclusion"),
                    }
    except Exception as e:
        log.warning(f"get_ci_status({repo}) failed: {e}")
    return None


async def get_repo_info(repo: str) -> Optional[dict]:
    repo = _resolve_repo(repo)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{GITHUB_API}/repos/{repo}", headers=_headers())
            if resp.status_code == 200:
                d = resp.json()
                return {
                    "name": d.get("full_name"),
                    "description": d.get("description"),
                    "stars": d.get("stargazers_count"),
                    "forks": d.get("forks_count"),
                    "open_issues": d.get("open_issues_count"),
                    "default_branch": d.get("default_branch"),
                    "language": d.get("language"),
                }
    except Exception as e:
        log.warning(f"get_repo_info({repo}) failed: {e}")
    return None


async def create_issue(repo: str, title: str, body: str = "") -> Optional[dict]:
    """Create a GitHub issue. Returns the created issue dict or None."""
    repo = _resolve_repo(repo)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{GITHUB_API}/repos/{repo}/issues",
                json={"title": title, "body": body},
                headers=_headers(),
            )
            if resp.status_code == 201:
                return resp.json()
    except Exception as e:
        log.warning(f"create_issue({repo}) failed: {e}")
    return None


async def list_my_repos(per_page: int = 10) -> list[dict]:
    """List the authenticated user's repos."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{GITHUB_API}/user/repos",
                params={"sort": "updated", "per_page": per_page},
                headers=_headers(),
            )
            if resp.status_code == 200:
                return [
                    {"name": r["name"], "full_name": r["full_name"], "private": r["private"]}
                    for r in resp.json()
                ]
    except Exception as e:
        log.warning(f"list_my_repos failed: {e}")
    return []


async def get_github_status(repo: str) -> dict:
    """Comprehensive repo status: PRs + issues + CI + repo info."""
    prs, issues, ci, info = await asyncio.gather(
        get_prs(repo),
        get_issues(repo),
        get_ci_status(repo),
        get_repo_info(repo),
        return_exceptions=True,
    )
    return {
        "prs": prs if isinstance(prs, list) else [],
        "issues": issues if isinstance(issues, list) else [],
        "ci": ci if isinstance(ci, dict) else None,
        "info": info if isinstance(info, dict) else None,
    }


def format_github_for_voice(data: dict, repo: str) -> str:
    prs = data.get("prs", [])
    issues = data.get("issues", [])
    ci = data.get("ci")
    info = data.get("info")

    parts = []

    if prs:
        pr_summary = ", ".join(p["title"][:50] for p in prs[:3])
        parts.append(f"{len(prs)} open PR{'s' if len(prs) != 1 else ''}: {pr_summary}")
    else:
        parts.append("No open PRs")

    if issues:
        issue_summary = ", ".join(i["title"][:50] for i in issues[:3])
        parts.append(f"{len(issues)} open issue{'s' if len(issues) != 1 else ''}: {issue_summary}")
    else:
        parts.append("no open issues")

    if ci:
        conclusion = ci.get("conclusion") or ci.get("status", "unknown")
        parts.append(f"CI is {conclusion}")

    if info and info.get("stars"):
        parts.append(f"{info['stars']} stars")

    return ". ".join(parts) + ", sir." if parts else f"No data found for {repo}, sir."
