# Cancel stuck CodeQL Setup runs

`cancel_codeql_setup.py` scans a limited number of repositories in a GitHub
organization and cancels workflow runs whose name is exactly `CodeQL Setup` and
whose status is active: `queued`, `waiting`, `pending`, `requested`, or
`in_progress`.

The script uses the existing GitHub CLI authentication on your machine. It does
not read or store a token directly.

## Requirements

- Python 3.10 or newer
- [GitHub CLI](https://cli.github.com/) authenticated with access to the target
  organization and permission to cancel Actions runs (`Actions: write` for a
  fine-grained token, or the appropriate repository access for your login)

Confirm authentication before running:

```bash
gh auth status
```

## Usage

Start with a dry run against a small number of repositories:

```bash
./cancel_codeql_setup.py YOUR_ORG --limit 5 --dry-run
```

Cancel the matching runs:

```bash
./cancel_codeql_setup.py YOUR_ORG --limit 5
```

Increase the limit after validating the report:

```bash
./cancel_codeql_setup.py YOUR_ORG --limit 500
```

Use `--status` one or more times to restrict the default set of active statuses:

```bash
./cancel_codeql_setup.py YOUR_ORG --limit 50 \
  --status queued --status waiting --status pending
```

GitHub can report a workflow run as `in_progress` while one or more of its jobs
are still running or waiting. Because `in_progress` is included by default,
always use a dry run first. Restrict inspection to queued and waiting runs when
you do not want to include actively running workflows:

```bash
./cancel_codeql_setup.py YOUR_ORG --limit 5 \
  --status queued --status waiting --dry-run
```

Run `./cancel_codeql_setup.py --help` for every option.

## REST APIs used

The script invokes these endpoints through `gh api`:

- `GET /orgs/{org}/repos` to enumerate accessible organization repositories.
- `GET /repos/{owner}/{repo}/actions/runs?status=...` to locate matching runs.
- `POST /repos/{owner}/{repo}/actions/runs/{run_id}/cancel` to request
  cancellation. GitHub returns `202 Accepted` when the cancellation request is
  accepted.
- `GET /rate_limit` to monitor the authenticated account's core REST API limit.

Both `queued` and `waiting` are checked because a workflow waiting for an
available self-hosted runner is normally reported as `queued`.

## Rate limiting and failures

The script checks the core REST API limit before scanning, refreshes it every 25
requests, and keeps a reserve of 100 requests by default. If the reserve is
reached, it waits until the reset time. It also retries primary or secondary
rate-limit failures with a delay.

Tune these safeguards if needed:

```bash
./cancel_codeql_setup.py YOUR_ORG --limit 500 \
  --rate-limit-reserve 200 --rate-check-interval 10
```

Every inspected repository appears in the final report, including the status of
matching workflow runs. Multiple statuses are comma-separated when necessary.
The process exits with status `1` when any repository inspection or cancellation
fails, `2` for a startup/authentication error, and `0` when all requested work
succeeds.
The `Cancel accepted` column counts `202 Accepted` responses; GitHub processes
those cancellation requests asynchronously.
