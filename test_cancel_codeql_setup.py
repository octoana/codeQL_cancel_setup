import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from cancel_codeql_setup import (
    DEFAULT_STATUSES,
    GhApi,
    RepoResult,
    inspect_and_cancel,
    list_repositories,
    parse_args,
    print_report,
)


class FakeApi:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class RepositoryTests(unittest.TestCase):
    def test_repository_limit_controls_page_size_and_result_count(self):
        api = FakeApi(
            [
                [{"name": f"repo-{index}"} for index in range(100)],
                [{"name": f"repo-{index}"} for index in range(100, 105)],
            ]
        )

        repositories = list_repositories(api, "octo org", 105)

        self.assertEqual(105, len(repositories))
        self.assertIn("per_page=100&page=1", api.calls[0][0])
        self.assertIn("per_page=100&page=2", api.calls[1][0])
        self.assertIn("/orgs/octo%20org/repos", api.calls[0][0])


class CancellationTests(unittest.TestCase):
    def test_default_statuses_include_in_progress_runs(self):
        self.assertIn("in_progress", DEFAULT_STATUSES)

    def test_in_progress_status_can_be_selected_explicitly(self):
        args = parse_args(["octo", "--status", "in_progress"])

        self.assertEqual(["in_progress"], args.statuses)

    def test_cancels_only_exact_matching_workflow_runs(self):
        api = FakeApi(
            [
                {
                    "workflow_runs": [
                        {
                            "id": 10,
                            "name": "CodeQL Setup",
                            "status": "queued",
                        },
                        {"id": 11, "name": "CodeQL"},
                    ]
                },
                {
                    "workflow_runs": [
                        {
                            "id": 12,
                            "name": "CodeQL Setup",
                            "status": "waiting",
                        }
                    ]
                },
                None,
                None,
            ]
        )

        result = inspect_and_cancel(
            api,
            "octo",
            "repo",
            "CodeQL Setup",
            ("queued", "waiting"),
            False,
        )

        self.assertEqual(2, result.located)
        self.assertEqual(2, result.canceled)
        self.assertEqual(0, result.failed)
        self.assertEqual("queued, waiting", result.workflow_status)
        cancel_calls = [
            call for call in api.calls if call[1].get("method") == "POST"
        ]
        self.assertEqual(2, len(cancel_calls))
        self.assertTrue(cancel_calls[0][0].endswith("/10/cancel"))
        self.assertTrue(cancel_calls[1][0].endswith("/12/cancel"))

    def test_dry_run_locates_without_canceling(self):
        api = FakeApi(
            [
                {
                    "workflow_runs": [
                        {
                            "id": 10,
                            "name": "CodeQL Setup",
                            "status": "in_progress",
                        }
                    ]
                },
                {"workflow_runs": []},
            ]
        )

        result = inspect_and_cancel(
            api,
            "octo",
            "repo",
            "CodeQL Setup",
            ("queued", "waiting"),
            True,
        )

        self.assertEqual(1, result.located)
        self.assertEqual(0, result.canceled)
        self.assertEqual("in_progress", result.workflow_status)
        self.assertFalse(any(call[1].get("method") == "POST" for call in api.calls))


class RateLimitTests(unittest.TestCase):
    def test_sleeps_until_reset_when_reserve_is_reached(self):
        sleeper = Mock()
        api = GhApi(reserve=100, check_interval=25, sleeper=sleeper, clock=lambda: 1000)
        api.remaining = 100
        api.reset_at = 1009

        with patch.object(
            api,
            "get_rate_limit",
            return_value=type("Limit", (), {"remaining": 5000, "reset_at": 2000})(),
        ):
            api.ensure_rate_limit()

        sleeper.assert_called_once_with(10)
        self.assertEqual(5000, api.remaining)


class ReportTests(unittest.TestCase):
    def test_report_includes_totals(self):
        output = io.StringIO()
        results = [
            RepoResult(
                "octo/one",
                workflow_status="queued",
                located=1,
                canceled=1,
                result="Accepted",
            ),
            RepoResult("octo/two", result="No match"),
        ]

        with redirect_stdout(output):
            print_report(results, False)

        self.assertIn("octo/one", output.getvalue())
        self.assertIn("Workflow status", output.getvalue())
        self.assertIn("queued", output.getvalue())
        self.assertIn(
            "located 1 matching runs; 1 cancellation requests accepted; 0 failures",
            output.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
