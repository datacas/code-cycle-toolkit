"""Recovery crosses the real process boundary against a disposable Git/gh fixture."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_run_cycle import RunCycleTestCase, Talker, block
import run_cycle as rc
import review_contract as contract
import telemetry as tm


class Worker(Talker):
    def __init__(self, name, callback):
        super().__init__(name)
        self.callback = callback

    def spoken(self, task):
        return self.callback(task)


@unittest.skipIf(os.name == "nt", "the gh fixture is a shebang script Windows cannot launch")
class StageRecoveryTests(RunCycleTestCase):
    def setUp(self):
        super().setUp()
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        for args in (["init", "-b", "task-174"], ["-c", "user.name=Test", "-c",
                     "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "fixture"]):
            subprocess.run(["git", *args], cwd=self.checkout, check=True, capture_output=True)
        self.head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.checkout, text=True).strip()
        self.fixture = self.root / "provider.json"
        self.comments = []
        self.save()
        binary = self.root / "gh"
        binary.write_text(
            "#!" + sys.executable + "\nimport json,os,sys\n"
            "d=json.load(open(os.environ['RECOVERY_FIXTURE']))\n"
            "a=sys.argv[1:]\n"
            "if a[:2]==['pr','list']: print(json.dumps([{'number':4}]))\n"
            "elif a[:2]==['pr','view']: print(json.dumps({'number':4,'state':'OPEN','headRefOid':d['head']}))\n"
            "elif a[0]=='api': print(json.dumps([d['comments']]))\n"
            "else: sys.exit(2)\n"
        )
        binary.chmod(0o755)
        patch = mock.patch.dict(os.environ, {"PATH": str(self.root) + os.pathsep + os.environ['PATH'],
                                           "RECOVERY_FIXTURE": str(self.fixture)})
        patch.start()
        self.addCleanup(patch.stop)
        self.receipt = None

    def save(self):
        self.fixture.write_text(json.dumps({"head": self.head, "comments": self.comments}))

    def publish(self, task, role, **changes):
        attempt = re.search(r"Runtime attempt_id: ([^. ]+)\.", task).group(1)
        value = {"schema": 2, "repo": "owner/api", "change_request_id": "4", "stage": role,
                 "attempt_id": attempt, "head_sha": self.head,
                 "status": "IMPLEMENTED" if role == "implement" else "RESOLVED",
                 "finding_outcomes": [{"id": "REV-001", "status": "resolved"}] if role == "resolve" else []}
        value.update(changes)
        self.receipt = value
        self.comments.append({"id": len(self.comments) + 1, "user": {"login": "trusted"},
                              "body": '<!-- code-cycle-stage ' + json.dumps(value) + ' -->'})
        self.save()
        return "Work published; structured result omitted."

    def execute(self, coder, reviewer=None, **kwargs):
        return self.run_cycle(Worker("codex", coder), Worker("claude", reviewer or (
            lambda task: block("APPROVED", head_sha=self.head, verified_findings=[]))),
            cwd=str(self.checkout), code_host="github", trusted_authors=("trusted",), **kwargs)

    def test_missing_implementation_advances_only_to_structured_review(self):
        report = self.execute(lambda task: self.publish(task, "implement"))
        self.assertEqual(rc.APPROVED_END, report.status)
        self.assertEqual("recovered_pending_verification", report.stage_details[0].status)
        attempt = self.receipt['attempt_id']
        self.assertEqual("failed", self.store.attempt(attempt)['outcome'])
        row = self.store.recoveries(attempt)[0]
        self.assertEqual(16, row['schema_version'])
        self.assertEqual(self.head, row['head_sha'])
        with self.store._connect() as connection:
            for sql in ("DELETE FROM stage_recoveries", "UPDATE stage_recoveries SET comment_id='9'"):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(sql)

    def test_comment_only_resolution_is_verified_without_push(self):
        report = self.execute(lambda task: self.publish(task, "resolve"), lambda task: block(
            "APPROVED", head_sha=self.head, verified_findings=[{'id': 'REV-001', 'status': 'resolved'}]),
            start_from="resolve", change_request_id="4")
        self.assertEqual(rc.APPROVED_END, report.status)
        self.assertEqual(["resolve", "rereview"], [s.role for s in report.stages])

    def test_reopened_claim_enters_another_resolution_and_survivals_ladder(self):
        rounds = []
        prompts = []
        def coder(task):
            prompts.append(task)
            if not rounds:
                return self.publish(task, "resolve")
            return block("RESOLVED", finding_outcomes=[{'id': 'REV-001', 'status': 'resolved'}])
        def reviewer(task):
            rounds.append(task)
            return block("CHANGES_REQUESTED" if len(rounds) == 1 else "APPROVED", head_sha=self.head,
                         verified_findings=[{'id': 'REV-001', 'status': 'still_open' if len(rounds) == 1 else 'resolved'}])
        report = self.execute(coder, reviewer, start_from="resolve", change_request_id="4")
        self.assertEqual(rc.APPROVED_END, report.status)
        self.assertEqual(2, len(prompts))
        self.assertIn("survived a claimed fix", prompts[1])

    def test_recovered_review_is_redispatched_once(self):
        calls = []
        def review(task):
            calls.append(task)
            if len(calls) == 1:
                return self.publish(task, "review", status="APPROVED")
            return block("APPROVED", head_sha=self.head)
        report = self.execute(lambda task: block("IMPLEMENTED"), review)
        self.assertEqual(rc.APPROVED_END, report.status)
        self.assertEqual(2, len(calls))
        self.assertIn("Obtain a structured verdict", calls[1])

    def test_recovered_rereview_is_redispatched_once(self):
        calls = []
        def review(task):
            calls.append(task)
            if len(calls) == 1:
                return self.publish(task, "rereview", status="CHANGES_REQUESTED")
            return block("APPROVED", head_sha=self.head)
        report = self.execute(lambda task: block("RESOLVED"), review,
                              start_from="rereview", change_request_id="4")
        self.assertEqual(rc.APPROVED_END, report.status)
        self.assertEqual(2, len(calls))

    def test_unstructured_verifier_cannot_recover_again_or_approve(self):
        calls = []
        def review(task):
            calls.append(task)
            return self.publish(task, "review", status="APPROVED")
        report = self.execute(lambda task: block("IMPLEMENTED"), review)
        self.assertEqual(rc.UNRESOLVED_END, report.status)
        self.assertEqual(2, len(calls))

    def test_scope_and_blocked_rejections(self):
        for field, value in (("head_sha", "f" * 40), ("attempt_id", "attempt-other"),
                             ("repo", "other/repo"), ("change_request_id", "5"),
                             ("stage", "resolve"), ("status", "BLOCKED")):
            with self.subTest(field=field):
                self.comments = []
                report = self.execute(lambda task: self.publish(task, "implement", **{field: value}))
                self.assertEqual(rc.UNRESOLVED_END, report.status)
                self.assertIn("recovery rejected", report.stopped_because)
                self.assertEqual(1, len(report.stages))

    def test_untrusted_and_tokenless_evidence_is_rejected(self):
        def coder(task):
            self.publish(task, "implement")
            self.comments[0]['user']['login'] = "untrusted"
            self.save()
            return "published"
        report = self.execute(coder)
        self.assertEqual(rc.UNRESOLVED_END, report.status)
        self.comments = []
        report = self.execute(lambda task: "fix pushed and comment published")
        self.assertEqual(rc.UNRESOLVED_END, report.status)

    def test_wrong_verifier_head_or_missing_claim_stops(self):
        for payload in ({'head_sha': 'f' * 40, 'verified_findings': [{'id': 'REV-001', 'status': 'resolved'}]},
                        {'head_sha': self.head, 'verified_findings': []},
                        {'head_sha': self.head, 'verified_findings': [{'id': 'REV-001', 'status': 'still_open'}]}):
            with self.subTest(payload=payload):
                self.comments = []
                report = self.execute(lambda task: self.publish(task, "resolve"),
                                      lambda task: block("APPROVED", **payload),
                                      start_from="resolve", change_request_id="4")
                self.assertEqual(rc.UNRESOLVED_END, report.status)
                self.assertIn("verifier did not confirm", report.stopped_because)

    def test_schema_fifteen_upgrade_keeps_failed_attempts(self):
        report = self.execute(lambda task: self.publish(task, "implement"))
        self.assertEqual(rc.APPROVED_END, report.status)
        attempt_id = self.receipt['attempt_id']
        original = self.store.attempt(attempt_id)
        with self.store._connect() as connection:
            connection.execute("DROP TABLE stage_recoveries")
        upgraded = tm.Telemetry(self.store.path)
        self.assertEqual(original, upgraded.attempt(attempt_id))
        self.assertEqual([], upgraded.recoveries(attempt_id))
        upgraded.record_recovery(attempt_id, change_request_id="4", head_sha=self.head, comment_id="1")
        self.assertEqual("failed", upgraded.attempt(attempt_id)['outcome'])

    def test_duplicate_receipts_and_published_blocked_result_veto_recovery(self):
        for duplicate in (True, False):
            with self.subTest(duplicate=duplicate):
                self.comments = []
                def coder(task):
                    self.publish(task, "implement")
                    if duplicate:
                        self.comments.append(dict(self.comments[0], id=2))
                    else:
                        self.comments[0]['body'] += '\n' + block("BLOCKED")
                    self.save()
                    return "published"
                report = self.execute(coder)
                self.assertEqual(rc.UNRESOLVED_END, report.status)
                self.assertIn("recovery rejected", report.stopped_because)

    def test_explicit_blocked_stage_is_never_recovered(self):
        def coder(task):
            self.publish(task, "implement")
            return block("BLOCKED")
        report = self.execute(coder)
        self.assertEqual(rc.UNRESOLVED_END, report.status)
        self.assertEqual([], self.store.recoveries(self.receipt['attempt_id']))

    def test_remote_head_change_during_verification_stops(self):
        def review(task):
            payload = block("APPROVED", head_sha=self.head)
            self.head = 'f' * 40
            self.save()
            return payload
        report = self.execute(lambda task: self.publish(task, "implement"), review)
        self.assertEqual(rc.UNRESOLVED_END, report.status)
        self.assertIn("recovered stage not completed", report.stopped_because)

    def test_provider_nonzero_exit_reports_recovery_reason(self):
        (self.root / "gh").write_text("#!" + sys.executable + "\nimport sys\nsys.exit(1)\n")
        report = self.execute(lambda task: self.publish(task, "implement"))
        self.assertEqual(rc.UNRESOLVED_END, report.status)
        self.assertIn("could not read GitHub evidence", report.stopped_because)


class StageEvidenceParserTests(unittest.TestCase):
    def test_parser_rejects_malformed_and_header_only_claims(self):
        self.assertEqual([], contract.stage_evidence("#### [REV-001] · low · resolved · valid · blocks:no"))
        for raw in ('{}', 'null', '{"schema":1}', 'broken'):
            with self.assertRaises(contract.ContractError):
                contract.stage_evidence('<!-- code-cycle-stage ' + raw + ' -->')
