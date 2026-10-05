from __future__ import annotations

import contextlib
import copy
import dataclasses
import io
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import behavioral as bv  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "behavioral"
PROJECT = Path("/work/project")


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def keep_ids(report: dict, ids: list[str]) -> dict:
    """A `--list` report narrowed to the specs carrying these IDs."""
    narrowed = copy.deepcopy(report)

    def visit(suite: dict) -> None:
        suite["specs"] = [s for s in suite.get("specs", []) if any(t in ids for t in s["tags"])]
        for child in suite.get("suites", []):
            visit(child)

    for suite in narrowed["suites"]:
        visit(suite)
    return narrowed


def expected_for(*ids: str) -> list[bv.ListedTest]:
    return bv.listed_tests(keep_ids(load("list"), list(ids)))


def spec_named(report: dict, title: str) -> dict:
    for _, _, spec in bv.iter_specs(report):
        if spec["title"] == title:
            return spec
    raise AssertionError(title)


class SelectionTests(unittest.TestCase):
    def test_an_id_selects_only_itself_not_a_longer_id(self) -> None:
        pattern = re.compile(bv.selection_grep(["AUTH-LOGIN-001"]))

        self.assertTrue(pattern.search("login renders @behavioral @AUTH-LOGIN-001"))
        self.assertFalse(pattern.search("login variant @behavioral @AUTH-LOGIN-0010"))
        self.assertFalse(pattern.search("login variant @behavioral @AUTH-LOGIN-001-b"))

    def test_several_ids_become_one_alternation(self) -> None:
        grep = bv.selection_grep(["AUTH-LOGIN-001", "CART-ADD-002"])

        self.assertEqual(grep, "@AUTH-LOGIN-001(?![\\w-])|@CART-ADD-002(?![\\w-])")

    def test_the_tag_selection_does_not_match_a_longer_tag(self) -> None:
        pattern = re.compile(bv.selection_grep())

        self.assertTrue(pattern.search("x @behavioral @AUTH-LOGIN-001"))
        self.assertFalse(pattern.search("x @behavioral-slow"))

    def test_a_value_that_is_not_an_id_is_refused(self) -> None:
        for bad in ("login", "AUTH-LOGIN-1", "AUTH-LOGIN-001; rm -rf /", "auth-login-001"):
            with self.subTest(bad=bad), self.assertRaises(bv.BehavioralError):
                bv.selection_grep([bad])

    def test_a_tag_with_regex_syntax_is_refused(self) -> None:
        with self.assertRaises(bv.BehavioralError):
            bv.selection_grep(tag="a|b")


class RegistryTests(unittest.TestCase):
    def test_the_listing_exposes_identity_location_and_covers(self) -> None:
        tests = {t.test_id: t for t in bv.listed_tests(load("list"))}

        login = tests["AUTH-LOGIN-001"]
        self.assertEqual(login.title, "login page renders")
        self.assertEqual(login.file, "tests/behavioral/pass.spec.ts")
        self.assertEqual(login.line, 2)
        self.assertEqual(login.covers, ("issue:151",))
        self.assertIn("behavioral", login.tags)

    def test_a_describe_title_is_kept_apart_from_the_file(self) -> None:
        tests = {t.test_id: t for t in bv.listed_tests(load("list"))}

        self.assertEqual(tests["MISC-SETUP-001"].describe, ("setup group",))
        self.assertEqual(tests["AUTH-LOGIN-001"].describe, ())

    def test_a_clean_listing_has_no_problems(self) -> None:
        self.assertEqual(bv.registry_problems(bv.listed_tests(load("list"))), [])

    def test_the_same_test_identity_in_multiple_projects_is_valid(self) -> None:
        listed = bv.listed_tests(keep_ids(load("list"), ["AUTH-LOGIN-001"]))
        repeated = [dataclasses.replace(test, key=f"{test.key.split('::')[0]}::{project}", project=project)
                    for test in listed for project in ("chromium", "firefox")]

        self.assertEqual(bv.registry_problems(repeated), [])

    def test_unidentified_duplicate_and_ambiguous_tests_are_reported(self) -> None:
        report = copy.deepcopy(load("list"))
        file_suite = report["suites"][0]
        first = file_suite["specs"][0]
        file_suite["specs"].append({**first, "id": "dup", "tags": ["behavioral", first["tags"][1]]})
        file_suite["specs"].append({**first, "id": "none", "title": "no id", "tags": ["behavioral"]})
        file_suite["specs"].append({**first, "id": "two", "title": "two ids",
                                    "tags": ["behavioral", "AREA-ONE-001", "AREA-TWO-002"]})

        kinds = sorted(p["kind"] for p in bv.registry_problems(bv.listed_tests(report)))

        self.assertEqual(kinds, ["duplicate_id", "multiple_ids", "unidentified"])


class PassRuleTests(unittest.TestCase):
    def test_a_clean_selected_run_passes(self) -> None:
        outcome = bv.evaluate(load("grep_look"), expected_for("AUTH-LOGIN-001"), 0)

        self.assertEqual(outcome["status"], "pass")
        self.assertEqual(outcome["counts"]["selected"], 1)
        self.assertEqual(outcome["counts"]["executed"], 1)
        self.assertEqual(outcome["results"][0]["executionMode"], "deterministic")

    def test_a_test_fail_assertion_is_not_counted_as_a_pass(self) -> None:
        report = load("grep_look")
        spec = next(spec for _, _, spec in bv.iter_specs(report) if spec.get("tests"))
        test = spec["tests"][0]
        test["expectedStatus"] = "failed"
        test["status"] = "expected"
        test["results"][-1]["status"] = "failed"

        outcome = bv.evaluate(report, expected_for("AUTH-LOGIN-001"), 0)

        self.assertEqual(outcome["status"], "fail")
        self.assertEqual((outcome["counts"]["passed"], outcome["counts"]["failed"]), (0, 1))

    def test_a_missing_report_is_an_error_whatever_the_exit_code(self) -> None:
        outcome = bv.evaluate(None, expected_for("AUTH-LOGIN-001"), 0)

        self.assertEqual((outcome["status"], outcome["reason"]), ("error", "report_missing_or_invalid"))

    def test_a_timeout_is_an_error(self) -> None:
        outcome = bv.evaluate(load("grep_look"), expected_for("AUTH-LOGIN-001"), None)

        self.assertEqual((outcome["status"], outcome["reason"]), ("error", "timeout"))

    def test_a_flaky_test_exits_zero_but_is_not_a_pass(self) -> None:
        outcome = bv.evaluate(load("flaky_r1"), expected_for("RETRY-FLAKY-001"), 0)

        self.assertEqual(outcome["status"], "flaky")
        self.assertEqual(outcome["results"][0]["stability"], "passed_on_retry")
        self.assertEqual([a["status"] for a in outcome["results"][0]["attempts"]], ["failed", "passed"])

    def test_a_failure_that_repeats_is_stable_within_the_run(self) -> None:
        outcome = bv.evaluate(load("repeated_r1"), expected_for("RETRY-REPEAT-001"), 1)

        self.assertEqual(outcome["status"], "fail")
        self.assertEqual(outcome["results"][0]["stability"], "repeated_in_run")

    def test_a_selected_test_that_is_skipped_is_not_a_pass(self) -> None:
        outcome = bv.evaluate(load("skipped"), expected_for("MISC-SKIP-001"), 0)

        self.assertEqual((outcome["status"], outcome["reason"]), ("skipped", "selected_test_skipped"))
        self.assertEqual(outcome["counts"]["executed"], 0)

    def test_zero_tests_with_a_tag_selection_means_not_configured(self) -> None:
        outcome = bv.evaluate(load("zero"), [], 1, selection_kind="tag")

        self.assertEqual((outcome["status"], outcome["reason"]), ("not_configured", "no_tests_selected"))

    def test_zero_tests_for_an_id_selection_is_an_error(self) -> None:
        outcome = bv.evaluate(load("zero"), [], 1, selection_kind="ids")

        self.assertEqual((outcome["status"], outcome["reason"]), ("error", "unknown_test_ids"))

    def test_nothing_executed_is_not_a_pass_even_with_exit_zero(self) -> None:
        """`--pass-with-no-tests` and `--only-changed` with no change both exit 0."""
        outcome = bv.evaluate(load("zero_nofile"), expected_for("AUTH-LOGIN-001"), 0)

        self.assertEqual((outcome["status"], outcome["reason"]), ("error", "selection_incomplete"))
        self.assertEqual(outcome["counts"]["executed"], 0)

    def test_test_only_without_forbid_only_drops_tests_and_is_caught_by_the_listing(self) -> None:
        listing = copy.deepcopy(load("only_noforbid"))
        report = load("only_noforbid")
        other = copy.deepcopy(spec_named(listing, "O-ONLY"))
        other.update({"id": "other", "title": "O-OTHER", "tags": ["behavioral", "ONLY-002"]})
        listing["suites"][0]["specs"].append(other)
        expected = bv.listed_tests(listing)

        outcome = bv.evaluate(report, expected, 0)

        self.assertEqual(len(expected), 2)
        self.assertEqual((outcome["status"], outcome["reason"]), ("error", "selection_incomplete"))
        self.assertEqual(outcome["missing"], ["ONLY-002"])

    def test_forbid_only_is_a_run_level_error_and_not_a_failure(self) -> None:
        outcome = bv.evaluate(load("only_forbid"), expected_for("AUTH-LOGIN-001"), 1)

        self.assertEqual((outcome["status"], outcome["reason"]), ("error", "run_level_error"))
        self.assertIn(".only", outcome["errors"][0])

    def test_a_web_server_that_does_not_start_blocks_the_run(self) -> None:
        outcome = bv.evaluate(load("badserver"), expected_for("AUTH-LOGIN-001"), 1)

        self.assertEqual((outcome["status"], outcome["reason"]), ("blocked", "web_server_failed"))

    def test_a_colliding_grep_that_ran_extra_tests_is_an_error(self) -> None:
        """`@AUTH-LOGIN-001` without the lookahead also ran `-0010`."""
        outcome = bv.evaluate(load("grep_naive"), expected_for("AUTH-LOGIN-001"), 0)

        self.assertEqual((outcome["status"], outcome["reason"]), ("error", "selection_exceeded"))
        self.assertEqual(outcome["unexpected_tests"], ["AUTH-LOGIN-0010"])

    def test_a_non_zero_exit_with_no_cause_in_the_report_is_an_error(self) -> None:
        outcome = bv.evaluate(load("grep_look"), expected_for("AUTH-LOGIN-001"), 1)

        self.assertEqual((outcome["status"], outcome["reason"]), ("error", "exit_code_inconsistent"))

    def test_a_setup_failure_is_a_failure_with_an_unknown_category(self) -> None:
        outcome = bv.evaluate(load("setup_err"), expected_for("MISC-SETUP-001"), 1)

        self.assertEqual(outcome["status"], "fail")
        self.assertEqual(outcome["results"][0]["failure"]["category"], "unknown")

    def test_an_unreachable_application_blocks_only_when_the_error_says_so(self) -> None:
        refused = copy.deepcopy(load("repeated_r1"))
        spec = spec_named(refused, "R-REPEATED")
        for result in spec["tests"][0]["results"]:
            result["errors"] = [{"message": "Error: page.goto: net::ERR_CONNECTION_REFUSED at http://x/"}]

        blocked = bv.evaluate(refused, expected_for("RETRY-REPEAT-001"), 1)
        aborted = bv.evaluate(load("nosrv"), expected_for("AUTH-LOGIN-001"), 1)

        self.assertEqual((blocked["status"], blocked["reason"]), ("blocked", "environment_suspected"))
        self.assertEqual(aborted["status"], "fail")
        self.assertEqual(aborted["results"][0]["failure"]["category"], "timeout")

    def test_a_failure_dominates_an_incomplete_selection(self) -> None:
        outcome = bv.evaluate(load("run1"), expected_for("FAIL-TOBE-001", "AUTH-LOGIN-001", "NOPE-NOPE-001"), 1)

        self.assertEqual(outcome["status"], "fail")


class FailureExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.outcome = bv.evaluate(load("run1"), bv.listed_tests(load("list")), 1)
        cls.by_id = {r["testId"]: r for r in cls.outcome["results"]}

    def failure(self, test_id: str) -> dict:
        return self.by_id[test_id]["failure"]

    def test_the_whole_run_is_counted(self) -> None:
        self.assertEqual(self.outcome["status"], "fail")
        self.assertEqual(self.outcome["counts"], {
            "selected": 12, "executed": 11, "passed": 2, "failed": 8, "flaky": 1,
            "skipped": 1, "other": 0, "missing": 0,
        })

    def test_expected_and_received_are_extracted_when_both_exist(self) -> None:
        failure = self.failure("FAIL-TOBE-001")

        self.assertEqual((failure["expected"], failure["observed"]), ('"5"', '"3"'))
        self.assertEqual(self.failure("FAIL-HTTP-001")["observed"], "404")
        self.assertEqual(self.failure("FAIL-TEXT-001")["expected"], '"Dashboard"')

    def test_when_they_cannot_be_extracted_the_message_is_still_kept(self) -> None:
        for test_id in ("FAIL-TOEQUAL-001", "FAIL-VISIBLE-001", "FAIL-SELECTOR-001"):
            with self.subTest(test_id=test_id):
                failure = self.failure(test_id)
                self.assertIsNone(failure["expected"])
                self.assertIsNone(failure["observed"])
                self.assertTrue(failure["error"].strip())

    def test_the_message_has_no_colour_codes_and_no_host_paths(self) -> None:
        failures = [r["failure"] for r in self.outcome["results"] if r.get("failure")]
        text = json.dumps(failures)

        self.assertFalse("\\u001b" in text)
        self.assertFalse(str(PROJECT) in text)

    def test_categories_are_a_heuristic_hint(self) -> None:
        self.assertEqual(self.failure("FAIL-TEXT-001")["category"], "assertion")
        self.assertEqual(self.failure("FAIL-SELECTOR-001")["category"], "selector")
        self.assertTrue(self.failure("FAIL-TEXT-001")["categoryIsHeuristic"])

    def test_the_step_is_the_failing_statement_and_the_location_is_relative(self) -> None:
        failure = self.failure("FAIL-TOBE-001")

        self.assertEqual(failure["step"], "expect(n).toBe('5');")
        self.assertEqual(failure["location"]["file"], "tests/behavioral/fail.spec.ts")
        self.assertEqual(failure["location"]["line"], 9)

    def test_a_flaky_test_keeps_the_first_failing_attempt(self) -> None:
        result = self.by_id["RETRY-FLAKY-001"]

        self.assertEqual(result["status"], "flaky")
        self.assertEqual(result["failure"]["attempt"], 0)

    def test_evidence_lists_each_attachment_with_its_attempt(self) -> None:
        kinds = {(e["kind"], e["attempt"]) for e in self.by_id["FAIL-TOBE-001"]["evidence"]}

        self.assertIn(("error-context", 0), kinds)
        self.assertIn(("screenshot", 1), kinds)
        self.assertIn(("trace", 1), kinds)


class FingerprintTests(unittest.TestCase):
    """The fingerprint names where a test failed; the signature names what it saw."""

    def failure_of(self, report: dict) -> dict:
        outcome = bv.evaluate(report, expected_for("FAIL-HTTP-001"), 1)
        return outcome["results"][0]["failure"]

    def edited(self, transform) -> dict:
        report = copy.deepcopy(load("run1"))
        spec = spec_named(report, "F-HTTP-404")
        for result in spec["tests"][0]["results"]:
            for error in result["errors"]:
                error["message"] = transform(error["message"])
            result["errorLocation"]["line"] += 7
        spec["line"] += 7
        return report

    def test_the_fingerprint_survives_code_that_moves(self) -> None:
        base = self.failure_of(load("run1"))
        moved = self.failure_of(self.edited(lambda m: m.replace("> 24 |", "> 31 |")))

        self.assertEqual(moved["fingerprint"], base["fingerprint"])

    def test_the_fingerprint_survives_a_changed_symptom_but_the_signature_does_not(self) -> None:
        base = self.failure_of(load("run1"))
        changed = self.failure_of(self.edited(lambda m: m.replace("404", "500")))

        self.assertEqual(changed["fingerprint"], base["fingerprint"])
        self.assertNotEqual(changed["observedSignature"]["sha"], base["observedSignature"]["sha"])
        self.assertEqual(changed["observed"], "500")

    def test_the_fingerprint_changes_when_the_failing_statement_changes(self) -> None:
        base = self.failure_of(load("run1"))
        elsewhere = self.failure_of(self.edited(
            lambda m: m.replace("expect(r?.status()).toBe(200);", "expect(r?.ok()).toBe(true);")))

        self.assertNotEqual(elsewhere["fingerprint"], base["fingerprint"])

    def test_two_tests_do_not_share_a_fingerprint(self) -> None:
        outcome = bv.evaluate(load("run1"), bv.listed_tests(load("list")), 1)
        prints = [r["failure"]["fingerprint"] for r in outcome["results"] if r.get("failure")]

        self.assertEqual(len(prints), len(set(prints)))


class TextTests(unittest.TestCase):
    def test_colour_codes_are_removed(self) -> None:
        self.assertEqual(bv.strip_ansi("\x1b[2mexpect(\x1b[22m\x1b[31mx\x1b[39m"), "expect(x")

    def test_the_project_path_becomes_relative(self) -> None:
        text = bv.clean_text("at /work/project/tests/a.spec.ts:3", Path("/work/project"))

        self.assertEqual(text, "at tests/a.spec.ts:3")


def fake_runner(report_for_run: dict | None, listing: dict | None, *, run_code: int = 0,
                calls: list | None = None):
    """A process runner that writes the reports Playwright would have written."""

    def run(argv, cwd, env, timeout):
        if calls is not None:
            calls.append({"argv": list(argv), "cwd": cwd, "env": env, "timeout": timeout})
        target = Path(env["PLAYWRIGHT_JSON_OUTPUT_NAME"])
        if "--list" in argv:
            if listing is None:
                return 1
            target.write_text(json.dumps(listing), encoding="utf-8")
            return 0
        if report_for_run is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(report_for_run), encoding="utf-8")
        return run_code

    return run


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "project"
        self.project.mkdir()
        (self.project / "playwright.config.ts").write_text("export default {};\n", encoding="utf-8")
        self.evidence = Path(self._tmp.name) / "evidence"

    def run_with(self, report, listing, **kwargs):
        calls: list = kwargs.pop("calls", [])
        kwargs.setdefault("probe_version", lambda _: "1.63.0")
        result = bv.run_behavioral(
            self.project, self.evidence,
            runner=fake_runner(report, listing, run_code=kwargs.pop("code", 0), calls=calls), **kwargs)
        return result, calls

    def test_a_passing_run_is_persisted_with_its_hash(self) -> None:
        result, _ = self.run_with(load("grep_look"), keep_ids(load("list"), ["AUTH-LOGIN-001"]),
                                  ids=["AUTH-LOGIN-001"])

        run_dir = self.evidence / result["runId"]
        stored = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "pass")
        self.assertEqual(stored["status"], "pass")
        self.assertEqual(stored["schema"], bv.SCHEMA_VERSION)
        self.assertEqual(stored["source"]["path"], "raw/playwright.json")
        self.assertEqual(stored["source"]["sha256"], bv.sha256_file(run_dir / "raw" / "playwright.json"))
        self.assertEqual(result["runJsonSha256"], bv.sha256_file(run_dir / "run.json"))
        self.assertTrue((run_dir / "summary.md").is_file())
        self.assertEqual(stored["toolVersion"], "1.63.0")
        self.assertTrue(stored["configHash"])

    def test_playwright_is_invoked_with_the_required_controls_and_no_shell(self) -> None:
        _, calls = self.run_with(load("grep_look"), keep_ids(load("list"), ["AUTH-LOGIN-001"]),
                                 ids=["AUTH-LOGIN-001"], base_url="http://127.0.0.1:4173")

        listing, execution = calls
        self.assertIn("--list", listing["argv"])
        for flag in ("--reporter=line,json", "--retries=1", "--trace=retain-on-first-failure", "--forbid-only"):
            self.assertIn(flag, execution["argv"])
        self.assertNotIn("--pass-with-no-tests", execution["argv"])
        grep = execution["argv"][execution["argv"].index("--grep") + 1]
        self.assertEqual(grep, "@AUTH-LOGIN-001(?![\\w-])")
        self.assertEqual(listing["argv"][listing["argv"].index("--grep") + 1], grep)
        self.assertIsInstance(execution["argv"], list)
        self.assertEqual(execution["cwd"], self.project)
        self.assertEqual(execution["env"]["BASE_URL"], "http://127.0.0.1:4173")
        self.assertTrue(execution["env"]["PLAYWRIGHT_JSON_OUTPUT_NAME"].endswith("raw/playwright.json"))
        self.assertFalse(any(a.startswith("--project") or a.startswith("-c") for a in execution["argv"]))

    def test_the_project_configuration_is_not_modified(self) -> None:
        before = (self.project / "playwright.config.ts").read_bytes()

        self.run_with(load("grep_look"), keep_ids(load("list"), ["AUTH-LOGIN-001"]), ids=["AUTH-LOGIN-001"])

        self.assertEqual((self.project / "playwright.config.ts").read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.project.iterdir()), ["playwright.config.ts"])

    def test_no_config_means_not_configured_and_runs_nothing(self) -> None:
        (self.project / "playwright.config.ts").unlink()

        result, calls = self.run_with(None, None)

        self.assertEqual((result["status"], result["reason"]), ("not_configured", "no_playwright_config"))
        self.assertEqual(calls, [])

    def test_an_unavailable_or_old_playwright_blocks_the_run(self) -> None:
        missing, calls = self.run_with(None, None, probe_version=lambda _: None)
        old, _ = self.run_with(None, None, probe_version=lambda _: "1.62.9")

        self.assertEqual((missing["status"], missing["reason"]), ("blocked", "playwright_unavailable"))
        self.assertEqual((old["status"], old["reason"]), ("blocked", "playwright_below_1.63.0"))
        self.assertEqual(calls, [])

    def test_a_listing_that_fails_is_an_error(self) -> None:
        result, _ = self.run_with(None, None, ids=["AUTH-LOGIN-001"])

        self.assertEqual((result["status"], result["reason"]), ("error", "list_failed"))

    def test_an_invalid_identity_registry_stops_before_playwright_runs(self) -> None:
        listing = keep_ids(load("list"), ["AUTH-LOGIN-001"])
        next(bv.iter_specs(listing))[2]["tags"] = ["behavioral"]
        result, calls = self.run_with(load("grep_look"), listing, ids=["AUTH-LOGIN-001"])

        self.assertEqual((result["status"], result["reason"]), ("error", "invalid_test_registry"))
        self.assertEqual(len(calls), 1)
        self.assertIn("--list", calls[0]["argv"])
        self.assertEqual(result["registryProblems"][0]["kind"], "unidentified")

    def test_a_run_that_times_out_is_an_error(self) -> None:
        listing = keep_ids(load("list"), ["AUTH-LOGIN-001"])

        def runner(argv, cwd, env, timeout):
            if "--list" in argv:
                Path(env["PLAYWRIGHT_JSON_OUTPUT_NAME"]).write_text(json.dumps(listing), encoding="utf-8")
                return 0
            return None

        result = bv.run_behavioral(self.project, self.evidence, ids=["AUTH-LOGIN-001"],
                                   runner=runner, probe_version=lambda _: "1.63.0")

        self.assertEqual((result["status"], result["reason"]), ("error", "timeout"))
        self.assertIsNone(result["exitCode"])

    def test_a_failing_run_writes_a_bundle_per_failing_test(self) -> None:
        listing = keep_ids(load("list"), ["RETRY-REPEAT-001"])
        result, _ = self.run_with(load("repeated_r1"), listing, ids=["RETRY-REPEAT-001"], code=1)

        run_dir = self.evidence / result["runId"]
        bundle = json.loads((run_dir / "failures" / "RETRY-REPEAT-001" / "bundle.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "fail")
        self.assertEqual(bundle["result"]["testId"], "RETRY-REPEAT-001")
        self.assertEqual(bundle["runId"], result["runId"])

    def test_the_run_records_commit_and_selection(self) -> None:
        result, _ = self.run_with(load("grep_look"), keep_ids(load("list"), ["AUTH-LOGIN-001"]),
                                  ids=["AUTH-LOGIN-001"])

        self.assertEqual(result["selection"]["kind"], "ids")
        self.assertEqual(result["selection"]["value"], "AUTH-LOGIN-001")
        self.assertIn("commit", result)
        self.assertIn("dirty", result)

    def test_no_playwright_output_is_left_behind_in_the_run(self) -> None:
        result, _ = self.run_with(load("grep_look"), keep_ids(load("list"), ["AUTH-LOGIN-001"]),
                                  ids=["AUTH-LOGIN-001"])

        names = sorted(p.name for p in (self.evidence / result["runId"]).iterdir())
        self.assertEqual(names, ["raw", "run.json", "summary.md"])


class EvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.output = self.base / "pw-output"
        self.output.mkdir()
        self.outside = self.base / "outside.txt"
        self.outside.write_text("secret", encoding="utf-8")

    def report_with_artifacts(self) -> dict:
        report = copy.deepcopy(load("repeated_r1"))
        result = spec_named(report, "R-REPEATED")["tests"][0]["results"][0]
        folder = self.output / "retry-R-REPEATED"
        folder.mkdir()
        (folder / "error-context.md").write_text("# error\n", encoding="utf-8")
        (folder / "test-failed-1.png").write_bytes(b"png")
        result["attachments"] = [
            {"name": "error-context", "contentType": "text/markdown", "path": str(folder / "error-context.md")},
            {"name": "screenshot", "contentType": "image/png", "path": str(folder / "test-failed-1.png")},
            {"name": "trace", "contentType": "application/zip", "path": str(folder / "gone.zip")},
            {"name": "leak", "contentType": "text/plain", "path": str(self.outside)},
        ]
        return report

    def persist(self) -> tuple[dict, Path]:
        report = self.report_with_artifacts()
        outcome = bv.evaluate(report, expected_for("RETRY-REPEAT-001"), 1, root=PROJECT)
        run = {"schema": 1, "runId": "bv-test", "status": outcome["status"], "reason": None,
               "commit": None, "dirty": None, "toolVersion": "1.63.0",
               "selection": {"kind": "ids", "value": "RETRY-REPEAT-001"},
               "errors": [], "counts": outcome["counts"], "results": outcome["results"]}
        run_dir = self.base / "run"
        bv.write_run(run_dir, run, json.dumps(report).encode("utf-8"), self.output)
        return run, run_dir

    def test_artifacts_are_collected_under_the_test_and_attempt(self) -> None:
        run, run_dir = self.persist()

        evidence = {e["kind"]: e for e in run["results"][0]["evidence"] if e["attempt"] == 0}
        self.assertEqual(evidence["error-context"]["path"],
                         "artifacts/RETRY-REPEAT-001/attempt-0/error-context.md")
        self.assertTrue((run_dir / evidence["screenshot"]["path"]).is_file())
        self.assertEqual(evidence["error-context"]["sha256"],
                         bv.sha256_file(run_dir / evidence["error-context"]["path"]))

    @unittest.skipIf(os.name != "posix", "POSIX file modes are not available")
    def test_evidence_modes_are_private_independent_of_umask(self) -> None:
        previous_umask = os.umask(0)
        try:
            _, run_dir = self.persist()
        finally:
            os.umask(previous_umask)

        directories = [run_dir, run_dir / "raw", run_dir / "artifacts",
                       run_dir / "artifacts" / "RETRY-REPEAT-001",
                       run_dir / "artifacts" / "RETRY-REPEAT-001" / "attempt-0",
                       run_dir / "failures", run_dir / "failures" / "RETRY-REPEAT-001"]
        files = [run_dir / "raw" / "playwright.json", run_dir / "run.json", run_dir / "summary.md",
                 run_dir / "failures" / "RETRY-REPEAT-001" / "bundle.json",
                 run_dir / "artifacts" / "RETRY-REPEAT-001" / "attempt-0" / "error-context.md",
                 run_dir / "artifacts" / "RETRY-REPEAT-001" / "attempt-0" / "test-failed-1.png"]

        self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in directories))
        self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files))

    def test_a_listed_artifact_that_does_not_exist_is_recorded_as_missing(self) -> None:
        run, _ = self.persist()

        trace = [e for e in run["results"][0]["evidence"] if e["kind"] == "trace" and e["attempt"] == 0][0]
        self.assertEqual(trace["status"], "missing")
        self.assertNotIn("path", trace)

    def test_an_attachment_outside_the_output_directory_is_never_copied(self) -> None:
        run, run_dir = self.persist()

        leak = [e for e in run["results"][0]["evidence"] if e["kind"] == "other"][0]
        self.assertEqual((leak["status"], leak["reason"]), ("not_collected", "outside_output_dir"))
        self.assertFalse(any(p.name == "outside.txt" for p in run_dir.rglob("*")))

    def test_the_stored_run_never_contains_a_source_path(self) -> None:
        _, run_dir = self.persist()

        self.assertNotIn(str(self.base), (run_dir / "run.json").read_text(encoding="utf-8"))

    def test_the_raw_report_is_kept_byte_for_byte_with_its_hash(self) -> None:
        run, run_dir = self.persist()

        self.assertEqual(run["source"]["sha256"], bv.sha256_file(run_dir / "raw" / "playwright.json"))
        self.assertTrue(run["source"]["retained"])


class SummaryTests(unittest.TestCase):
    def test_the_summary_is_short_and_shows_at_most_five_failures(self) -> None:
        outcome = bv.evaluate(load("run1"), bv.listed_tests(load("list")), 1)
        run = {"runId": "bv-x", "status": "fail", "reason": None, "commit": "abc123", "dirty": False,
               "toolVersion": "1.63.0", "selection": {"kind": "tag", "value": "behavioral"},
               "errors": [], "counts": outcome["counts"], "results": outcome["results"]}

        text = bv.render_summary(run)

        self.assertLessEqual(len(text.splitlines()), 80)
        self.assertIn("Failures (showing 5 of 9)", text)
        self.assertIn("FAIL-TOBE-001", text)
        self.assertIn('expected: "5" · observed: "3"', text)
        self.assertIn("(heuristic)", text)
        self.assertNotIn("\x1b", text)

    def test_a_run_level_error_is_shown(self) -> None:
        run = {"runId": "bv-x", "status": "error", "reason": "run_level_error", "commit": None,
               "dirty": None, "toolVersion": None, "selection": {"kind": "tag", "value": "behavioral"},
               "errors": ["Error: item focused with '.only' is not allowed"],
               "counts": bv._empty_counts(1), "results": []}

        self.assertIn("run-level error: Error: item focused with '.only'", bv.render_summary(run))


class NormalizeTests(unittest.TestCase):
    def test_a_ci_run_is_normalised_from_its_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "raw.json").write_text(json.dumps(load("grep_look")), encoding="utf-8")
            (base / "list.json").write_text(
                json.dumps(keep_ids(load("list"), ["AUTH-LOGIN-001"])), encoding="utf-8")

            result = bv.normalize(base / "raw.json", base / "list.json", 0, base / "evidence",
                                  selection_value="behavioral")

            self.assertEqual(result["status"], "pass")
            self.assertTrue((base / "evidence" / result["runId"] / "run.json").is_file())

    def test_a_ci_run_with_an_invalid_identity_registry_cannot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            listing = keep_ids(load("list"), ["AUTH-LOGIN-001"])
            next(bv.iter_specs(listing))[2]["tags"] = ["behavioral"]
            (base / "raw.json").write_text(json.dumps(load("grep_look")), encoding="utf-8")
            (base / "list.json").write_text(json.dumps(listing), encoding="utf-8")

            result = bv.normalize(base / "raw.json", base / "list.json", 0, base / "evidence")

            self.assertEqual((result["status"], result["reason"]), ("error", "invalid_test_registry"))
            self.assertEqual(result["registryProblems"][0]["kind"], "unidentified")

    def test_the_command_line_exits_with_the_status_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "raw.json").write_text(json.dumps(load("repeated_r1")), encoding="utf-8")
            (base / "list.json").write_text(
                json.dumps(keep_ids(load("list"), ["RETRY-REPEAT-001"])), encoding="utf-8")
            out = io.StringIO()

            with contextlib.redirect_stdout(out):
                code = bv.main(["normalize", "--report", str(base / "raw.json"),
                                "--list-report", str(base / "list.json"), "--exit-code", "1",
                                "--ids", "RETRY-REPEAT-001", "--project-dir", str(base),
                                "--evidence-dir", str(base / "evidence")])

            summary = json.loads(out.getvalue())
            self.assertEqual(code, bv.EXIT_CODES["fail"])
            self.assertEqual(summary["status"], "fail")
            self.assertEqual(summary["exitCode"], 10)
            self.assertTrue(summary["runJsonSha256"])

    def test_every_status_has_its_own_exit_code(self) -> None:
        codes = list(bv.EXIT_CODES.values())

        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(bv.EXIT_CODES["pass"], 0)
        self.assertTrue(all(code != 0 for status, code in bv.EXIT_CODES.items() if status != "pass"))

    def test_a_malformed_id_is_refused_by_the_command_line(self) -> None:
        err = io.StringIO()

        with contextlib.redirect_stderr(err), tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "r.json").write_text("{}", encoding="utf-8")
            code = bv.main(["run", "--project-dir", str(base), "--ids", "bad;id",
                            "--evidence-dir", str(base / "e")])

        self.assertEqual(code, bv.EXIT_CODES["error"])
        self.assertIn("not a test ID", err.getvalue())


class VersionTests(unittest.TestCase):
    def test_versions_are_parsed_from_playwright_output(self) -> None:
        self.assertEqual(bv.parse_version("Version 1.63.0"), (1, 63, 0))
        self.assertIsNone(bv.parse_version("not a version"))
        self.assertGreaterEqual((1, 63, 1), bv.MIN_PLAYWRIGHT)
        self.assertLess((1, 62, 9), bv.MIN_PLAYWRIGHT)


if __name__ == "__main__":
    unittest.main()
