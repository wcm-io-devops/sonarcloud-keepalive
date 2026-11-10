#!/usr/bin/env python3
"""
Keeps SonarQube Cloud projects of a GitHub organization alive.

SonarQube Cloud deletes projects on the free plan that have not been analyzed for one year.
This script identifies all non-archived repositories of a GitHub organization that have a
matching SonarQube Cloud project (key "<org>_<repo>"), and triggers the regular Maven build
workflow on the default branch for those that have not been analyzed and built recently.

A build is dispatched only if both apply:
- the last analysis of the main branch in SonarQube Cloud is older than --max-age-days
- there was no run of the build workflow on the default branch within --max-age-days

Requires a GitHub token with "Actions: write" and "Metadata: read" permissions
for all repositories of the organization in the GITHUB_TOKEN environment variable.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

GITHUB_API_URL = os.environ.get("GITHUB_API_URL", "https://api.github.com")
SONAR_API_URL = "https://sonarcloud.io/api"


class HttpError(Exception):
    def __init__(self, status, url, body):
        super().__init__(f"HTTP {status} for {url}: {body[:500]}")
        self.status = status


def http_request(url, method="GET", headers=None, data=None, retries=3):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req_headers = {"User-Agent": "wcm-io-devops-sonarcloud-keepalive", **(headers or {})}
    if body is not None:
        req_headers["Content-Type"] = "application/json"
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                content = response.read()
                return json.loads(content) if content else None
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            if (e.code == 429 or e.code >= 500) and attempt < retries:
                time.sleep(5 * (attempt + 1))
                continue
            raise HttpError(e.code, url, error_body) from e
        except urllib.error.URLError:
            if attempt < retries:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    raise AssertionError("unreachable")


class GitHub:
    def __init__(self, token):
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def get(self, path, params=None):
        url = f"{GITHUB_API_URL}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return http_request(url, headers=self.headers)

    def post(self, path, data):
        return http_request(f"{GITHUB_API_URL}{path}", method="POST", headers=self.headers, data=data)

    def list_org_repos(self, org):
        repos = []
        page = 1
        while True:
            result = self.get(f"/orgs/{org}/repos", {"type": "all", "per_page": 100, "page": page})
            repos.extend(result)
            if len(result) < 100:
                return repos
            page += 1


def list_sonar_project_keys(organization):
    """Returns all project keys of the SonarQube Cloud organization (lower-cased key -> key), or None if it does not exist."""
    keys = {}
    page = 1
    while True:
        params = urllib.parse.urlencode(
            {"organization": organization, "qualifiers": "TRK", "ps": 500, "p": page})
        try:
            result = http_request(f"{SONAR_API_URL}/components/search?{params}")
        except HttpError as e:
            if e.status == 404:
                return None
            raise
        for component in result["components"]:
            keys[component["key"].lower()] = component["key"]
        paging = result["paging"]
        if paging["pageIndex"] * paging["pageSize"] >= paging["total"]:
            return keys
        page += 1


def get_sonar_main_branch(project_key):
    """Returns (main branch name, last analysis date) of the given SonarQube Cloud project."""
    params = urllib.parse.urlencode({"project": project_key})
    result = http_request(f"{SONAR_API_URL}/project_branches/list?{params}")
    for branch in result.get("branches", []):
        if branch.get("isMain"):
            analysis_date = branch.get("analysisDate")
            return branch["name"], parse_date(analysis_date) if analysis_date else None
    return None, None


def parse_date(value):
    # SonarQube Cloud returns e.g. "2026-11-01T07:57:50+0000", GitHub e.g. "2026-11-01T07:57:50Z"
    value = value.replace("Z", "+00:00")
    if len(value) > 5 and value[-5] in "+-" and value[-3] != ":":
        value = value[:-2] + ":" + value[-2:]
    return datetime.fromisoformat(value)


def format_date(value):
    return value.strftime("%Y-%m-%d") if value else "never"


@dataclass
class Result:
    repo: str
    default_branch: str
    sonar_branch: str | None
    last_analysis: datetime | None
    action: str
    level: str = "info"  # info | warning | error


def process_repo(github, org, repo, sonar_key, workflow_file, cutoff, dry_run):
    name = repo["name"]
    default_branch = repo["default_branch"]
    sonar_branch, last_analysis = get_sonar_main_branch(sonar_key)

    def result(action, level="info"):
        return Result(name, default_branch, sonar_branch, last_analysis, action, level)

    if last_analysis and last_analysis >= cutoff:
        return result("up to date")

    if sonar_branch and sonar_branch != default_branch:
        print(f"::warning::{org}/{name}: SonarQube Cloud main branch '{sonar_branch}' "
              f"differs from GitHub default branch '{default_branch}'")

    try:
        workflow = github.get(f"/repos/{org}/{name}/actions/workflows/{workflow_file}")
    except HttpError as e:
        if e.status == 404:
            return result(f"skipped: workflow {workflow_file} not found", "warning")
        raise
    if workflow["state"] != "active":
        return result(f"skipped: workflow {workflow_file} is {workflow['state']}", "warning")

    # a recent build on the default branch that did not lead to an analysis is most likely broken -
    # report it instead of re-triggering it on every run
    runs = github.get(
        f"/repos/{org}/{name}/actions/workflows/{workflow_file}/runs",
        {
            "branch": default_branch,
            "exclude_pull_requests": "true",
            "created": f">={cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "per_page": 1,
        })
    if runs["total_count"] > 0:
        run = runs["workflow_runs"][0]
        state = run["conclusion"] or run["status"]
        return result(f"skipped: recent build without analysis ({state}, "
                      f"{format_date(parse_date(run['created_at']))}): {run['html_url']}", "warning")

    if dry_run:
        return result(f"would trigger {workflow_file} on {default_branch} (dry run)")
    github.post(f"/repos/{org}/{name}/actions/workflows/{workflow_file}/dispatches",
                {"ref": default_branch})
    return result(f"triggered {workflow_file} on {default_branch}")


def write_summary(org, results, without_sonar_count, archived_count, max_age_days, dry_run):
    lines = [
        f"## SonarQube Cloud keepalive: {org}",
        "",
        f"Max. age of last analysis: {max_age_days} days" + (" (**dry run**)" if dry_run else ""),
        "",
        f"- Repositories with SonarQube Cloud project: {len(results)}",
        f"- Archived/disabled repositories (ignored): {archived_count}",
        f"- Repositories without SonarQube Cloud project (ignored): {without_sonar_count}",
        "",
    ]
    if results:
        icons = {"info": "", "warning": "⚠️ ", "error": "❌ "}
        lines += ["| Repository | Default branch | Last analysis | Action |",
                  "|---|---|---|---|"]
        for r in results:
            sonar_url = f"https://sonarcloud.io/project/overview?id={org}_{r.repo}"
            lines.append(f"| [{r.repo}](https://github.com/{org}/{r.repo}) | {r.default_branch} "
                         f"| [{format_date(r.last_analysis)}]({sonar_url}) "
                         f"| {icons[r.level]}{r.action} |")
    summary = "\n".join(lines) + "\n"
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(summary)
    print(summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--org", required=True, help="GitHub organization (= SonarQube Cloud organization)")
    parser.add_argument("--max-age-days", type=int, default=30,
                        help="Trigger a build if there was no analysis/build within this number of days")
    parser.add_argument("--workflow-file", default="maven-build.yml",
                        help="File name of the build workflow to dispatch in each repository")
    parser.add_argument("--dry-run", action="store_true", help="Only report, do not trigger any builds")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("GITHUB_TOKEN environment variable is required")
    github = GitHub(token)
    org = args.org
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.max_age_days)

    sonar_keys = list_sonar_project_keys(org)
    if sonar_keys is None:
        print(f"::notice::SonarQube Cloud organization '{org}' does not exist - nothing to do.")
        sonar_keys = {}

    repos = github.list_org_repos(org)
    active_repos = [r for r in repos if not r.get("archived") and not r.get("disabled")]
    results = []
    without_sonar_count = 0
    for repo in sorted(active_repos, key=lambda r: r["name"].lower()):
        sonar_key = sonar_keys.get(f"{org}_{repo['name']}".lower())
        if not sonar_key:
            without_sonar_count += 1
            continue
        try:
            result = process_repo(github, org, repo, sonar_key, args.workflow_file, cutoff, args.dry_run)
        except Exception as e:  # noqa: BLE001 - continue with other repositories
            result = Result(repo["name"], repo["default_branch"], None, None, f"error: {e}", "error")
        if result.level != "info":
            print(f"::{result.level}::{org}/{result.repo}: {result.action}")
        results.append(result)

    write_summary(org, results, without_sonar_count, len(repos) - len(active_repos),
                  args.max_age_days, args.dry_run)

    if any(r.level == "error" for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
