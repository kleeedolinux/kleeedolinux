#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "assets" / "profile"
PROJECTS_CONFIG = ROOT / "profile-special-projects.json"


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def request_json(url: str, token: str, payload: dict | None = None) -> dict | list:
    data = None
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "kleeedolinux-profile-intel",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method="POST" if payload else "GET")
    with urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def graphql(token: str, query: str, variables: dict) -> dict:
    data = request_json(
        "https://api.github.com/graphql",
        token,
        {"query": query, "variables": variables},
    )
    if isinstance(data, dict) and data.get("errors"):
        raise RuntimeError(data["errors"])
    return data["data"]


def safe_int(value: object) -> int:
    return int(value or 0)


def compact(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 10_000:
        return f"{value // 1000}k"
    if value >= 1_000:
        return f"{value / 1000:.1f}k"
    return str(value)


def profile_data(token: str, login: str) -> dict:
    query = """
    query($login: String!) {
      user(login: $login) {
        login
        followers { totalCount }
        repositories(first: 100, ownerAffiliations: OWNER, orderBy: {field: STARGAZERS, direction: DESC}) {
          totalCount
          nodes {
            nameWithOwner
            url
            isFork
            isPrivate
            stargazerCount
            forkCount
            primaryLanguage { name }
            defaultBranchRef {
              target {
                ... on Commit {
                  history(first: 0) { totalCount }
                }
              }
            }
          }
        }
        contributionsCollection {
          totalCommitContributions
          totalPullRequestContributions
          totalPullRequestReviewContributions
          totalIssueContributions
          restrictedContributionsCount
          contributionCalendar {
            totalContributions
            weeks {
              contributionDays {
                contributionCount
                date
              }
            }
          }
        }
      }
    }
    """
    return graphql(token, query, {"login": login})["user"]


def follower_logins(token: str, login: str, limit: int = 40) -> list[str]:
    followers = request_json(
        f"https://api.github.com/users/{quote(login)}/followers?per_page={limit}",
        token,
    )
    return [item["login"] for item in followers if item.get("login")]


def follower_signal(token: str, logins: list[str]) -> list[dict]:
    aliases = []
    variables: dict[str, str] = {}
    for index, login in enumerate(logins[:24]):
        key = f"u{index}"
        variables[key] = login
        aliases.append(
            f"""{key}: user(login: ${key}) {{
              login
              url
              followers {{ totalCount }}
              repositories(first: 25, ownerAffiliations: OWNER, orderBy: {{field: STARGAZERS, direction: DESC}}) {{
                nodes {{ stargazerCount forkCount }}
              }}
              contributionsCollection {{
                contributionCalendar {{ totalContributions }}
              }}
            }}"""
        )
    if not aliases:
        return []
    query = "query(" + ", ".join(f"${key}: String!" for key in variables) + ") { " + " ".join(aliases) + " }"
    data = graphql(token, query, variables)
    ranked = []
    for item in data.values():
        if not item:
            continue
        repos = item["repositories"]["nodes"]
        stars = sum(safe_int(repo["stargazerCount"]) for repo in repos)
        forks = sum(safe_int(repo["forkCount"]) for repo in repos)
        year = safe_int(item["contributionsCollection"]["contributionCalendar"]["totalContributions"])
        followers = safe_int(item["followers"]["totalCount"])
        ranked.append(
            {
                "login": item["login"],
                "url": item["url"],
                "followers": followers,
                "stars": stars,
                "forks": forks,
                "year": year,
                "score": followers * 3 + stars * 2 + forks + year,
            }
        )
    return sorted(ranked, key=lambda item: item["score"], reverse=True)[:10]


def repo_project(token: str, owner: str, name: str) -> dict:
    query = """
    query($owner: String!, $name: String!) {
      repository(owner: $owner, name: $name) {
        nameWithOwner
        url
        description
        stargazerCount
        forkCount
        isPrivate
        primaryLanguage { name }
        defaultBranchRef {
          target {
            ... on Commit {
              history(first: 0) { totalCount }
            }
          }
        }
      }
    }
    """
    repo = graphql(token, query, {"owner": owner, "name": name})["repository"]
    if not repo:
        raise RuntimeError(f"Repository not found: {owner}/{name}")
    return {
        "kind": "repo",
        "name": repo["nameWithOwner"],
        "url": repo["url"],
        "description": repo.get("description") or "repository",
        "stars": safe_int(repo["stargazerCount"]),
        "forks": safe_int(repo["forkCount"]),
        "commits": safe_int((((repo.get("defaultBranchRef") or {}).get("target") or {}).get("history") or {}).get("totalCount")),
        "language": (repo.get("primaryLanguage") or {}).get("name") or "mixed",
        "private": bool(repo["isPrivate"]),
    }


def org_project(token: str, login: str) -> dict:
    query = """
    query($login: String!) {
      organization(login: $login) {
        login
        name
        url
        description
        repositories(first: 50, orderBy: {field: STARGAZERS, direction: DESC}) {
          totalCount
          nodes {
            stargazerCount
            forkCount
            primaryLanguage { name }
            defaultBranchRef {
              target {
                ... on Commit {
                  history(first: 0) { totalCount }
                }
              }
            }
          }
        }
      }
    }
    """
    org = graphql(token, query, {"login": login})["organization"]
    if not org:
        raise RuntimeError(f"Organization not found: {login}")
    repos = org["repositories"]["nodes"]
    languages: dict[str, int] = {}
    for repo in repos:
        language = (repo.get("primaryLanguage") or {}).get("name")
        if language:
            languages[language] = languages.get(language, 0) + 1
    language = max(languages, key=languages.get) if languages else "mixed"
    return {
        "kind": "org",
        "name": org.get("name") or org["login"],
        "url": org["url"],
        "description": org.get("description") or "organization",
        "stars": sum(safe_int(repo["stargazerCount"]) for repo in repos),
        "forks": sum(safe_int(repo["forkCount"]) for repo in repos),
        "commits": sum(
            safe_int((((repo.get("defaultBranchRef") or {}).get("target") or {}).get("history") or {}).get("totalCount"))
            for repo in repos
        ),
        "language": language,
        "repos": safe_int(org["repositories"]["totalCount"]),
        "private": False,
    }


def load_projects(token: str) -> list[dict]:
    if not PROJECTS_CONFIG.exists():
        PROJECTS_CONFIG.write_text(
            json.dumps(
                {
                    "projects": [
                        {
                            "type": "repo",
                            "target": "kleeedolinux/kleeedolinux",
                            "label": "profile automation",
                        }
                    ]
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    config = json.loads(PROJECTS_CONFIG.read_text(encoding="utf-8"))
    projects = []
    for entry in config.get("projects", []):
        target = entry.get("target", "")
        kind = entry.get("type", "repo")
        try:
            if kind == "org":
                data = org_project(token, target)
            else:
                owner, name = target.split("/", 1)
                data = repo_project(token, owner, name)
            data["label"] = entry.get("label") or data["kind"]
            projects.append(data)
        except (HTTPError, RuntimeError, KeyError, ValueError) as exc:
            projects.append(
                {
                    "kind": kind,
                    "name": target,
                    "url": f"https://github.com/{target}",
                    "description": f"not reachable: {exc}",
                    "stars": 0,
                    "forks": 0,
                    "commits": 0,
                    "language": "unknown",
                    "label": entry.get("label") or kind,
                    "private": False,
                }
            )
    return projects[:8]


def svg_shell(width: int, height: int, title: str, body: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{esc(title)}">
  <defs>
    <linearGradient id="bg" x1="0" x2="1" y1="0" y2="1">
      <stop offset="0%" stop-color="#0f172a"/>
      <stop offset="52%" stop-color="#0f766e"/>
      <stop offset="100%" stop-color="#111827"/>
    </linearGradient>
    <filter id="glow"><feGaussianBlur stdDeviation="3.5" result="blur"/><feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
    <style>
      .title {{ font: 800 28px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; fill: #ecfeff; }}
      .meta {{ font: 500 12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; fill: #a7f3d0; }}
      .label {{ font: 700 13px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; fill: #ccfbf1; }}
      .value {{ font: 800 21px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; fill: #ffffff; }}
      .small {{ font: 500 12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; fill: #cbd5e1; }}
      .rank {{ font: 900 20px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; fill: #5eead4; }}
    </style>
  </defs>
  <rect width="{width}" height="{height}" rx="22" fill="url(#bg)"/>
  <rect x="1" y="1" width="{width - 2}" height="{height - 2}" rx="21" fill="none" stroke="#5eead4" stroke-opacity=".38"/>
  <circle cx="72" cy="34" r="6" fill="#5eead4" filter="url(#glow)">
    <animate attributeName="opacity" values=".35;1;.35" dur="2.4s" repeatCount="indefinite"/>
  </circle>
  <text x="92" y="43" class="title">{esc(title)}</text>
  <text x="{width - 245}" y="{height - 18}" class="meta">generated {esc(now)}</text>
{body}
</svg>
"""


def write_ledger(user: dict) -> None:
    repos = [repo for repo in user["repositories"]["nodes"] if repo]
    contribs = user["contributionsCollection"]
    calendar_days = [day for week in contribs["contributionCalendar"]["weeks"] for day in week["contributionDays"]]
    best = max(calendar_days, key=lambda day: day["contributionCount"], default={"date": "n/a", "contributionCount": 0})
    active_days = sum(1 for day in calendar_days if day["contributionCount"] > 0)
    values = [
        ("year", safe_int(contribs["contributionCalendar"]["totalContributions"])),
        ("commits+", safe_int(contribs["totalCommitContributions"]) + safe_int(contribs["restrictedContributionsCount"])),
        ("PRs", safe_int(contribs["totalPullRequestContributions"])),
        ("reviews", safe_int(contribs["totalPullRequestReviewContributions"])),
        ("issues", safe_int(contribs["totalIssueContributions"])),
        ("stars", sum(safe_int(repo["stargazerCount"]) for repo in repos)),
        ("forks", sum(safe_int(repo["forkCount"]) for repo in repos)),
        ("repos", safe_int(user["repositories"]["totalCount"])),
    ]
    cards = []
    for index, (label, value) in enumerate(values):
        x = 36 + (index % 4) * 216
        y = 82 + (index // 4) * 92
        cards.append(f'<rect x="{x}" y="{y}" width="186" height="66" rx="14" fill="#020617" fill-opacity=".42" stroke="#99f6e4" stroke-opacity=".2"/>')
        cards.append(f'<text x="{x + 16}" y="{y + 28}" class="label">{esc(label)}</text>')
        cards.append(f'<text x="{x + 16}" y="{y + 56}" class="value">{compact(value)}</text>')
    cards.append(f'<text x="38" y="286" class="small">{active_days}/365 active days - best day {esc(best["date"])} / {safe_int(best["contributionCount"])} - private-aware when PROFILE_TOKEN exists</text>')
    (ASSET_DIR / "engineering-ledger.svg").write_text(svg_shell(930, 320, "engineering ledger", "\n".join(cards)), encoding="utf-8")


def write_activity(user: dict) -> None:
    contribs = user["contributionsCollection"]
    days = [day for week in contribs["contributionCalendar"]["weeks"] for day in week["contributionDays"]]
    recent = days[-91:]
    max_count = max([safe_int(day["contributionCount"]) for day in recent] or [1])
    cells = []
    for index, day in enumerate(recent):
        col = index % 13
        row = index // 13
        value = safe_int(day["contributionCount"])
        opacity = 0.16 + (value / max_count * 0.84 if max_count else 0)
        x = 42 + col * 64
        y = 86 + row * 44
        cells.append(
            f'<rect x="{x}" y="{y}" width="46" height="28" rx="7" fill="#5eead4" opacity="{opacity:.2f}">'
            f'<animate attributeName="opacity" values=".12;{opacity:.2f}" dur="{0.5 + index * 0.015:.2f}s" fill="freeze"/></rect>'
        )
    total = safe_int(contribs["contributionCalendar"]["totalContributions"])
    cells.append(f'<text x="42" y="440" class="value">{compact(total)} contributions this year</text>')
    cells.append('<text x="42" y="468" class="small">last 91 days rendered from GitHub contribution calendar</text>')
    (ASSET_DIR / "activity-radar.svg").write_text(svg_shell(930, 500, "activity radar", "\n".join(cells)), encoding="utf-8")


def write_achievements(user: dict) -> None:
    repos = [repo for repo in user["repositories"]["nodes"] if repo]
    contribs = user["contributionsCollection"]
    stars = sum(safe_int(repo["stargazerCount"]) for repo in repos)
    forks = sum(safe_int(repo["forkCount"]) for repo in repos)
    private = safe_int(contribs["restrictedContributionsCount"])
    badges = [
        ("commit engine", safe_int(contribs["totalCommitContributions"]) + private),
        ("PR surface", safe_int(contribs["totalPullRequestContributions"])),
        ("review loop", safe_int(contribs["totalPullRequestReviewContributions"])),
        ("issue scout", safe_int(contribs["totalIssueContributions"])),
        ("star gravity", stars),
        ("fork gravity", forks),
    ]
    nodes = []
    for index, (label, value) in enumerate(badges):
        x = 64 + index * 136
        y = 178 + (index % 2) * 18
        nodes.append(f'<g transform="translate({x}, {y})">')
        nodes.append('<polygon points="44,0 57,27 87,31 65,52 70,82 44,68 18,82 23,52 1,31 31,27" fill="#5eead4" opacity=".85"><animateTransform attributeName="transform" type="scale" values=".88;1;.88" dur="2.6s" repeatCount="indefinite"/></polygon>')
        nodes.append(f'<text x="44" y="112" text-anchor="middle" class="label">{esc(label)}</text>')
        nodes.append(f'<text x="44" y="140" text-anchor="middle" class="value">{compact(value)}</text>')
        nodes.append("</g>")
    nodes.append('<text x="64" y="440" class="small">local generated trophies; no external trophy service</text>')
    (ASSET_DIR / "achievements.svg").write_text(svg_shell(930, 500, "achievement board", "\n".join(nodes)), encoding="utf-8")


def write_followers(followers: list[dict]) -> None:
    rows = []
    for index, follower in enumerate(followers[:8]):
        y = 86 + index * 48
        bar = min(430, max(24, int(follower["score"] / max(1, followers[0]["score"]) * 430))) if followers else 24
        rows.append(f'<text x="38" y="{y + 22}" class="rank">#{index + 1}</text>')
        rows.append(f'<text x="92" y="{y + 14}" class="label">{esc(follower["login"])}</text>')
        rows.append(f'<rect x="92" y="{y + 24}" width="{bar}" height="8" rx="4" fill="#5eead4" opacity=".78"><animate attributeName="width" values="24;{bar}" dur="1.3s" fill="freeze"/></rect>')
        rows.append(f'<text x="548" y="{y + 14}" class="small">{compact(follower["followers"])} followers</text>')
        rows.append(f'<text x="690" y="{y + 14}" class="small">{compact(follower["stars"])} stars</text>')
        rows.append(f'<text x="805" y="{y + 14}" class="small">{compact(follower["year"])} year</text>')
    if not rows:
        rows.append('<text x="38" y="120" class="small">waiting for first authenticated follower scan</text>')
    (ASSET_DIR / "followers-rank.svg").write_text(svg_shell(930, 500, "top followers by engineering signal", "\n".join(rows)), encoding="utf-8")


def write_projects(projects: list[dict]) -> None:
    rows = []
    for index, project in enumerate(projects[:6]):
        col = index % 2
        row = index // 2
        x = 38 + col * 426
        y = 84 + row * 126
        suffix = "private" if project.get("private") else project["kind"]
        rows.append(f'<a href="{esc(project["url"])}">')
        rows.append(f'<rect x="{x}" y="{y}" width="388" height="100" rx="16" fill="#020617" fill-opacity=".46" stroke="#99f6e4" stroke-opacity=".25"/>')
        rows.append(f'<text x="{x + 18}" y="{y + 28}" class="label">{esc(project["label"])} / {esc(suffix)}</text>')
        rows.append(f'<text x="{x + 18}" y="{y + 56}" class="value">{esc(project["name"][:28])}</text>')
        rows.append(f'<text x="{x + 18}" y="{y + 82}" class="small">{esc(project["language"])} - ★ {compact(project["stars"])} - forks {compact(project["forks"])} - commits {compact(project["commits"])}</text>')
        rows.append("</a>")
    if not rows:
        rows.append('<text x="38" y="120" class="small">add repos or orgs to profile-special-projects.json</text>')
    (ASSET_DIR / "special-projects.svg").write_text(svg_shell(930, 500, "special projects", "\n".join(rows)), encoding="utf-8")


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    login = os.environ.get("GITHUB_USER", "kleeedolinux")
    if not token:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        return 1
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    try:
        user = profile_data(token, login)
        followers = follower_signal(token, follower_logins(token, login))
        projects = load_projects(token)
        write_ledger(user)
        write_activity(user)
        write_achievements(user)
        write_followers(followers)
        write_projects(projects)
    except (HTTPError, URLError, RuntimeError, KeyError) as exc:
        print(f"Could not refresh profile assets: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
