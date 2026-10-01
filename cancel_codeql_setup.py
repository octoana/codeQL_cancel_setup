#!/usr/bin/env python3
"""Cancel active CodeQL Setup workflow runs across GitHub organization repositories."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Sequence
from urllib.parse import quote


API_VERSION = "2022-11-28"
DEFAULT_STATUSES = ("queued", "waiting", "pending", "requested", "in_progress")


class GhApiError(RuntimeError):
    """Raised when a GitHub CLI API request fails."""


@dataclass
class RateLimit:
    remaining: int
    reset_at: int


@dataclass
class RepoResult:
    repository: str
    workflow_status: str = "-"
    located: int = 0
    canceled: int = 0
    failed: int = 0
    result: str = ""


class GhApi:
    def __init__(
        self,
        reserve: int,
        check_interval: int,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.reserve = reserve
        self.check_interval = check_interval
        self.sleeper = sleeper
        self.clock = clock
        self.remaining: int | None = None
        self.reset_at: int | None = None
        self.requests_since_check = 0

    def check_authentication(self) -> None:
        completed = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise GhApiError(f"`gh` is not authenticated: {detail}")

    def request(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        expect_json: bool = True,
        check_rate_limit: bool = True,
    ) -> Any:
        if check_rate_limit:
            self.ensure_rate_limit()

        command = [
            "gh",
            "api",
            "--method",
            method,
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            f"X-GitHub-Api-Version: {API_VERSION}",
            endpoint,
        ]

        for attempt in range(3):
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode == 0:
                if check_rate_limit and self.remaining is not None:
                    self.remaining = max(0, self.remaining - 1)
                    self.requests_since_check += 1
                if not expect_json or not completed.stdout.strip():
                    return None
                try:
                    return json.loads(completed.stdout)
                except json.JSONDecodeError as error:
                    raise GhApiError(
                        f"GitHub returned invalid JSON for {method} {endpoint}: {error}"
                    ) from error

            detail = completed.stderr.strip() or completed.stdout.strip()
            if attempt < 2 and self._is_rate_limit_error(detail):
                if check_rate_limit:
                    self._wait_after_rate_limit(attempt)
                else:
                    delay = 60 * (attempt + 1)
                    print(
                        f"GitHub rate-limit endpoint was throttled; retrying in "
                        f"{delay}s.",
                        file=sys.stderr,
                    )
                    self.sleeper(delay)
                continue
            raise GhApiError(
                f"GitHub API request failed for {method} {endpoint}: {detail}"
            )

        raise GhApiError(f"GitHub API request failed for {method} {endpoint}")

    def get_rate_limit(self) -> RateLimit:
        payload = self.request("/rate_limit", check_rate_limit=False)
        try:
            core = payload["resources"]["core"]
            return RateLimit(
                remaining=int(core["remaining"]),
                reset_at=int(core["reset"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise GhApiError("GitHub returned an unexpected rate-limit response") from error

    def ensure_rate_limit(self) -> None:
        should_refresh = (
            self.remaining is None
            or self.reset_at is None
            or self.requests_since_check >= self.check_interval
        )
        if should_refresh:
            self._refresh_rate_limit()

        if self.remaining is not None and self.remaining <= self.reserve:
            self._sleep_until_reset()
            self._refresh_rate_limit()
            if self.remaining is not None and self.remaining <= self.reserve:
                raise GhApiError(
                    "GitHub API rate limit is still below the configured reserve "
                    f"({self.remaining} remaining, reserve {self.reserve})"
                )

    def _refresh_rate_limit(self) -> None:
        limit = self.get_rate_limit()
        self.remaining = limit.remaining
        self.reset_at = limit.reset_at
        self.requests_since_check = 0

    def _sleep_until_reset(self) -> None:
        if self.reset_at is None:
            return
        delay = max(1, self.reset_at - int(self.clock()) + 1)
        reset_time = datetime.fromtimestamp(self.reset_at, timezone.utc).isoformat()
        print(
            f"Rate-limit reserve reached; sleeping {delay}s until {reset_time}.",
            file=sys.stderr,
        )
        self.sleeper(delay)

    def _wait_after_rate_limit(self, attempt: int) -> None:
        try:
            self._refresh_rate_limit()
        except GhApiError:
            pass

        if self.remaining is not None and self.remaining <= self.reserve:
            self._sleep_until_reset()
        else:
            delay = 60 * (attempt + 1)
            print(
                f"GitHub secondary rate limit detected; retrying in {delay}s.",
                file=sys.stderr,
            )
            self.sleeper(delay)

    @staticmethod
    def _is_rate_limit_error(detail: str) -> bool:
        lowered = detail.lower()
        return (
            "rate limit" in lowered
            or "secondary rate" in lowered
            or "http 429" in lowered
            or "status code 429" in lowered
            or "abuse detection" in lowered
        )


def list_repositories(api: GhApi, organization: str, limit: int) -> list[str]:
    repositories: list[str] = []
    page = 1
    encoded_org = quote(organization, safe="")

    while len(repositories) < limit:
        payload = api.request(
            f"/orgs/{encoded_org}/repos?type=all&per_page=100&page={page}"
        )
        if not isinstance(payload, list):
            raise GhApiError("GitHub returned an unexpected repository-list response")

        repositories.extend(
            repository["name"]
            for repository in payload
            if isinstance(repository, dict) and isinstance(repository.get("name"), str)
        )
        if len(payload) < 100:
            break
        page += 1

    return repositories[:limit]


def list_matching_runs(
    api: GhApi,
    organization: str,
    repository: str,
    workflow_name: str,
    statuses: Sequence[str],
) -> list[dict[str, Any]]:
    matches: dict[int, dict[str, Any]] = {}
    encoded_org = quote(organization, safe="")
    encoded_repo = quote(repository, safe="")

    for status in statuses:
        page = 1
        while True:
            payload = api.request(
                f"/repos/{encoded_org}/{encoded_repo}/actions/runs"
                f"?status={quote(status, safe='')}&per_page=100&page={page}"
            )
            if not isinstance(payload, dict) or not isinstance(
                payload.get("workflow_runs"), list
            ):
                raise GhApiError(
                    f"GitHub returned an unexpected workflow-run response for "
                    f"{organization}/{repository}"
                )

            runs = payload["workflow_runs"]
            for run in runs:
                if (
                    isinstance(run, dict)
                    and run.get("name") == workflow_name
                    and isinstance(run.get("id"), int)
                ):
                    matches[run["id"]] = run

            if len(runs) < 100:
                break
            page += 1

    return list(matches.values())


def inspect_and_cancel(
    api: GhApi,
    organization: str,
    repository: str,
    workflow_name: str,
    statuses: Sequence[str],
    dry_run: bool,
) -> RepoResult:
    full_name = f"{organization}/{repository}"
    result = RepoResult(repository=full_name)

    try:
        runs = list_matching_runs(
            api, organization, repository, workflow_name, statuses
        )
    except GhApiError as error:
        result.failed = 1
        result.result = f"Inspection failed: {error}"
        return result

    result.located = len(runs)
    if not runs:
        result.result = "No matching run"
        return result

    result.workflow_status = ", ".join(
        sorted(
            {
                str(run["status"])
                for run in runs
                if isinstance(run.get("status"), str)
            }
        )
    ) or "unknown"

    if dry_run:
        result.result = "Dry run; no cancellation requested"
        return result

    encoded_org = quote(organization, safe="")
    encoded_repo = quote(repository, safe="")
    failures: list[str] = []
    for run in runs:
        run_id = run["id"]
        try:
            api.request(
                f"/repos/{encoded_org}/{encoded_repo}/actions/runs/{run_id}/cancel",
                method="POST",
                expect_json=False,
            )
            result.canceled += 1
        except GhApiError as error:
            result.failed += 1
            failures.append(f"run {run_id}: {error}")

    if failures:
        result.result = "; ".join(failures)
    else:
        result.result = "Cancellation accepted"
    return result


def print_report(results: Sequence[RepoResult], dry_run: bool) -> None:
    headers = (
        "Repository",
        "Workflow status",
        "Located",
        "Cancel accepted",
        "Failed",
        "Result",
    )
    rows = [
        (
            result.repository,
            result.workflow_status,
            str(result.located),
            str(result.canceled),
            str(result.failed),
            result.result,
        )
        for result in results
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        if rows
        else len(headers[index])
        for index in range(len(headers))
    ]

    print("\nReport")
    print(
        " | ".join(
            headers[index].ljust(widths[index]) for index in range(len(headers))
        )
    )
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(
            " | ".join(row[index].ljust(widths[index]) for index in range(len(headers)))
        )

    located = sum(result.located for result in results)
    canceled = sum(result.canceled for result in results)
    failed = sum(result.failed for result in results)
    action = (
        "would receive cancellation requests"
        if dry_run
        else "cancellation requests accepted"
    )
    print(
        f"\nScanned {len(results)} repositories; located {located} matching runs; "
        f"{canceled if not dry_run else located} {action}; {failed} failures."
    )


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Cancel active 'CodeQL Setup' workflow runs across a "
            "limited number of repositories in a GitHub organization."
        )
    )
    parser.add_argument("organization", help="GitHub organization login")
    parser.add_argument(
        "--limit",
        type=positive_int,
        default=10,
        help="maximum repositories to inspect (default: 10)",
    )
    parser.add_argument(
        "--workflow-name",
        default="CodeQL Setup",
        help="exact workflow run name to match (default: CodeQL Setup)",
    )
    parser.add_argument(
        "--status",
        dest="statuses",
        action="append",
        choices=("queued", "waiting", "pending", "requested", "in_progress"),
        help=(
            "run status to inspect; repeatable "
            "(default: all non-completed statuses)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report matching runs without canceling them",
    )
    parser.add_argument(
        "--rate-limit-reserve",
        type=nonnegative_int,
        default=100,
        help="pause when core API requests remaining reach this number (default: 100)",
    )
    parser.add_argument(
        "--rate-check-interval",
        type=positive_int,
        default=25,
        help="refresh rate-limit data after this many API requests (default: 25)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    statuses = args.statuses or list(DEFAULT_STATUSES)
    api = GhApi(args.rate_limit_reserve, args.rate_check_interval)

    try:
        api.check_authentication()
        initial_limit = api.get_rate_limit()
        api.remaining = initial_limit.remaining
        api.reset_at = initial_limit.reset_at
        print(
            f"Core API rate limit: {initial_limit.remaining} requests remaining. "
            f"Scanning up to {args.limit} repositories in {args.organization}."
        )
        repositories = list_repositories(api, args.organization, args.limit)
    except (GhApiError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2

    results: list[RepoResult] = []
    for index, repository in enumerate(repositories, start=1):
        print(
            f"[{index}/{len(repositories)}] Inspecting "
            f"{args.organization}/{repository}..."
        )
        results.append(
            inspect_and_cancel(
                api,
                args.organization,
                repository,
                args.workflow_name,
                statuses,
                args.dry_run,
            )
        )

    print_report(results, args.dry_run)
    return 1 if any(result.failed for result in results) else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted; no further repositories will be processed.", file=sys.stderr)
        raise SystemExit(130)
