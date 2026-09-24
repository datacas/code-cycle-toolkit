"""Jev in shadow mode: observed beside the rules, never in their place."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import jev_shadow as js  # noqa: E402
import router  # noqa: E402
import run_cycle as rc  # noqa: E402
import telemetry as tm  # noqa: E402
from test_cycle import CycleTestCase, ScriptedAdapter  # noqa: E402
from test_run_cycle import RunCycleTestCase, Talker, block  # noqa: E402

KEY = "jev-test-key-0123456789"
SHADOW = js.JevConfig(js.JevMode.SHADOW)


def answer(choice="cheap_coder", *, confidence=0.8, probabilities=None,
           model=None) -> bytes:
    profile = {"type": "choice", "choice": choice}
    if confidence is not None:
        profile["confidence"] = confidence
    if probabilities is not None:
        profile["probabilities"] = probabilities
    return json.dumps({
        "model": model or "jev-1.13.0",
        "answers": {"profile": profile},
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }).encode()


class FakeTransport:
    """Records every request and answers from a script."""

    def __init__(self, status=200, body=None, raises=None):
        self.status, self.body, self.raises = status, body, raises
        self.calls: list[dict] = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append({"url": url, "body": json.loads(body), "headers": headers,
                           "timeout": timeout, "raw": body})
        if self.raises is not None:
            raise self.raises
        return self.status, answer() if self.body is None else self.body


def shadow(transport, key=KEY):
    return js.JevShadow(SHADOW, transport=transport,
                        environ={} if key is None else {js.API_KEY_ENV: key})


class ConfigTests(unittest.TestCase):
    def test_absent_configuration_is_disabled(self) -> None:
        for config in (None, {}, {"code_cycle": {}}, {"code_cycle": {"routing": {}}}):
            self.assertFalse(js.load_jev_config(config).enabled)

    def test_disabled_and_shadow_are_the_only_modes(self) -> None:
        def load(mode):
            return js.load_jev_config({"code_cycle": {"routing": {"jev": {"mode": mode}}}})

        self.assertFalse(load("disabled").enabled)
        self.assertTrue(load("shadow").enabled)
        for mode in ("jev", "production", "on", True):
            with self.assertRaises(js.JevConfigError):
                load(mode)

    def test_unknown_keys_models_and_timeouts_are_refused(self) -> None:
        for jev in ({"mode": "shadow", "api_key": "x"},
                    {"mode": "shadow", "model": "gpt-6-luna"},
                    {"mode": "shadow", "timeout_seconds": 0},
                    {"mode": "shadow", "timeout_seconds": 60},
                    {"mode": "shadow", "timeout_seconds": True},
                    "shadow"):
            with self.subTest(jev=jev), self.assertRaises(js.JevConfigError):
                js.load_jev_config({"code_cycle": {"routing": {"jev": jev}}})

        configured = js.load_jev_config({"code_cycle": {"routing": {"jev": {
            "mode": "shadow", "model": "jev-1.14.0",
        }}}})
        self.assertEqual("jev-1.14.0", configured.model)

        legacy = js.load_jev_config({"code_cycle": {"routing": {"jev": {
            "mode": "shadow", "model": "typesafe-ai/jev",
        }}}})
        self.assertEqual(js.DEFAULT_MODEL, legacy.model)

    def test_the_driver_refuses_a_bad_block_before_anything_runs(self) -> None:
        for text, named in (
            ("code_cycle:\n  routing:\n    jev:\n      mod: shadow\n", "mod"),
            ("code_cycle:\n  routing:\n    jevv:\n      mode: shadow\n", "jevv"),
            ("code_cycle:\n  routing:\n    jev:\n      mode: jev\n", "jev"),
        ):
            with self.subTest(named=named), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / ".code-cycle.yml"
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(rc.CycleDriverError) as raised:
                    rc.load_config(path)
                self.assertIn(named, str(raised.exception))

    def test_a_shadow_is_never_built_for_a_disabled_mode(self) -> None:
        with self.assertRaises(js.JevConfigError):
            js.JevShadow(js.JevConfig())


class PayloadTests(unittest.TestCase):
    def test_only_allowlisted_scalar_signals_are_sent(self) -> None:
        transport = FakeTransport()
        shadow(transport).suggest("implement", {
            "difficulty": 3, "verifiability": "partial", "security_sensitive": True,
            "previous_failed_attempts": 1, "changed_files_count": 4,
            "touches_auth": False,
            # Nothing below is a pre-routing signal, or a scalar one.
            "repo_id": "owner/repo", "task_id": "API-7", "cycle_id": "cycle-1",
            "path": "src/auth/login.py", "diff": "+secret", "status": "APPROVED",
            "findings_total": 3, "tests_passed": True, "verifiability_note": "x",
            "changed_other_files": -1,
        })

        request = transport.calls[0]["body"]
        self.assertEqual({"model", "state", "questions"}, set(request))
        self.assertEqual({
            "role": "implement", "difficulty": 3, "verifiability": "partial",
            "security_sensitive": True, "previous_failed_attempts": 1,
            "changed_files_count": 4, "touches_auth": False,
        }, request["state"])
        self.assertTrue(set(request["state"]) <= js.PAYLOAD_FIELDS)

    def test_the_question_is_a_fixed_choice_between_the_two_coders(self) -> None:
        transport = FakeTransport()
        shadow(transport).suggest("resolve", {"verifiability": "free text"})

        request = transport.calls[0]["body"]
        self.assertEqual("jev-latest", request["model"])
        self.assertEqual({"role": "resolve"}, request["state"])
        question = request["questions"]["profile"]
        self.assertEqual("choice", question["type"])
        self.assertEqual({"cheap_coder", "deep_coder"}, set(question["criteria"]))

    def test_the_key_travels_only_in_the_authorization_header(self) -> None:
        transport = FakeTransport()
        shadow(transport).suggest("implement", {"difficulty": 2})

        call = transport.calls[0]
        self.assertEqual(js.JEV_URL, call["url"])
        self.assertTrue(call["url"].startswith("https://"))
        self.assertEqual(f"Bearer {KEY}", call["headers"]["Authorization"])
        self.assertEqual(js.USER_AGENT, call["headers"]["User-Agent"])
        self.assertNotIn(KEY.encode(), call["raw"])
        self.assertEqual(3.0, call["timeout"])

    def test_typesafe_key_wins_and_legacy_key_is_a_fallback(self) -> None:
        for environ, expected in (
            ({js.API_KEY_ENV: "typesafe-key", js.LEGACY_API_KEY_ENV: "legacy-key"},
             "Bearer typesafe-key"),
            ({js.LEGACY_API_KEY_ENV: "legacy-key"}, "Bearer legacy-key"),
        ):
            with self.subTest(expected=expected):
                transport = FakeTransport()
                js.JevShadow(SHADOW, transport=transport, environ=environ).suggest(
                    "implement", {"difficulty": 2})
                self.assertEqual(expected, transport.calls[0]["headers"]["Authorization"])


class ResponseTests(unittest.TestCase):
    def parse(self, status=200, body=None, model="jev-latest"):
        return js.parse_response(status, answer() if body is None else body,
                                 model)

    def test_a_typed_choice_is_read_with_its_evidence(self) -> None:
        suggestion = self.parse(body=answer(
            "deep_coder", confidence=0.7,
            probabilities={"cheap_coder": 0.3, "deep_coder": 0.7},
            model="jev-1.13.0"))

        self.assertEqual("suggested", suggestion.status)
        self.assertEqual("deep_coder", suggestion.profile)
        self.assertEqual(0.7, suggestion.confidence)
        self.assertEqual({"cheap_coder": 0.3, "deep_coder": 0.7}, suggestion.probabilities)
        self.assertEqual(("jev-1.13.0", "matched"),
                         (suggestion.model_resolved, suggestion.model_resolution))

    def test_what_the_service_does_not_report_stays_unknown(self) -> None:
        body = json.loads(answer(confidence=None))
        body.pop("model")
        suggestion = self.parse(body=json.dumps(body).encode())

        self.assertEqual("suggested", suggestion.status)
        self.assertIsNone(suggestion.confidence)
        self.assertIsNone(suggestion.probabilities)
        self.assertIsNone(suggestion.model_resolved)
        self.assertEqual("unreported", suggestion.model_resolution)

    def test_an_unknown_reported_model_is_not_stored_by_name(self) -> None:
        suggestion = self.parse(body=answer(model="sk-live-abc123"))

        self.assertIsNone(suggestion.model_resolved)
        self.assertEqual("mismatch_unrecognized", suggestion.model_resolution)

    def test_latest_alias_matches_any_concrete_reported_version(self) -> None:
        suggestion = self.parse(body=answer(model="jev-1.14.0"))

        self.assertEqual(("jev-1.14.0", "matched"),
                         (suggestion.model_resolved, suggestion.model_resolution))

    def test_a_concrete_request_reports_version_drift(self) -> None:
        suggestion = self.parse(body=answer(model="jev-1.14.0"), model="jev-1.13.0")

        self.assertEqual(("jev-1.14.0", "mismatch_known"),
                         (suggestion.model_resolved, suggestion.model_resolution))

    def test_failures_become_closed_categories(self) -> None:
        cases = (
            ("rate_limited", 429, b"slow down"),
            ("unavailable", 503, b"<html>maintenance</html>"),
            ("http_error", 401, b"bad key"),
            ("http_error", 403, b"forbidden"),
        )
        for expected, status, body in cases:
            with self.subTest(expected=expected):
                self.assertEqual(expected, self.parse(status, body).status)

    def test_anything_but_a_valid_choice_is_an_invalid_response(self) -> None:
        for body in (
            b"not json", b"\xff\xfe", b"[]",
            answer("senior_reviewer"),
            answer("Use the cheap coder, it is fine"),
            answer(confidence=1.5),
            answer(confidence="high"),
            answer(probabilities={"cheap_coder": 0.4, "reviewer": 0.6}),
            answer(probabilities={"cheap_coder": 2}),
            json.dumps({"model": "jev-latest", "answers": {
                "profile": {"type": "noul", "noul": 0.7}},
                "usage": {"input_tokens": 1, "output_tokens": 1}}).encode(),
            json.dumps({"code": 0, "data": {"answers": {
                "profile": {"choice": "cheap_coder"}}}}).encode(),
            b" " * (js.MAX_RESPONSE_BYTES + 1),
        ):
            with self.subTest(body=body[:40]):
                self.assertEqual("invalid_response", self.parse(body=body).status)


class SuggestTests(unittest.TestCase):
    def test_without_a_key_nothing_is_sent(self) -> None:
        transport = FakeTransport()

        suggestion = shadow(transport, key=None).suggest("implement", {})

        self.assertEqual("unavailable", suggestion.status)
        self.assertEqual([], transport.calls)

    def test_transport_failures_never_raise(self) -> None:
        for raised, expected in ((TimeoutError(), "timeout"),
                                 (OSError("connection refused"), "unavailable"),
                                 (RuntimeError("boom"), "invalid_response")):
            with self.subTest(expected=expected):
                suggestion = shadow(FakeTransport(raises=raised)).suggest("implement", {})
                self.assertEqual(expected, suggestion.status)
                self.assertIsNone(suggestion.profile)

    def test_the_urllib_transport_maps_errors_without_keeping_them(self) -> None:
        import socket
        import urllib.error

        def fails(reason):
            opener = unittest.mock.Mock()
            opener.open.side_effect = urllib.error.URLError(reason)
            return opener

        with unittest.mock.patch("urllib.request.build_opener",
                                 return_value=fails(socket.timeout())) as build_opener:
            with self.assertRaises(TimeoutError):
                js.urllib_transport(js.JEV_URL, b"{}", {}, 1)
            self.assertIsInstance(build_opener.call_args.args[0], js._NoRedirectHandler)
        with unittest.mock.patch("urllib.request.build_opener",
                                 return_value=fails("refused")):
            with self.assertRaises(OSError) as raised:
                js.urllib_transport(js.JEV_URL, b"{}", {}, 1)
        self.assertEqual((), raised.exception.args)

    def test_redirects_are_not_followed_to_another_host(self) -> None:
        import urllib.request

        request = urllib.request.Request(js.JEV_URL, data=b"{}", method="POST")
        redirect = js._NoRedirectHandler().redirect_request(
            request, None, 302, "Found", {}, "https://attacker.example/collect")

        self.assertIsNone(redirect)


class RecorderShadowTests(CycleTestCase):
    def adapters(self):
        return [ScriptedAdapter("codex"), ScriptedAdapter("claude")]

    def run_stages(self, transport, *, difficulty=2):
        recorder = self.recorder(self.adapters(), shadow=shadow(transport))
        recorder.signals = router.TaskSignals(difficulty=difficulty)
        for role in ("implement", "review", "resolve", "rereview"):
            recorder.stage(role, role)
        recorder.close("READY_FOR_MANUAL_MERGE")
        return recorder

    def kinds(self):
        return [(row["role"], row["payload"].get("record_kind"))
                for row in self.store.rows("owner/repo")]

    def test_only_implement_and_resolve_are_asked(self) -> None:
        transport = FakeTransport()
        self.run_stages(transport)

        self.assertEqual(["implement", "resolve"],
                         [call["body"]["state"]["role"] for call in transport.calls])
        self.assertEqual([
            ("implement", "dispatch"), ("implement", "shadow"),
            ("review", "dispatch"),
            ("resolve", "dispatch"), ("resolve", "shadow"),
            ("rereview", "dispatch"), ("coordinate", "cycle"),
        ], self.kinds())

    def test_a_disagreeing_suggestion_changes_no_dispatch(self) -> None:
        baseline = self.recorder(self.adapters())
        baseline.signals = router.TaskSignals(difficulty=3)
        expected = baseline.stage("implement", "implement").decision

        transport = FakeTransport(body=answer("cheap_coder", confidence=0.99))
        recorder = self.recorder(self.adapters(), shadow=shadow(transport))
        recorder.signals = router.TaskSignals(difficulty=3)
        outcome = recorder.stage("implement", "implement")
        outcome_rows = [row for row in self.store.rows("owner/repo")
                        if row["payload"].get("cycle_id") == recorder.cycle_id]

        self.assertEqual(expected.profile, outcome.decision.profile)
        self.assertEqual("deep_coder", outcome.decision.profile)
        self.assertEqual(expected.target, outcome.decision.target)
        self.assertEqual(expected.used_fallback, outcome.decision.used_fallback)
        dispatch, suggestion = outcome_rows
        self.assertEqual("deep_coder", dispatch["profile"])
        self.assertEqual({
            "jev_status": "suggested", "jev_rule_profile": "deep_coder",
            "jev_suggested_profile": "cheap_coder", "jev_agreement": False,
            "jev_confidence": 0.99, "jev_model_requested": "jev-latest",
            "jev_model_resolved": "jev-1.13.0", "jev_model_resolution": "matched",
        }, {key: value for key, value in suggestion["payload"].items()
            if key.startswith("jev_") and key != "jev_duration_ms"})
        self.assertIsNone(suggestion["profile"])

    def test_a_failing_service_leaves_the_cycle_as_it_was(self) -> None:
        for raised in (TimeoutError(), OSError(), ValueError()):
            with self.subTest(raised=type(raised).__name__):
                recorder = self.recorder(
                    self.adapters(), shadow=shadow(FakeTransport(raises=raised)))
                outcome = recorder.stage("implement", "implement")
                self.assertTrue(outcome.succeeded)
                self.assertEqual("cheap_coder", outcome.decision.profile)

    def test_a_suggestion_the_store_refuses_does_not_stop_the_stage(self) -> None:
        recorder = self.recorder(self.adapters(), shadow=shadow(FakeTransport()))
        with unittest.mock.patch("cycle.shadow_fields",
                                 return_value={"jev_confidence": 7.0}):
            outcome = recorder.stage("implement", "implement")

        self.assertTrue(outcome.succeeded)
        self.assertEqual(["dispatch"], [row["payload"]["record_kind"]
                                        for row in self.store.rows("owner/repo")])

    def test_the_shadow_row_holds_no_signal_and_no_outcome(self) -> None:
        recorder = self.run_stages(FakeTransport(body=answer(
            "deep_coder", probabilities={"cheap_coder": 0.2, "deep_coder": 0.8},
            model="jev-latest")))

        allowed = tm.SHADOW_FIELDS | tm.CORRELATION_FIELDS | {
            "routing_strategy", "local_only"}
        for row in self.store.rows("owner/repo"):
            if row["payload"].get("record_kind") != "shadow":
                self.assertFalse({k for k in row["payload"] if k.startswith("jev_")})
                continue
            # No pre-routing signal and no outcome, in payload or in a column.
            self.assertTrue(set(row["payload"]) <= allowed, set(row["payload"]) - allowed)
            self.assertEqual(set(), {
                key for key in tm._COLUMNS
                if key not in ("repo_id", "task_id", "role", "iteration")
                and row[key] not in (None, 0)})

        cycle = self.store.cycle_outcome("owner/repo", recorder.cycle_id)
        self.assertEqual([1, 3], [entry["stage_seq"] for entry in cycle["shadows"]])
        self.assertEqual(0.8, cycle["shadows"][0]["jev_probability_deep_coder"])
        self.assertEqual("matched", cycle["shadows"][0]["jev_model_resolution"])
        self.assertTrue(cycle["shadows"][0]["jev_agreement"] is False)
        self.assertEqual(4, len(cycle["dispatches"]))

    def test_a_shadow_row_is_not_read_as_the_implementer(self) -> None:
        for index in range(10):
            task = f"API-{index}"
            # Even a suggestion written first must not name the implementer.
            self.store.record_stage("owner/repo", task, "implement",
                                    record_kind="shadow", jev_status="suggested",
                                    jev_suggested_profile="deep_coder")
            self.store.record_stage("owner/repo", task, "implement",
                                    record_kind="dispatch", profile="cheap_coder")
            self.store.record_stage("owner/repo", task, "review",
                                    record_kind="verdict", status="APPROVED")

        rate = self.store.first_pass_rate("owner/repo", "cheap_coder")
        self.assertEqual(10, rate.observations)
        self.assertEqual(1.0, rate.value)


class DriverShadowTests(RunCycleTestCase):
    def test_disabled_builds_no_client_and_opens_no_socket(self) -> None:
        with unittest.mock.patch.object(rc, "JevShadow",
                                        side_effect=AssertionError("built")), \
                unittest.mock.patch("urllib.request.urlopen",
                                    side_effect=AssertionError("network")):
            for jev in (None, js.JevConfig(), js.load_jev_config({})):
                self.run_cycle(Talker("codex", block("IMPLEMENTED")), Talker("claude"),
                               jev=jev)

        self.assertFalse([row for row in self.rows()
                          if row["payload"].get("record_kind") == "shadow"])

    def test_shadow_without_a_key_records_unavailable_and_carries_on(self) -> None:
        with unittest.mock.patch.dict("os.environ", {}, clear=True), \
                unittest.mock.patch("urllib.request.urlopen",
                                    side_effect=AssertionError("network")):
            report = self.run_cycle(Talker("codex", block("IMPLEMENTED")),
                                    Talker("claude", block("APPROVED")), jev=SHADOW)

        self.assertEqual(rc.APPROVED_END, report.status)
        shadows = [row for row in self.rows()
                   if row["payload"].get("record_kind") == "shadow"]
        self.assertEqual(1, len(shadows))
        self.assertEqual("unavailable", shadows[0]["payload"]["jev_status"])
        self.assertEqual("cheap_coder", shadows[0]["payload"]["jev_rule_profile"])
        self.assertNotIn("jev_suggested_profile", shadows[0]["payload"])

    def test_the_rules_decide_the_same_run_with_or_without_the_shadow(self) -> None:
        def dispatches():
            return [(row["role"], row["profile"], row["executor"], row["model_requested"],
                     row["effort"], row["used_fallback"], row["outcome"])
                    for row in self.rows()
                    if row["payload"].get("record_kind") == "dispatch"]

        self.run_cycle(Talker("codex", block("IMPLEMENTED")),
                       Talker("claude", block("APPROVED")))
        without = dispatches()
        self.run_cycle(Talker("codex", block("IMPLEMENTED")),
                       Talker("claude", block("APPROVED")),
                       shadow=shadow(FakeTransport(body=answer("deep_coder"))))

        self.assertEqual(without * 2, dispatches())


class TelemetryShapeTests(unittest.TestCase):
    def test_probabilities_outside_the_unit_interval_are_refused(self) -> None:
        for key in tm.PROBABILITY_FIELDS:
            with self.subTest(key=key):
                self.assertEqual(0.5, tm.validate_reference(key, 0.5))
                with self.assertRaises(tm.TelemetryError):
                    tm.validate_reference(key, 1.5)

    def test_the_suggestion_vocabulary_is_closed(self) -> None:
        with self.assertRaises(tm.TelemetryError):
            tm.validate_reference("jev_suggested_profile", "reviewer")
        with self.assertRaises(tm.TelemetryError):
            tm.validate_reference("jev_model_requested", "gpt-6-luna")
        self.assertEqual("jev-1.14.0",
                         tm.validate_reference("jev_model_resolved", "jev-1.14.0"))
        with self.assertRaises(tm.TelemetryError):
            tm.validate_reference("jev_model_resolved", "sk-live-secret")
        self.assertFalse(tm.is_jev_model("typesafe-ai/jev"))
        self.assertEqual(js.STATUSES, tm.FIELD_SPECS["jev_status"][1])
        self.assertEqual(set(js.OPTIONS), set(router.ROLE_CANDIDATES["implement"]))
        self.assertEqual(set(js.OPTIONS), set(router.ROLE_CANDIDATES["resolve"]))
        self.assertEqual(set(js.OPTIONS), tm.SHADOW_PROFILES)


if __name__ == "__main__":
    unittest.main()
