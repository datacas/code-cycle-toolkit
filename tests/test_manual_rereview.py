"""#346: a rereview that ran outside the runtime, recorded and closed on.

The receipt says it is manual, the command that publishes it reads the
operator and the head from the code host, and a resume accepts it only when
it is clean, confirmed and still on the current head, and only after the
required checks of that head pass.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import ci_gate as gate  # noqa: E402
import cycle_status  # noqa: E402
import manual_rereview as mr  # noqa: E402
import review_contract as contract  # noqa: E402
import run_cycle as rc  # noqa: E402
from test_ci_policy import (  # noqa: E402
    AWAITING, FAILED, FINDING, FIX, HEAD, OTHER, PASSED, PENDING,
    ParallelTestCase, published, receipt,
)

CHECKS = [{"name": "python3 -m unittest", "result": "pass",
           "evidence": "https://example.test/run/1"}]


def manual(head: str = HEAD, *, status: str = "APPROVED", operator: str = "reviewer",
           findings=(), checks=None, number: str = "4") -> dict:
    return mr.build_receipt("owner/api", number, head, operator, status,
                            list(CHECKS if checks is None else checks), list(findings))


class ManualReceiptContractTests(unittest.TestCase):
    def test_a_manual_receipt_reads_back_and_says_what_it_is(self) -> None:
        value = manual()
        (parsed,) = contract.stage_evidence(contract.format_stage_evidence(value))
        self.assertEqual(value, parsed)
        self.assertEqual("manual", contract.receipt_provenance(parsed))
        self.assertEqual(HEAD, parsed["confirmed_head"])
        self.assertTrue(parsed["attempt_id"].startswith("manual-"))

    def test_a_receipt_without_provenance_is_a_runtime_receipt(self) -> None:
        (parsed,) = contract.stage_evidence(receipt("rereview", "attempt-1", "APPROVED", HEAD))
        self.assertEqual("runtime", contract.receipt_provenance(parsed))

    def test_an_incomplete_or_inconsistent_manual_receipt_is_refused(self) -> None:
        cases = {
            "another stage": {"stage": "review"},
            "an awaiting verdict": {"status": AWAITING},
            "no operator": {"operator": " "},
            "another confirmed head": {"confirmed_head": OTHER},
            "no checks": {"checks": []},
            "a check without evidence": {"checks": [{"name": "x", "result": "pass",
                                                     "evidence": ""}]},
            "an unknown check result": {"checks": [{"name": "x", "result": "green",
                                                    "evidence": "log"}]},
            "an approval leaving a finding open": {
                "finding_outcomes": [{"id": "REV-001", "status": "open"}]},
            "an unknown provenance": {"provenance": "host"},
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                line = ("<!-- code-cycle-stage " + json.dumps({**manual(), **change})
                        + " -->")
                with self.assertRaises(contract.ContractError):
                    contract.stage_evidence(line)

    def test_a_value_that_would_end_the_comment_early_is_refused(self) -> None:
        with self.assertRaises(contract.ContractError):
            contract.format_stage_evidence(manual(checks=[
                {"name": "x --> y", "result": "pass", "evidence": "log"}]))

    def test_the_attempt_id_is_derived_from_what_is_recorded(self) -> None:
        self.assertEqual(manual()["attempt_id"], manual()["attempt_id"])
        self.assertNotEqual(manual()["attempt_id"], manual(head=FIX)["attempt_id"])
        self.assertNotEqual(manual()["attempt_id"],
                            manual(status="CHANGES_REQUESTED")["attempt_id"])


class FakeGh:
    """`gh` as `record` calls it: the user, the pull request and its comments."""

    def __init__(self, *, login="reviewer", head=HEAD, state="OPEN", comments=()):
        self.login = login
        self.head = head
        self.state = state
        self.comments = list(comments)
        self.calls: list[list[str]] = []

    def __call__(self, command, **kw):
        self.calls.append(command)
        args = command[1:]
        if args[:2] == ["api", "user"]:
            out = {"login": self.login}
        elif args[:2] == ["pr", "view"]:
            out = {"number": int(args[2]), "state": self.state, "headRefOid": self.head}
        elif "--method" in args:
            body = args[-1].removeprefix("body=")
            self.comments.append({"user": {"login": self.login}, "body": body,
                                  "html_url": f"https://github.test/c/{len(self.comments)}"})
            out = {"html_url": self.comments[-1]["html_url"]}
        else:
            out = [list(self.comments)]
        return subprocess.CompletedProcess(command, 0, json.dumps(out), "")

    def posts(self) -> int:
        return sum(1 for call in self.calls if "--method" in call)


class RecordCommandTests(unittest.TestCase):
    def record(self, gh, **kw):
        kw.setdefault("status", "APPROVED")
        kw.setdefault("confirm_head", HEAD)
        kw.setdefault("checks", list(CHECKS))
        kw.setdefault("findings", [])
        return mr.record("owner/api", "4", runner=gh, **kw)

    def test_it_publishes_one_manual_receipt_for_the_authenticated_operator(self) -> None:
        gh = FakeGh(login="Operator-1")
        result = self.record(gh)
        self.assertEqual("recorded", result["status"])
        self.assertEqual("Operator-1", result["operator"])
        (published_receipt,) = contract.stage_evidence(gh.comments[0]["body"])
        self.assertEqual("manual", published_receipt["provenance"])
        self.assertEqual("Operator-1", published_receipt["operator"])
        self.assertEqual(CHECKS, published_receipt["checks"])
        self.assertIn("not run by the runtime", gh.comments[0]["body"])
        self.assertIn("not an approval", gh.comments[0]["body"])

    def test_recording_the_same_rereview_twice_publishes_once(self) -> None:
        gh = FakeGh()
        first = self.record(gh)
        second = self.record(gh)
        self.assertEqual("already_recorded", second["status"])
        self.assertEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual(1, gh.posts())

    def test_the_same_record_by_another_account_is_not_its_record(self) -> None:
        gh = FakeGh()
        self.record(gh)
        gh.comments[0]["user"]["login"] = "someone-else"
        self.assertEqual("recorded", self.record(gh)["status"])

    def test_nothing_is_published_for_a_head_that_is_not_current(self) -> None:
        cases = {
            "another head": dict(confirm_head=OTHER),
            "a short SHA": dict(confirm_head=HEAD[:12]),
            "no check": dict(checks=[]),
        }
        for name, kw in cases.items():
            with self.subTest(case=name):
                gh = FakeGh()
                with self.assertRaises(mr.ManualRereviewError):
                    self.record(gh, **kw)
                self.assertEqual(0, gh.posts())
        gh = FakeGh(state="CLOSED")
        with self.assertRaises(mr.ManualRereviewError):
            self.record(gh)
        self.assertEqual(0, gh.posts())

    def test_a_dry_run_publishes_nothing(self) -> None:
        gh = FakeGh()
        result = self.record(gh, dry_run=True)
        self.assertEqual("dry_run", result["status"])
        self.assertIn("<!-- code-cycle-stage", result["body"])
        self.assertEqual(0, gh.posts())

    def test_checks_and_findings_are_parsed_strictly(self) -> None:
        self.assertEqual(
            {"name": "unit", "result": "pass", "evidence": "https://x.test/a:b"},
            mr.parse_check("pass:unit:https://x.test/a:b"))
        self.assertEqual({"id": "REV-002", "status": "resolved"},
                         mr.parse_finding("REV-002=resolved"))
        for bad in ("pass:unit", "green:unit:log", "pass::log"):
            with self.subTest(check=bad), self.assertRaises(mr.ManualRereviewError):
                mr.parse_check(bad)
        for bad in ("REV-1=resolved", "REV-001=fixed", "REV-001"):
            with self.subTest(finding=bad), self.assertRaises(mr.ManualRereviewError):
                mr.parse_finding(bad)


class ManualClosureTests(ParallelTestCase):
    """A resume at the rereview closes on a confirmed manual rereview, or says why not."""

    def record(self, head: str = HEAD, *, author: str = "reviewer", **kw) -> dict:
        value = manual(head, **kw)
        self.github.publish(mr.comment_body(value), author=author)
        return value

    def resume(self, **kw):
        kw.setdefault("start_from", "rereview")
        kw.setdefault("change_request_id", "4")
        return self.parallel(**kw)

    def status_file(self) -> dict:
        directory = cycle_status.status_directory(self.store.path)
        finished = [item for item in cycle_status.read_statuses(directory)
                    if item.get("finished")]
        self.assertTrue(finished, "no finished status file")
        return finished[-1]

    def test_both_policies_close_on_it_after_the_checks_pass(self) -> None:
        for policy in ("parallel", "strict"):
            with self.subTest(policy=policy):
                self.setUp()
                self.stages()
                self.github.publish(published("resolve", "RESOLVED", HEAD, "attempt-fix", 1))
                self.record()
                self.github.set_checks(HEAD, PENDING, PASSED)

                report = self.resume(ci_policy=policy)

                self.assertEqual(rc.APPROVED_END, report.status)
                self.assertEqual([], self.dispatched())
                # Never without the checks: the gate read them for that head.
                self.assertIn(("ci_read", HEAD, gate.SUCCESS), self.events())
                self.assertEqual("manual", report.review_provenance)
                self.assertIn("review_provenance: manual", report.explain())
                self.assertIn("declared by the operator, not run by the runtime",
                              report.explain())
                self.assertEqual("manual", self.closing()["review_provenance"])
                self.assertEqual("manual", self.status_file()["review_provenance"])

    def test_a_receipt_it_cannot_accept_stops_with_the_missing_step(self) -> None:
        cases = {
            "another head": (lambda: (self.record(), setattr(self.github, "head", FIX)),
                             f"--confirm-head {FIX}"),
            "changes requested": (lambda: self.record(status="CHANGES_REQUESTED"),
                                  "--from resolve --pr 4"),
            "another operator": (lambda: self.record(operator="someone"),
                                 "names operator"),
            "an unverified blocking finding": (
                lambda: (self.github.publish(published(
                    "rereview", "CHANGES_REQUESTED", HEAD, "attempt-r", 1,
                    findings=[FINDING])), self.record()),
                "REV-001"),
        }
        for name, (prepare, fragment) in cases.items():
            with self.subTest(case=name):
                self.setUp()
                self.stages()
                prepare()
                self.github.set_checks(self.github.head, PASSED)

                report = self.resume(ci_policy="strict")

                self.assertEqual(rc.UNRESOLVED_END, report.status)
                self.assertEqual("manual_rereview_rejected", report.stop_reason)
                self.assertIn("manual rereview", report.stopped_because)
                self.assertIn(fragment, report.stopped_because)
                self.assertEqual([], self.dispatched())
                self.assertEqual([], [e for e in self.github.log if e[0] == "ci_read"])
                self.assertIsNone(report.review_provenance)

    def test_a_blocking_finding_it_verified_does_not_stop_it(self) -> None:
        self.stages()
        self.github.publish(published("rereview", "CHANGES_REQUESTED", HEAD, "attempt-r", 1,
                                      findings=[FINDING]))
        self.record(findings=[{"id": "REV-001", "status": "resolved"}])
        self.github.set_checks(HEAD, PASSED)

        self.assertEqual(rc.APPROVED_END, self.resume().status)

    def test_a_runtime_receipt_published_after_it_is_what_the_resume_reads(self) -> None:
        self.stages(rereview=[(AWAITING, {})])
        self.record()
        self.github.publish(published("resolve", "RESOLVED", HEAD, "attempt-fix", 2))
        self.github.set_checks(HEAD, PASSED)

        report = self.resume()

        self.assertEqual(1, len(self.dispatched("rereview")))
        self.assertIsNone(report.review_provenance)

    def test_a_publication_during_the_wait_blocks_the_approval(self) -> None:
        self.stages()
        self.record()
        self.github.set_checks(HEAD, PENDING, PASSED)
        self.clock.on_sleep = lambda now: self.github.publish(
            receipt("rereview", "attempt-elsewhere", "APPROVED", HEAD))

        report = self.resume()

        self.assertEqual("BLOCKED", report.status)
        self.assertIn("another stage published after it", report.stopped_because)

    def test_failed_checks_are_never_bypassed(self) -> None:
        self.stages()
        self.record()
        self.github.set_checks(HEAD, FAILED)

        report = self.resume(ci_policy="strict")

        self.assertEqual(rc.UNRESOLVED_END, report.status)
        self.assertEqual("ci_not_passing", report.stop_reason)
        self.assertIn("never bypasses the required checks", report.stopped_because)
        self.assertEqual([], self.dispatched())

    def test_a_head_that_moves_during_the_wait_invalidates_the_confirmation(self) -> None:
        self.stages()
        self.record()
        self.github.set_checks(HEAD, PENDING)

        def push(now):
            self.github.head = FIX

        self.clock.on_sleep = push

        report = self.resume()

        self.assertEqual("head_moved", report.stop_reason)
        self.assertIn(f"--confirm-head {FIX}", report.stopped_because)
        self.assertEqual([], self.dispatched())

    def test_without_a_receipt_the_stop_names_it_and_the_command(self) -> None:
        self.stages(rereview=[("BLOCKED", {"publish": False})])

        report = self.resume(ci_policy="strict")

        self.assertEqual(1, len(self.dispatched("rereview")))
        self.assertIn("has no receipt until it is recorded", report.stopped_because)
        self.assertIn(f"manual_rereview.py record --repo owner/api --pr 4 --status APPROVED "
                      f"--confirm-head {HEAD}", report.stopped_because)
        self.assertIn("--from rereview --pr 4", report.stopped_because)

    def test_a_manual_receipt_is_never_reused_as_a_runtime_review(self) -> None:
        comments = [contract.ProviderComment(body=mr.comment_body(manual()),
                                             author="reviewer")]
        self.assertIsNone(rc.resolve_delta_base(comments, "owner/api", "4",
                                                ("reviewer",), None))


if __name__ == "__main__":
    unittest.main()
