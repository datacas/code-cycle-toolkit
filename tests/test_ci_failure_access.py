"""Failed-output reads stay scoped to the current PR head (#306)."""
import json
import fnmatch
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ci_gate as gate
import run_cycle as driver
import stage_permissions as sp

SHA = "a" * 40


class FailureAccessTests(unittest.TestCase):
    def setUp(self):
        self.policy = sp.StagePolicy("resolve", repo="owner/api", change_request_id="4")

    def check(self, command, *, head=SHA, run_head=SHA, job="123"):
        def lookup(path, field, **kw):
            if "/pulls/" in path:
                return head
            if "/jobs/" in path:
                return job
            return run_head
        with mock.patch.object(sp, "GITHUB_FIELD", side_effect=lookup):
            return sp.check(command, self.policy)

    def test_read_forms_and_argument_orders(self):
        for command in (
            "gh run view 123 --log", "gh run view --log-failed 123",
            "gh run view --job 9 123 --log-failed", "gh run view 123 -j=9 --log",
            "gh run view --json jobs 123 -j 9", "gh run view 123 --job=9 --job 10 --log",
            "gh api repos/owner/api/check-runs/8/annotations",
            "gh api --paginate repos/owner/api/check-runs/8/annotations --jq '.[].message'",
            "gh api --jq='.[].message' repos/owner/api/check-runs/8/annotations",
        ):
            with self.subTest(command=command):
                self.assertTrue(self.check(command).allowed, self.check(command).reason)

    def test_annotation_patterns_cover_orders_and_rtk_wrappers(self):
        policy = sp.StagePolicy("resolve", repo="owner/api", change_request_id="4", rtk=True)
        patterns = [p[len("Bash("):-1].replace(":*", " *")
                    for p in sp.allowed_tool_patterns(policy) if p.startswith("Bash(")]
        for options in ("repos/owner/api/check-runs/8/annotations --paginate",
                        "--paginate repos/owner/api/check-runs/8/annotations",
                        "--jq='.[].message' repos/owner/api/check-runs/8/annotations"):
            for wrapper in ("", "rtk ", "rtk proxy "):
                command = wrapper + "gh api " + options
                self.assertTrue(any(fnmatch.fnmatchcase(command, p) for p in patterns), command)

    def test_mutations_placeholders_other_repos_and_methods_are_denied(self):
        for command in (
            "gh run rerun 123", "gh run cancel 123", "gh run delete 123",
            "gh api /repos/owner/api/check-runs/8/annotations",
            "gh api repos/other/api/check-runs/8/annotations",
            "gh api 'repos/{owner}/{repo}/check-runs/8/annotations'",
            "gh api repos/owner/api/check-runs/abc/annotations",
            "gh api repos/owner/api/check-runs/8",
            *[f"gh api repos/owner/api/check-runs/8/annotations {flag}" for flag in
              ("-X GET", "--method=GET", "-f x=y", "-F x=y", "--input /tmp/x")],
            "gh api repos/owner/api/check-runs/8/annotations --repo owner/api",
        ):
            with self.subTest(command=command):
                self.assertFalse(self.check(command).allowed)

    def test_head_mismatch_and_unverifiable_head_fail_closed(self):
        for command in ("gh run view 123 --log-failed",
                        "gh api repos/owner/api/check-runs/8/annotations"):
            for fields in ({"head": "b" * 40}, {"head": None}, {"run_head": None},
                           {"run_head": "null"}):
                with self.subTest(command=command, fields=fields):
                    self.assertFalse(self.check(command, **fields).allowed)

    def test_every_job_form_needs_its_named_numeric_run(self):
        for command in ("gh run view --job 9 --log", "gh run view latest --log",
                        "gh run view 123 --job 9 --job=10 --log",
                        "gh run view 123 -j 9", "gh run view 123 --job=9"):
            with self.subTest(command=command):
                self.assertFalse(self.check(command, job="456").allowed)
                self.assertFalse(self.check(command, job=None).allowed)

    def test_implement_keeps_its_existing_run_view_behavior(self):
        policy = sp.StagePolicy("implement", repo="owner/api")
        with mock.patch.object(sp, "GITHUB_FIELD", side_effect=AssertionError):
            self.assertTrue(sp.check("gh run view --job 9 --log", policy).allowed)

    def test_head_and_job_lookups_use_scoped_read_only_api(self):
        calls = []
        def run(command, **kw):
            calls.append(command)
            value = "123" if "/jobs/" in command[2] else SHA
            return subprocess.CompletedProcess(command, 0, value, "")
        result = sp.check("gh run view 123 --job 9 --log-failed", self.policy, run=run)
        self.assertTrue(result.allowed, result.reason)
        self.assertEqual([c[2] for c in calls], ["repos/owner/api/actions/jobs/9",
                         "repos/owner/api/pulls/4", "repos/owner/api/actions/runs/123"])
        self.assertTrue(all(c[:2] == ["gh", "api"] and c[3] == "--jq" for c in calls))

    def test_gate_preserves_failed_check_and_run_ids_for_the_directive(self):
        def run(command, **kw):
            path = command[-1]
            if "check-runs?" in path:
                data = {"check_runs": [dict(id=8, name="untrusted", status="completed",
                    conclusion="failure", app={"slug": "github-actions", "id": 1},
                    details_url="https://github.com/owner/api/actions/runs/123/job/9")]}
            else:
                data = {"statuses": [dict(context="external", state="failure")]}
            return subprocess.CompletedProcess(command, 0, json.dumps([data]), "")
        checks = gate.GitHubChecks(run=run).checks("owner/api", SHA)
        self.assertEqual((8, 123), (checks[0].check_run_id, checks[0].run_id))
        result = gate.GateResult(gate.CI_FAILED, SHA, failed_checks=checks)
        items = driver.failed_check_items(result)
        self.assertEqual("check run 8 of workflow run 123", items[0])
        self.assertIn('"external"', items[1])
        self.assertEqual(123, result.to_json()["failed_ids"][0]["run_id"])

    def test_suite_fallback_does_not_guess_a_run(self):
        client = gate.GitHubChecks()
        check = {"app": {"slug": "github-actions"}, "check_suite": {"id": 7}}
        with mock.patch.object(client, "_api", return_value={"workflow_runs": [{"id": 44}]}):
            self.assertEqual(44, client._workflow_run("owner/api", check, {}))
        with mock.patch.object(client, "_api", side_effect=gate.CheckReadError("offline")):
            self.assertIsNone(client._workflow_run("owner/api", check, {}))
        self.assertIsNone(client._workflow_run("owner/api", {
            "app": {"slug": "external"},
            "details_url": "https://github.com/owner/api/actions/runs/123/job/9"}, {}))
