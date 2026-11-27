from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import grants  # noqa: E402
import reduced_guarantees as reduced  # noqa: E402
import router  # noqa: E402
import stage_permissions as sp  # noqa: E402


class ReducedGuaranteeConsentTests(unittest.TestCase):
    def record(self, **changes):
        value = {
            "setting": "disableAllHooks", "value": True,
            "source": "/user/settings.json", "layer": "user",
            "executor": "claude", "guarantees": ["stage command hook"],
            "provenance": "trusted_user_configuration",
        }
        value.update(changes)
        return reduced.binding(**value)

    def test_binding_changes_when_any_authorized_dimension_changes(self):
        baseline = self.record()
        for field, value in (
            ("value", False), ("source", "/other/settings.json"),
            ("layer", "repository"), ("executor", "codex"),
            ("guarantees", ["filesystem confinement"]),
            ("provenance", "untrusted_change"),
        ):
            with self.subTest(field=field):
                self.assertNotEqual(baseline["fingerprint"], self.record(**{field: value})["fingerprint"])

    def test_question_names_risk_and_only_offers_the_allowed_scopes(self):
        for role, policy, expected in (
            ("implement", "cycle_repository_global", {"this cycle", "this repository", "forever", "deny"}),
            ("review", "cycle_repository", {"this cycle", "this repository", "deny"}),
            ("review", "cycle", {"this cycle", "deny"}),
        ):
            record = self.record(provenance=("untrusted_change" if policy == "cycle" else "trusted_user_configuration"))
            grant = reduced.grant_for(record, "linux", role)
            asked = grants.question(grant, grant.scopes[0], role=role)
            self.assertEqual(set(asked["options"]), expected)
            self.assertIn("reduced_guarantee_consent", asked["prompt"])
            for operation in ("force-push", "base-push", "ref deletion", "credential access"):
                self.assertIn(operation, asked["prompt"])

    def test_repository_or_global_answers_cannot_escape_the_bound_policy(self):
        for provenance, role, answer in (
            ("untrusted_change", "review", grants.ALLOW_PERSISTENT),
            ("trusted_user_configuration", "review", grants.ALLOW_GLOBAL),
        ):
            grant = reduced.grant_for(self.record(provenance=provenance), "linux", role)
            broker = grants.ConsentBroker("owner/repo", platform="linux")
            with self.assertRaises(grants.GrantError):
                broker.apply_answer(grant, grant.scopes[0], answer)

    def test_the_same_bound_setting_is_asked_once_and_the_answer_is_audited(self):
        prompts = []
        events = []
        broker = grants.ConsentBroker(
            "owner/repo", platform="linux", prompt=lambda question: (prompts.append(question) or
                                                                       grants.ALLOW_CYCLE),
            observer=lambda *event: events.append(event))
        record = self.record()
        grant = reduced.grant_for(record, "linux", "implement")
        self.assertEqual(grants.ALLOW_CYCLE,
                         broker.request(grant, "claude", role="implement"))
        self.assertEqual(grants.ALLOW_CYCLE,
                         broker.request(grant, "claude", role="implement"))
        self.assertEqual(1, len(prompts))
        self.assertTrue(any(event[0] == "decision_asked" for event in events))
        self.assertTrue(any(event[0] == "decision_answered" and
                            event[1].operation.get("kind") == "reduced_guarantee_consent"
                            for event in events))
        self.assertEqual(grant.id, reduced.grant_for(record, "linux", "review").id)

    def test_claude_user_settings_are_classified_and_managed_hooks_remain_a_limit(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "claude"
            config.mkdir()
            settings = config / "settings.json"
            settings.write_text('{"disableAllHooks": true}', encoding="utf-8")
            overrides, limits = sp.reduced_guarantee_overrides(
                config_dir=config, managed=(), policy=None)
            self.assertEqual([], limits)
            self.assertEqual("disableAllHooks", overrides[0]["setting"])
            managed = Path(root) / "managed-settings.json"
            managed.write_text('{"allowManagedHooksOnly": true}', encoding="utf-8")
            overrides, limits = sp.reduced_guarantee_overrides(
                config_dir=config, managed=(managed,), policy=None)
            self.assertTrue(any("allowManagedHooksOnly" in item for item in limits))

    def test_tool_check_plugin_fingerprint_tracks_its_enabled_source_tree(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "claude"
            plugin = config / "plugins" / "plugin"
            plugin.mkdir(parents=True)
            (plugin / "hooks.js").write_text("on('tool.check', () => true)", encoding="utf-8")
            (config / "plugins" / "installed_plugins.json").write_text(
                json.dumps({"plugins": {
                    "guard@market": [{"installPath": str(plugin)}]}}), encoding="utf-8")
            (config / "settings.json").write_text(json.dumps(
                {"enabledPlugins": {"guard@market": True}}), encoding="utf-8")
            first, _ = sp.reduced_guarantee_overrides(config_dir=config, managed=())
            (plugin / "hooks.js").write_text("on('tool.check', () => false)", encoding="utf-8")
            second, _ = sp.reduced_guarantee_overrides(config_dir=config, managed=())
            self.assertEqual("tool.check plugin", first[0]["setting"])
            self.assertNotEqual(first[0]["fingerprint"], second[0]["fingerprint"])

    def test_claude_reduced_guarantee_direct_baseline(self):
        """An allowed direct Claude operation remains dispatchable after disclosure."""
        record = self.record()
        grant = reduced.grant_for(record, "linux", "implement")
        adapter = __import__("executors").ClaudeAdapter()
        policy = sp.StagePolicy(role="implement", repo="owner/repo", base_branch="main")
        # The direct Claude invocation is represented by the runner. The
        # toolkit proceeds only after the exact, configuration-bound decision.
        seen = []
        target = router.parse_target("claude:anthropic/claude-sonnet-5 high")
        with unittest.mock.patch.object(
            sp, "reduced_guarantee_overrides", return_value=([record], [])
        ):
            self.assertIsNotNone(adapter.policy_preflight(policy, ".", ()))
            self.assertIsNone(adapter.policy_preflight(policy, ".", (grant.id,)))
            result = adapter.dispatch(
                target, "perform the requested stage operation", cwd=".", writes=True,
                command_policy=policy, reduced_guarantee_authorizations=(grant.id,),
                runner=lambda argv, **kwargs: (seen.append(argv) or subprocess.CompletedProcess(
                    argv, 0, '{"model":"claude-sonnet-5"}', "")))
        self.assertEqual("succeeded", result.outcome.value, result.detail)
        self.assertEqual(1, len(seen))

if __name__ == "__main__":
    unittest.main()
