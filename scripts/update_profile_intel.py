#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
START = "<!-- PROFILE-INTEL:START -->"
END = "<!-- PROFILE-INTEL:END -->"


def request_json(url: str, token: str, payload: dict | None = None) -> dict:
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
    if data.get("errors"):
        raise RuntimeError(data["errors"])
    return data["data"]


def safe_int(value: object) -> int:
    return int(value or 0)


def compact_number(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 10_000:
        return f"{value // 1000}k"
    if value >= 1_000:
        return f"{value / 1000:.1f}k"
    return str(value)


def profile_query(token: str, login: str) -> dict:
    query = """
    query($login: String!) {
      user(login: $login) {
        login
        name
        followers { totalCount }
        following { totalCount }
        repositories(first: 100, ownerAffiliations: OWNER, orderBy: {field: STARGAZERS, direction: DESC}) {
          totalCount
          nodes {
            name
            nameWithOwner
            url
            isPrivate
            isFork
            stargazerCount
            forkCount
            pushedAt
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


def follower_logins(token: str, login: str, limit: int = 30) -> list[str]:
    followers = request_json(
        f"https://api.github.com/users/{login}/followers?per_page={limit}",
        token,
    )
    return [item["login"] for item in followers[:limit] if item.get("login")]


def follower_signal(token: str, logins: list[str]) -> list[dict]:
    if not logins:
        return []
    query = """
    query($logins: [String!]!) {
      rateLimit { remaining }
      nodes(ids: []) { id }
    }
    """
    # GitHub GraphQL does not support user lookup by a login list directly, so keep
    # this deliberately small and use aliases.
    aliases = []
    variables: dict[str, str] = {}
    for index, login in enumerate(logins[:20]):
        key = f"u{index}"
        variables[key] = login
        aliases.append(
            f"""{key}: user(login: ${key}) {{
              login
              name
              url
              followers {{ totalCount }}
              repositories(first: 20, ownerAffiliations: OWNER, orderBy: {{field: STARGAZERS, direction: DESC}}) {{
                totalCount
                nodes {{ stargazerCount forkCount }}
              }}
              contributionsCollection {{
                contributionCalendar {{ totalContributions }}
              }}
            }}"""
        )
    query = "query(" + ", ".join(f"${key}: String!" for key in variables) + ") { " + " ".join(aliases) + " }"
    data = graphql(token, query, variables)
    ranked = []
    for item in data.values():
        if not item:
            continue
        repos = item["repositories"]["nodes"]
        stars = sum(safe_int(repo["stargazerCount"]) for repo in repos)
        forks = sum(safe_int(repo["forkCount"]) for repo in repos)
        contributions = safe_int(item["contributionsCollection"]["contributionCalendar"]["totalContributions"])
        followers = safe_int(item["followers"]["totalCount"])
        score = followers * 3 + stars * 2 + forks + contributions
        ranked.append(
            {
                "login": item["login"],
                "url": item["url"],
                "followers": followers,
                "stars": stars,
                "contributions": contributions,
                "score": score,
            }
        )
    return sorted(ranked, key=lambda entry: entry["score"], reverse=True)[:8]


def build_block(user: dict, followers: list[dict]) -> str:
    repos = user["repositories"]["nodes"]
    visible_repos = [repo for repo in repos if repo]
    own_repos = [repo for repo in visible_repos if not repo["isFork"]]
    stars = sum(safe_int(repo["stargazerCount"]) for repo in visible_repos)
    forks = sum(safe_int(repo["forkCount"]) for repo in visible_repos)
    repo_commits = sum(
        safe_int((((repo.get("defaultBranchRef") or {}).get("target") or {}).get("history") or {}).get("totalCount"))
        for repo in visible_repos
    )
    contributions = user["contributionsCollection"]
    calendar_days = [
        day
        for week in contributions["contributionCalendar"]["weeks"]
        for day in week["contributionDays"]
    ]
    best_day = max(calendar_days, key=lambda day: day["contributionCount"], default={"date": "n/a", "contributionCount": 0})
    active_days = sum(1 for day in calendar_days if day["contributionCount"] > 0)
    total_year = safe_int(contributions["contributionCalendar"]["totalContributions"])
    commit_contribs = safe_int(contributions["totalCommitContributions"])
    prs = safe_int(contributions["totalPullRequestContributions"])
    reviews = safe_int(contributions["totalPullRequestReviewContributions"])
    issues = safe_int(contributions["totalIssueContributions"])
    private = safe_int(contributions["restrictedContributionsCount"])
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    top_repos = sorted(
        visible_repos,
        key=lambda repo: (safe_int(repo["stargazerCount"]), safe_int(repo["forkCount"]), repo.get("pushedAt") or ""),
        reverse=True,
    )[:5]

    lines = [
        START,
        '<table align="center">',
        "  <tr>",
        f'    <td align="center"><strong>{compact_number(total_year)}</strong><br/>year contributions</td>',
        f'    <td align="center"><strong>{compact_number(commit_contribs + private)}</strong><br/>commit signal incl. private</td>',
        f'    <td align="center"><strong>{compact_number(prs)}</strong><br/>pull requests</td>',
        f'    <td align="center"><strong>{compact_number(reviews)}</strong><br/>reviews</td>',
        "  </tr>",
        "  <tr>",
        f'    <td align="center"><strong>{compact_number(issues)}</strong><br/>issues</td>',
        f'    <td align="center"><strong>{compact_number(stars)}</strong><br/>stars</td>',
        f'    <td align="center"><strong>{compact_number(forks)}</strong><br/>forks</td>',
        f'    <td align="center"><strong>{compact_number(repo_commits)}</strong><br/>indexed commits</td>',
        "  </tr>",
        "</table>",
        "",
        f'<p align="center"><sub>{active_days}/365 active days - best day {best_day["date"]} / {best_day["contributionCount"]} - {compact_number(private)} private-visible</sub></p>',
        "",
        "<details>",
        "<summary><strong>top repositories</strong></summary>",
        "",
    ]
    for repo in top_repos:
        private_label = " private" if repo["isPrivate"] else ""
        language = (repo.get("primaryLanguage") or {}).get("name") or "mixed"
        lines.append(
            f"- [{repo['nameWithOwner']}]({repo['url']}) - {compact_number(repo['stargazerCount'])} stars, "
            f"{compact_number(repo['forkCount'])} forks, {language}{private_label}"
        )
    lines.extend(
        [
            "",
            "</details>",
            "",
            '<table align="center">',
            "  <tr>",
            "    <th>follower rank</th>",
            "    <th>profile</th>",
            "    <th>followers</th>",
            "    <th>repo stars</th>",
            "    <th>year commits</th>",
            "  </tr>",
        ]
    )
    if followers:
        for rank, follower in enumerate(followers, start=1):
            lines.extend(
                [
                    "  <tr>",
                    f'    <td align="center">#{rank}</td>',
                    f'    <td align="center"><a href="{follower["url"]}">{follower["login"]}</a></td>',
                    f'    <td align="center">{compact_number(follower["followers"])}</td>',
                    f'    <td align="center">{compact_number(follower["stars"])}</td>',
                    f'    <td align="center">{compact_number(follower["contributions"])}</td>',
                    "  </tr>",
                ]
            )
    else:
        lines.extend(
            [
                "  <tr>",
                '    <td align="center">auto</td>',
                '    <td align="center">next run</td>',
                '    <td align="center">auto</td>',
                '    <td align="center">auto</td>',
                '    <td align="center">auto</td>',
                "  </tr>",
            ]
        )
    lines.extend(["</table>", "", f'<p align="center"><sub>{updated}</sub></p>', END])
    return "\n".join(lines)


def replace_block(readme: str, block: str) -> str:
    if START not in readme or END not in readme:
        return readme.rstrip() + "\n\n" + block + "\n"
    before = readme.split(START, 1)[0]
    after = readme.split(END, 1)[1]
    return before.rstrip() + "\n\n" + block + after


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    login = os.environ.get("GITHUB_USER", "kleeedolinux")
    if not token:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        return 1
    try:
        user = profile_query(token, login)
        followers = follower_signal(token, follower_logins(token, login))
        block = build_block(user, followers)
    except (HTTPError, URLError, RuntimeError, KeyError) as exc:
        print(f"Could not refresh profile intelligence: {exc}", file=sys.stderr)
        return 1
    README.write_text(replace_block(README.read_text(encoding="utf-8"), block), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
