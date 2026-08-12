"""Pulls closed issues + their closing commit/PR from GitHub's GraphQL API.

Batched, not real-time — this is the offline crawl the doc describes: run
once before launch, not a live scraper. GitHub's REST API rate-limits at
5,000 req/hour; GraphQL lets one request return 50 issues with their closing
event in a single round trip, which is why this uses GraphQL over REST.

Needs GITHUB_TOKEN in .env — GitHub's GraphQL API has no anonymous access,
even for public repos.
"""

import httpx
from axiom_debug.config import settings
from axiom_debug.core.logger import logger

GRAPHQL_URL = "https://api.github.com/graphql"

ISSUES_QUERY = """
query($owner: String!, $repo: String!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    issues(
      states: CLOSED
      first: 50
      after: $cursor
      orderBy: {field: UPDATED_AT, direction: DESC}
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        url
        body
        stateReason
        timelineItems(itemTypes: [CLOSED_EVENT], last: 3) {
          nodes {
            ... on ClosedEvent {
              closer {
                __typename
                ... on Commit {
                  url
                  message
                }
                ... on PullRequest {
                  url
                  title
                  merged
                }
              }
            }
          }
        }
        comments(last: 5) {
          nodes {
            body
          }
        }
      }
    }
  }
}
"""


class RawIssue:
    """One closed GitHub issue, unprocessed — extract.py turns this into an
    (problem_summary, resolution_summary) pair worth indexing.
    """

    def __init__(self, number: int, title: str, url: str, body: str,
                 closer_url: str | None, closer_text: str | None,
                 comments: list[str]):
        self.number = number
        self.title = title
        self.url = url
        self.body = body or ""
        self.closer_url = closer_url
        self.closer_text = closer_text or ""
        self.comments = comments


def _headers() -> dict[str, str]:
    if not settings.GITHUB_TOKEN:
        raise ValueError(
            "GITHUB_TOKEN is not configured. Set it in backend/.env — the "
            "indexer needs it even for public repos; GitHub's GraphQL API "
            "has no anonymous access."
        )
    return {
        "Authorization": f"Bearer {settings.GITHUB_TOKEN}",
        "Content-Type": "application/json",
    }


async def fetch_closed_issues(
    owner: str, repo: str, max_issues: int = 300
) -> list[RawIssue]:
    """Every closed issue for `owner/repo`, most recently updated first,
    up to `max_issues` — paginating 50 at a time until either the repo runs
    out of closed issues or the cap is hit.
    """
    issues: list[RawIssue] = []
    cursor = None

    async with httpx.AsyncClient(timeout=30.0) as client:
        while len(issues) < max_issues:
            response = await client.post(
                GRAPHQL_URL,
                headers=_headers(),
                json={
                    "query": ISSUES_QUERY,
                    "variables": {"owner": owner, "repo": repo, "cursor": cursor},
                },
            )
            response.raise_for_status()
            payload = response.json()

            if "errors" in payload:
                logger.error("GitHub GraphQL error for %s/%s: %s", owner, repo, payload["errors"])
                break

            repo_data = payload["data"]["repository"]
            if repo_data is None:
                logger.error("Repository %s/%s not found", owner, repo)
                break

            issue_data = repo_data["issues"]

            for node in issue_data["nodes"]:
                closer_url = None
                closer_text = None

                for event in node["timelineItems"]["nodes"]:
                    closer = event.get("closer")
                    if not closer:
                        continue
                    closer_url = closer.get("url")
                    if closer["__typename"] == "Commit":
                        closer_text = closer.get("message")
                    elif closer["__typename"] == "PullRequest":
                        closer_text = closer.get("title")

                # No commit or merged PR closed this issue — nothing to cite
                # as "the fix", so it's not useful in a fix-grounded index.
                if closer_url is None:
                    continue

                issues.append(
                    RawIssue(
                        number=node["number"],
                        title=node["title"],
                        url=node["url"],
                        body=node["body"],
                        closer_url=closer_url,
                        closer_text=closer_text,
                        comments=[c["body"] for c in node["comments"]["nodes"] if c["body"]],
                    )
                )

            if not issue_data["pageInfo"]["hasNextPage"]:
                break
            cursor = issue_data["pageInfo"]["endCursor"]

    logger.info("Fetched %d fixable closed issues from %s/%s", len(issues), owner, repo)
    return issues[:max_issues]
