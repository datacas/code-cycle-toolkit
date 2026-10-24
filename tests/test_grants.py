from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import grants  # noqa: E402


LINUX = grants.DOCKER_HOST.scope_for("codex", "linux")
#: The question an earlier run showed: Codex's Linux scope.
ASKED = frozenset({grants.DOCKER_HOST.fingerprint(LINUX)})


class GrantRegistryTests(unittest.TestCase):
    def test_docker_names_its_real_scope_not_only_the_resource(self) -> None:
        self.assertIsNotNone(LINUX)
        self.assertIn("every local Unix socket", LINUX.effective)
        self.assertIn("not only Docker's", LINUX.effective)
        self.assertIn("root", LINUX.risks)

    def test_a_scope_exists_only_where_it_was_verified(self) -> None:
        for executor, platform in (("codex", "macos"), ("codex", "windows"),
                                   ("claude", "macos"), ("claude", "windows"),
                                   ("orca", "linux")):
            with self.subTest(executor=executor, platform=platform):
                self.assertIsNone(grants.DOCKER_HOST.scope_for(executor, platform))

    def test_claude_has_a_compose_only_scope_on_the_same_platforms_as_codex(self) -> None:
        claude = grants.DOCKER_HOST.scope_for("claude", "linux")
        self.assertIsNotNone(claude)
        self.assertEqual({scope.platform for scope in grants.DOCKER_HOST.scopes
                          if scope.executor == "codex"},
                         {scope.platform for scope in grants.DOCKER_HOST.scopes
                          if scope.executor == "claude"})
        self.assertIn("docker compose up, down, ps, logs and build only", claude.effective)
        self.assertIn("docker compose run", claude.effective)
        self.assertIn("root", claude.risks)
        self.assertTrue(claude.isolated_per_dispatch)
        self.assertNotEqual(grants.DOCKER_HOST.fingerprint(LINUX),
                            grants.DOCKER_HOST.fingerprint(claude))

    def test_a_blocker_names_the_resource_or_mentions_it(self) -> None:
        local = {"kind": "sandbox_local_access", "command": "docker info", "error": "denied"}
        self.assertTrue(grants.DOCKER_HOST.matches(local))
        self.assertTrue(grants.DOCKER_HOST.matches(
            {"kind": "sandbox_network_access", "resource": "docker_host"}))
        self.assertFalse(grants.DOCKER_HOST.matches(
            {"kind": "sandbox_network_access", "command": "docker pull", "error": "dns"}))
        self.assertFalse(grants.DOCKER_HOST.matches(
            {"kind": "sandbox_local_access", "command": "psql", "error": "refused"}))

    def test_grant_options_are_parsed_strictly(self) -> None:
        self.assertEqual({"docker_host": "deny"},
                         grants.parse_grant_options(["docker_host=deny", "docker_host=deny"]))
        for values in (["docker_host"], ["docker_host=yes"], ["other=cycle"],
                       ["docker_host=cycle", "docker_host=deny"]):
            with self.subTest(values=values):
                with self.assertRaises(grants.GrantError):
                    grants.parse_grant_options(values)


class QuestionTests(unittest.TestCase):
    def test_the_question_fits_the_stop_questions_shape(self) -> None:
        asked = grants.question(grants.DOCKER_HOST, LINUX, role="implement")

        self.assertEqual("Q-001", asked["id"])
        self.assertLessEqual(len(asked["prompt"]), 600)
        self.assertTrue(2 <= len(asked["options"]) <= 4)
        self.assertTrue(all(len(option) <= 200 for option in asked["options"]))
        self.assertEqual(len(asked["options"]), len(set(asked["options"])))
        self.assertIsNone(asked["recommended"])
        self.assertEqual(["cycle", "persistent", "deny", "deny"],
                         [grants.answer_from_option(option) for option in asked["options"]])

    def test_the_question_says_which_later_stages_the_grant_reaches(self) -> None:
        for role in ("implement", "resolve", "rereview", "security", "verify"):
            with self.subTest(role=role):
                prompt = grants.question(grants.DOCKER_HOST, LINUX, role=role)["prompt"]
                self.assertTrue(prompt.endswith(grants.reach(LINUX)), prompt)
                self.assertIn("every later codex stage", prompt)
                self.assertIn("reviews of untrusted pull requests", prompt)
        self.assertIn("every later codex stage",
                      grants.granted_context(grants.DOCKER_HOST, LINUX))

    def test_a_temporary_answer_is_not_offered_when_it_cannot_stay_isolated(self) -> None:
        shared = grants.GrantScope("codex", "linux", "x", "y", isolated_per_dispatch=False)
        asked = grants.question(grants.DOCKER_HOST, shared, role="review")
        self.assertEqual(["persistent", "deny", "deny"],
                         [grants.answer_from_option(option) for option in asked["options"]])
        broker = grants.ConsentBroker("owner/api", platform="linux")
        with self.assertRaises(grants.GrantError):
            broker.apply_answer(grants.DOCKER_HOST, shared, "cycle")

    def test_the_terminal_prompt_maps_a_number_to_its_answer(self) -> None:
        asked = grants.question(grants.DOCKER_HOST, LINUX, role="implement")
        out = io.StringIO()
        answer = grants.terminal_prompt(io.StringIO("9\n3\n"), out)(asked)
        self.assertEqual("deny", answer)
        self.assertIn("every local Unix socket", out.getvalue())
        self.assertIsNone(grants.terminal_prompt(io.StringIO(""), io.StringIO())(asked))


class ConsentStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "config" / "consents.json"
        self.store = grants.ConsentStore(self.path)

    def test_an_allow_is_kept_per_repository_and_owner_only(self) -> None:
        self.store.save("owner/api", grants.DOCKER_HOST, LINUX, "allow")

        self.assertEqual("allow", self.store.load("owner/api", grants.DOCKER_HOST, LINUX))
        self.assertIsNone(self.store.load("owner/web", grants.DOCKER_HOST, LINUX))
        if os.name != "nt":
            self.assertEqual(0o600, self.path.stat().st_mode & 0o777)

    def test_a_wider_scope_no_longer_matches_the_stored_answer(self) -> None:
        self.store.save("owner/api", grants.DOCKER_HOST, LINUX, "allow")
        wider = grants.GrantScope("codex", "linux", LINUX.effective + " and more",
                                  LINUX.risks, isolated_per_dispatch=True)

        self.assertIsNone(self.store.load("owner/api", grants.DOCKER_HOST, wider))

    def test_only_decisions_are_stored_and_an_unreadable_store_is_refused(self) -> None:
        with self.assertRaises(grants.GrantError):
            self.store.save("owner/api", grants.DOCKER_HOST, LINUX, "unknown")
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"schema": 99, "repos": {}}), encoding="utf-8")
        with self.assertRaises(grants.GrantError):
            self.store.load("owner/api", grants.DOCKER_HOST, LINUX)

    def test_the_default_store_is_outside_the_checkout(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"CODE_CYCLE_CONSENT_FILE": "",
                                                   "XDG_CONFIG_HOME": "/xdg"}):
            if os.name != "nt":
                self.assertEqual(Path("/xdg/code-cycle/consents.json"),
                                 grants.default_store_path())


class BrokerTests(unittest.TestCase):
    def test_stored_grants_refresh_before_dispatch_and_revocation_is_immediate(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = grants.ConsentStore(Path(temporary) / "consents.json")
            store.save("owner/api", grants.DOCKER_HOST, LINUX, "allow")
            broker = grants.ConsentBroker("owner/api", platform="linux", store=store)
            self.assertEqual(("docker_host",), broker.granted("codex"))
            store.revoke("owner/api", grants.DOCKER_HOST, LINUX)
            self.assertEqual((), broker.granted("codex"))
            self.assertEqual("pending", broker.request(grants.DOCKER_HOST, "codex", role="review"))

    def test_explicit_persistent_answer_does_not_restore_a_revoked_grant(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = grants.ConsentStore(Path(temporary) / "consents.json")
            broker = grants.ConsentBroker("owner/api", platform="linux", store=store,
                                          answers={"docker_host": "persistent"}, asked=ASKED)
            self.assertEqual(("docker_host",), broker.granted("codex"))
            store.revoke("owner/api", grants.DOCKER_HOST, LINUX)
            self.assertEqual((), broker.granted("codex"))

    def test_an_explicit_answer_is_stored_only_for_the_scope_it_was_asked_for(self):
        claude = grants.DOCKER_HOST.scope_for("claude", "linux")
        for answer, status in (("persistent", "GRANTED"), ("deny", "DENIED")):
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as temporary:
                store = grants.ConsentStore(Path(temporary) / "consents.json")
                broker = grants.ConsentBroker("owner/api", platform="linux", store=store,
                                              answers={"docker_host": answer}, asked=ASKED)
                broker.granted("codex")
                broker.granted("claude")
                # Claude's scope was never shown: it has the answer for this run only.
                self.assertEqual([("codex", status)], [(entry["executor"], entry["status"])
                                                       for entry in store.list("owner/api")])
                self.assertIsNone(store.load("owner/api", grants.DOCKER_HOST, claude))
                self.assertEqual(answer != "deny",
                                 "docker_host" in broker.granted("claude"))
                unasked = grants.ConsentBroker("owner/api", platform="linux",
                                               store=grants.ConsentStore(Path(temporary) / "x.json"),
                                               answers={"docker_host": answer})
                unasked.granted("codex")
                self.assertFalse((Path(temporary) / "x.json").exists())
                self.assertTrue(any("kept for this run only on codex/linux" in note
                                    for note in unasked.notes))
                # A scope that changed since its question was shown counts as unasked.
                wider = grants.GrantScope("codex", "linux", LINUX.effective + " and more",
                                          LINUX.risks, isolated_per_dispatch=True)
                changed = grants.ConsentBroker("owner/api", platform="linux",
                                               store=grants.ConsentStore(Path(temporary) / "y.json"),
                                               answers={"docker_host": answer}, asked=ASKED)
                changed._resolve(grants.DOCKER_HOST, wider)
                self.assertFalse((Path(temporary) / "y.json").exists())

    def test_revalidation_audits_only_a_changed_decision(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = grants.ConsentStore(Path(temporary) / "consents.json")
            store.save("owner/api", grants.DOCKER_HOST, LINUX, "allow")
            seen = []
            broker = grants.ConsentBroker("owner/api", platform="linux", store=store,
                                          observer=lambda *event: seen.append(event[4]))
            for _ in range(3):
                broker.granted("codex")
            self.assertEqual(["persistent"], seen)
            store.revoke("owner/api", grants.DOCKER_HOST, LINUX)
            broker.granted("codex")
            store.save("owner/api", grants.DOCKER_HOST, LINUX, "deny")
            broker.granted("codex")
            broker.granted("codex")
            self.assertEqual(["persistent", "deny"], seen)

    def test_executor_scopes_coexist_and_configuration_remains_visible_after_revoke(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = grants.ConsentStore(Path(temporary) / "consents.json")
            claude = grants.DOCKER_HOST.scope_for("claude", "linux")
            store.save("owner/api", grants.DOCKER_HOST, LINUX, "allow")
            store.save("owner/api", grants.DOCKER_HOST, claude, "deny")
            self.assertEqual(2, len(store.list("owner/api")))
            store.revoke("owner/api", grants.DOCKER_HOST, LINUX)
            self.assertEqual("deny", store.load("owner/api", grants.DOCKER_HOST, claude))
            broker = grants.ConsentBroker("owner/api", platform="linux", store=store,
                                          preauthorized=frozenset({"docker_host"}))
            effective = [entry for entry in broker.listing("codex") if entry.get("effective")]
            self.assertEqual("project configuration", effective[0]["source"])
            self.assertEqual("GRANTED", effective[0]["status"])

    def test_schema_one_grants_migrate_without_widening_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "consents.json"
            path.write_text(json.dumps({"schema": 1, "repos": {"owner/api": {"docker_host": {
                "decision": "allow", "fingerprint": grants.DOCKER_HOST.fingerprint(LINUX),
                "executor": "codex", "platform": "linux"}}}}))
            store = grants.ConsentStore(path)
            self.assertEqual("allow", store.load("owner/api", grants.DOCKER_HOST, LINUX))
            self.assertIsNone(store.load("owner/api", grants.DOCKER_HOST,
                                         grants.DOCKER_HOST.scope_for("claude", "linux")))

    def test_a_denial_is_persistent_until_revoked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = grants.ConsentStore(Path(temporary) / "consents.json")
            broker = grants.ConsentBroker("owner/api", platform="linux", store=store,
                                          answers={"docker_host": "deny"}, asked=ASKED)
            self.assertEqual("deny", broker.request(grants.DOCKER_HOST, "codex", role="implement"))
            self.assertEqual((), broker.granted("codex"))
            self.assertEqual("deny", broker.request(grants.DOCKER_HOST, "codex", role="review"))
            self.assertEqual("deny", store.load("owner/api", grants.DOCKER_HOST, LINUX))
            fresh = grants.ConsentBroker("owner/api", platform="linux", store=store,
                                         prompt=lambda q: self.fail("asked twice"))
            self.assertEqual("deny", fresh.request(grants.DOCKER_HOST, "codex", role="review"))
            self.assertEqual("DENIED", store.list("owner/api")[0]["status"])
            self.assertTrue(store.revoke("owner/api", grants.DOCKER_HOST, LINUX))
            self.assertIsNone(store.load("owner/api", grants.DOCKER_HOST, LINUX))
            self.assertEqual("deny", json.loads(store.path.read_text())["revocations"][0]["decision"])

    def test_a_grant_reaches_only_executors_with_a_scope(self) -> None:
        broker = grants.ConsentBroker("owner/api", platform="linux",
                                      answers={"docker_host": "cycle"})
        broker.request(grants.DOCKER_HOST, "codex", role="implement")

        self.assertEqual(("docker_host",), broker.granted("codex"))
        # An explicit cycle answer authorizes each registered executor scope.
        self.assertEqual(("docker_host",), broker.granted("claude"))
        self.assertEqual((), broker.granted("orca"))

    def test_an_unreadable_store_grants_nothing_and_asks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "consents.json"
            path.write_text("{not json", encoding="utf-8")
            broker = grants.ConsentBroker("owner/api", platform="linux",
                                          store=grants.ConsentStore(path))
            self.assertEqual("pending", broker.request(grants.DOCKER_HOST, "codex",
                                                       role="implement"))
            self.assertTrue(any("unreadable consent store" in note for note in broker.notes))

    def test_without_anybody_to_ask_the_request_stays_pending(self) -> None:
        broker = grants.ConsentBroker("owner/api", platform="linux")

        self.assertEqual("pending", broker.request(grants.DOCKER_HOST, "codex", role="resolve"))
        self.assertEqual((), broker.granted("codex"))
        self.assertEqual("docker_host", broker.pending[0].record()["grant"])
        self.assertIn("--grant docker_host=", broker.pending[0].record()["answer_with"])


if __name__ == "__main__":
    unittest.main()
