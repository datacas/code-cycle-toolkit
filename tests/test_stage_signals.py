"""Pre-routing signals: what a stage's router could have known, and only that."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import executors as ex  # noqa: E402
import run_cycle as rc  # noqa: E402
import stage_signals as ss  # noqa: E402
import telemetry as tm  # noqa: E402
from test_cycle import CycleTestCase, ScriptedAdapter  # noqa: E402
from test_run_cycle import RunCycleTestCase, Talker, block  # noqa: E402


def numstat(*entries: tuple[str, str, str]) -> str:
    return "".join(f"{added}\t{deleted}\t{path}\0" for added, deleted, path in entries)


class ClassifierTests(unittest.TestCase):
    def test_test_paths_are_recognised_by_directory_and_by_name(self) -> None:
        for path in ("tests/test_router.py", "src/foo_test.go", "web/a.spec.tsx",
                     "src/__tests__/x.js", "app/FooTest.java", "spec/user_spec.rb"):
            with self.subTest(path=path):
                self.assertTrue(ss.is_test_path(path))
        for path in ("src/router.py", "docs/testing-guide.md", "contest.py"):
            with self.subTest(path=path):
                self.assertFalse(ss.is_test_path(path))

    def test_areas_are_flagged_from_paths(self) -> None:
        signals = ss.derive_change_signals(numstat(
            ("1", "0", "requirements-dev.txt"),
            ("2", "0", "app/db/migrations/0002_add.py"),
            ("3", "0", "src/auth/tokens.py"),
            ("4", "0", "src/api/routes.py"),
            ("5", "0", ".github/workflows/validate.yml"),
        ))
        self.assertTrue(signals.touches_dependencies)
        self.assertTrue(signals.touches_migrations)
        self.assertTrue(signals.touches_database)
        self.assertTrue(signals.touches_auth)
        self.assertTrue(signals.touches_api)
        self.assertTrue(signals.touches_ci)

    def test_auth_is_matched_by_word_not_by_substring(self) -> None:
        for path in ("src/processor.py", "docs/author.md", "lib/miracle.rb"):
            with self.subTest(path=path):
                self.assertFalse(ss.derive_change_signals(numstat(("1", "0", path))).touches_auth)
        for path in ("src/AuthService.ts", "app/login_view.py", "lib/oauth2/client.go"):
            with self.subTest(path=path):
                self.assertTrue(ss.derive_change_signals(numstat(("1", "0", path))).touches_auth)

    def test_an_unrelated_change_touches_nothing(self) -> None:
        signals = ss.derive_change_signals(numstat(("10", "2", "src/router.py")))
        for flag in ("touches_dependencies", "touches_database", "touches_auth",
                     "touches_api", "touches_migrations", "touches_ci", "has_tests"):
            with self.subTest(flag=flag):
                self.assertIs(False, getattr(signals, flag))

    def test_counts_are_deterministic_and_binary_files_have_no_lines(self) -> None:
        text = numstat(("10", "2", "src/a.py"), ("-", "-", "img/logo.png"),
                       ("3", "3", "README.md"))
        first = ss.derive_change_signals(text)
        self.assertEqual(first, ss.derive_change_signals(text))
        self.assertEqual(3, first.changed_files_count)
        self.assertEqual(18, first.changed_lines_estimate)

    def test_language_counters_are_a_fixed_set_of_scalars(self) -> None:
        fields = ss.derive_change_signals(numstat(
            ("1", "0", "a.py"), ("1", "0", "b.py"), ("1", "0", "c.ts"),
            ("1", "0", "Makefile"),
        )).telemetry_fields()
        language = {key: value for key, value in fields.items() if key in ss.LANGUAGE_FIELDS}
        self.assertEqual(set(ss.LANGUAGE_FIELDS), set(language))
        self.assertEqual(2, language["changed_python_files"])
        self.assertEqual(1, language["changed_typescript_files"])
        self.assertEqual(1, language["changed_other_files"])
        self.assertEqual(0, language["changed_go_files"])
        self.assertTrue(all(isinstance(value, int) for value in language.values()))

    def test_every_field_it_can_emit_is_one_telemetry_accepts(self) -> None:
        fields = ss.derive_change_signals(
            numstat(("1", "1", "tests/test_a.py")), "+++ b/tests/test_a.py\n+def test_x():\n",
        ).telemetry_fields()
        for key, value in fields.items():
            with self.subTest(key=key):
                self.assertEqual(value, tm.validate_reference(key, value))

    def test_no_path_or_text_leaves_the_module(self) -> None:
        fields = ss.derive_change_signals(
            numstat(("1", "0", "src/secret-plan.py")), "+++ b/src/secret-plan.py\n+x = 1\n",
        ).telemetry_fields()
        for value in fields.values():
            self.assertIsInstance(value, (bool, int))

    def test_test_definitions_are_counted_only_in_added_lines_of_test_files(self) -> None:
        diff = "\n".join([
            "+++ b/tests/test_a.py",
            "+def test_one():",
            "+    async def test_two(self):",
            "-def test_removed():",
            "+++ b/src/a.py",
            "+def test_not_in_a_test_file():",
            "+++ b/web/a.test.ts",
            "+it('works', () => {})",
        ])
        self.assertEqual(3, ss.count_test_definitions(diff))

    def test_unknown_stays_unknown(self) -> None:
        self.assertEqual({}, ss.ChangeSignals().telemetry_fields())
        signals = ss.derive_change_signals(numstat(("1", "0", "a.py")))
        self.assertIsNone(signals.test_count_estimate)
        self.assertNotIn("test_count_estimate", signals.telemetry_fields())

    def test_an_empty_diff_that_was_read_is_zero_not_unknown(self) -> None:
        fields = ss.derive_change_signals("", "").telemetry_fields()
        self.assertEqual(0, fields["changed_files_count"])
        self.assertEqual(0, fields["test_count_estimate"])
        self.assertIs(False, fields["has_tests"])


@unittest.skipUnless(shutil.which("git"), "git is required")
class CollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("hello\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "base")
        self.git("checkout", "-q", "-b", "work")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_x.py").write_text("def test_a():\n    pass\n")
        (self.repo / "app.py").write_text("x = 1\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "work")

    def git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
             "-c", "commit.gpgsign=false", *args],
            cwd=self.repo, check=True, capture_output=True,
        )

    def test_it_reads_the_change_against_the_first_base_that_resolves(self) -> None:
        signals = ss.collect_change_signals(str(self.repo), ("origin/main", "main"))
        self.assertIsNotNone(signals)
        self.assertEqual(2, signals.changed_files_count)
        self.assertEqual(3, signals.changed_lines_estimate)
        self.assertTrue(signals.has_tests)
        self.assertEqual(1, signals.test_count_estimate)

    def test_no_resolvable_base_is_unknown_not_an_empty_change(self) -> None:
        self.assertIsNone(ss.collect_change_signals(str(self.repo), ("origin/nope",)))

    def test_outside_a_worktree_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as elsewhere:
            self.assertIsNone(ss.collect_change_signals(elsewhere, ("main",)))


CHANGE = ss.derive_change_signals(numstat(("4", "1", "src/api/a.py"),
                                          ("2", "0", "tests/test_a.py")), "")


class RecorderSignalTests(CycleTestCase):
    def rows_by_role(self, role):
        return [row for row in self.store.rows("owner/repo") if row["role"] == role]

    def cycle(self, **kw):
        recorder = self.recorder([ScriptedAdapter("codex"), ScriptedAdapter("claude")],
                                 change_observer=lambda: CHANGE, **kw)
        recorder.stage("implement", "work")
        recorder.record_verdict("implement", "IMPLEMENTED")
        recorder.stage("review", "review")
        recorder.record_verdict("review", "CHANGES_REQUESTED", findings_total=2,
                                findings_blocking=1, findings_critical=0,
                                findings_high=1, findings_medium=0, findings_low=1)
        recorder.next_iteration()
        recorder.stage("resolve", "resolve")
        recorder.record_verdict("resolve", "RESOLVED")
        recorder.stage("rereview", "rereview")
        return recorder

    def test_the_implementation_is_routed_knowing_no_change_and_no_findings(self) -> None:
        self.cycle()
        payload = self.rows_by_role("implement")[0]["payload"]
        for key in ("changed_files_count", "has_tests", "prior_findings_total",
                    "resolution_round", *ss.LANGUAGE_FIELDS):
            with self.subTest(key=key):
                self.assertNotIn(key, payload)
        self.assertEqual(0, payload["previous_failed_attempts"])

    def test_later_stages_carry_the_observed_change(self) -> None:
        self.cycle()
        for role in ("review", "resolve", "rereview"):
            payload = self.rows_by_role(role)[0]["payload"]
            with self.subTest(role=role):
                self.assertEqual(2, payload["changed_files_count"])
                self.assertEqual(7, payload["changed_lines_estimate"])
                self.assertTrue(payload["touches_api"])
                self.assertTrue(payload["has_tests"])

    def test_findings_are_those_reported_before_the_stage_not_after(self) -> None:
        self.cycle()
        review = self.rows_by_role("review")[0]["payload"]
        resolve = self.rows_by_role("resolve")[0]["payload"]
        rereview = self.rows_by_role("rereview")[0]["payload"]
        self.assertNotIn("prior_findings_total", review)
        self.assertEqual(2, resolve["prior_findings_total"])
        self.assertEqual(1, resolve["prior_findings_blocking"])
        self.assertEqual(1, resolve["prior_findings_high"])
        self.assertEqual(0, resolve["prior_findings_critical"])
        # The resolver reported no findings, so the rereview does not inherit
        # the review's count as if it were still true.
        self.assertNotIn("prior_findings_total", rereview)

    def test_the_resolution_round_is_known_only_where_it_means_something(self) -> None:
        self.cycle()
        self.assertEqual(1, self.rows_by_role("resolve")[0]["payload"]["resolution_round"])
        self.assertEqual(1, self.rows_by_role("rereview")[0]["payload"]["resolution_round"])
        self.assertNotIn("resolution_round", self.rows_by_role("review")[0]["payload"])

    def test_verification_availability_is_recorded_only_when_declared(self) -> None:
        self.cycle()
        self.assertNotIn("verification_available",
                         self.rows_by_role("implement")[0]["payload"])

    def test_declared_verification_availability_is_recorded(self) -> None:
        self.cycle(verification_available=False)
        self.assertIs(False, self.rows_by_role("implement")[0]["payload"]["verification_available"])

    def test_failed_attempts_count_what_happened_before_the_routing(self) -> None:
        codex = ScriptedAdapter("codex", outcomes=[
            (ex.DispatchOutcome.BLOCKED, "operating_quota"),
        ])
        claude = ScriptedAdapter("claude")
        recorder = self.recorder([codex, claude])
        recorder.stage("implement", "work")
        rows = self.rows_by_role("implement")
        self.assertEqual(2, len(rows))
        self.assertEqual(0, rows[0]["payload"]["previous_failed_attempts"])
        self.assertEqual(1, rows[1]["payload"]["previous_failed_attempts"])
        recorder.stage("review", "review")
        self.assertEqual(1, self.rows_by_role("review")[0]["payload"]["previous_failed_attempts"])

    def test_the_observer_is_not_consulted_for_the_implementation(self) -> None:
        calls = []
        recorder = self.recorder([ScriptedAdapter("codex"), ScriptedAdapter("claude")],
                                 change_observer=lambda: calls.append(1))
        recorder.stage("implement", "work")
        self.assertEqual([], calls)
        recorder.stage("review", "review")
        self.assertEqual([1], calls)

    def test_every_post_implementation_role_is_routed_knowing_the_change(self) -> None:
        for role in ("review", "rereview", "resolve", "security", "verify", "run"):
            with self.subTest(role=role):
                calls = []
                recorder = self.recorder(
                    [ScriptedAdapter("codex"), ScriptedAdapter("claude")],
                    change_observer=lambda: calls.append(role) or CHANGE,
                )
                recorder.stage(role, "work")
                self.assertEqual([role], calls)
                row = self.rows_by_role(role)[-1]
                self.assertEqual(2, row["payload"]["changed_files_count"])

    def test_an_unreadable_change_is_left_out(self) -> None:
        recorder = self.recorder([ScriptedAdapter("codex"), ScriptedAdapter("claude")],
                                 change_observer=lambda: None)
        recorder.stage("review", "review")
        self.assertNotIn("changed_files_count", self.rows_by_role("review")[0]["payload"])

    def test_a_blocked_routing_row_carries_the_same_signals(self) -> None:
        recorder = self.recorder([ScriptedAdapter("codex"), ScriptedAdapter("claude")],
                                 availability={"codex": ex.Availability.UNKNOWN,
                                               "claude": ex.Availability.UNKNOWN},
                                 change_observer=lambda: CHANGE)
        recorder.stage("review", "review")
        row = self.rows_by_role("review")[0]
        self.assertEqual("blocked", row["outcome"])
        self.assertEqual(2, row["payload"]["changed_files_count"])
        self.assertEqual(2, row["difficulty"])


class DriverSignalTests(RunCycleTestCase):
    def test_severity_counts_are_recorded_only_when_every_finding_has_one(self) -> None:
        complete = rc._findings({"unresolved_findings": [
            {"severity": "high", "blocks_approval": True}, {"severity": "low"},
        ]})
        self.assertEqual(1, complete["findings_high"])
        self.assertEqual(0, complete["findings_critical"])
        partial = rc._findings({"unresolved_findings": [
            {"severity": "high"}, {"id": "REV-002"},
        ]})
        self.assertEqual(2, partial["findings_total"])
        self.assertNotIn("findings_high", partial)

    def test_change_bases_follow_the_declared_default_branch(self) -> None:
        config = {"code_cycle": {"repository": {"default_branch": "trunk"}}}
        self.assertEqual(("origin/trunk", "trunk"), rc.change_bases_of(config))
        self.assertEqual(("origin/HEAD",), rc.change_bases_of({}))

    def test_the_review_is_routed_knowing_the_implementation_findings_never(self) -> None:
        reviewer = Talker("claude", block("CHANGES_REQUESTED", unresolved_findings=[
            {"id": "REV-001", "severity": "high", "blocks_approval": True},
        ]))
        self.run_cycle(Talker("codex"), reviewer, max_iterations=0)
        review = [row for row in self.rows() if row["role"] == "review"]
        self.assertNotIn("prior_findings_total", review[0]["payload"])
        self.assertEqual(1, review[1]["payload"]["findings_high"])


class FieldValidationTests(unittest.TestCase):
    def test_every_new_field_is_typed(self) -> None:
        cases = {
            "changed_files_count": ("3", True, 2.5),
            "has_tests": (1, "yes"),
            "touches_auth": (0,),
            "prior_findings_high": (-1, True),
            "resolution_round": ("1",),
        }
        for key, bad in cases.items():
            for value in bad:
                with self.subTest(key=key, value=value):
                    with self.assertRaises(tm.TelemetryError):
                        tm.validate_reference(key, value)

    def test_counts_are_bounded(self) -> None:
        for key, value in (("changed_lines_estimate", -1),
                           ("changed_files_count", tm._UPPER_COUNT + 1),
                           ("difficulty", 4), ("previous_failed_attempts", -2)):
            with self.subTest(key=key):
                with self.assertRaises(tm.TelemetryError):
                    tm.validate_reference(key, value)

    def test_nested_or_unknown_signals_are_refused(self) -> None:
        for key, value in (("changed_python_files", [1]),
                           ("changed_files_count", {"n": 1}),
                           ("changed_haskell_files", 1),
                           ("changed_paths", "src/a.py")):
            with self.subTest(key=key):
                with self.assertRaises(tm.TelemetryError):
                    tm.validate_reference(key, value)

    def test_every_language_counter_is_an_allowlisted_column(self) -> None:
        for key in ss.LANGUAGE_FIELDS:
            with self.subTest(key=key):
                self.assertEqual(("count", None), tm.FIELD_SPECS[key])

    def test_every_pre_routing_signal_is_classified_exactly_once(self) -> None:
        kinds = list(tm.PRE_ROUTING_SIGNALS.values())
        everything = set().union(*kinds)
        self.assertEqual(sum(len(kind) for kind in kinds), len(everything))
        self.assertTrue(everything <= set(tm.FIELD_SPECS))
        emitted = set(CHANGE.telemetry_fields())
        self.assertTrue(emitted <= everything)
        self.assertEqual({"changed_lines_estimate", "test_count_estimate"},
                         tm.PRE_ROUTING_SIGNALS["estimated"])


class SchemaCompatibilityTests(unittest.TestCase):
    def test_a_version_1_database_is_read_and_extended(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.sqlite"
            store = tm.Telemetry(path)
            with unittest.mock.patch.object(tm, "SCHEMA_VERSION", 1):
                store.record_stage("repo", "T-1", "implement", difficulty=2)
            reopened = tm.Telemetry(path)
            reopened.record_stage("repo", "T-2", "review", changed_files_count=3)
            rows = reopened.rows("repo")
            self.assertEqual([1, 2], [row["schema_version"] for row in rows])
            self.assertEqual({}, rows[0]["payload"])
            self.assertEqual(3, rows[1]["payload"]["changed_files_count"])


if __name__ == "__main__":
    unittest.main()
