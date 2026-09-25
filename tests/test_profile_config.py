from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import cycle_status  # noqa: E402
import executors  # noqa: E402
import profile_config as pc  # noqa: E402
import router  # noqa: E402
from cycle import role_contract  # noqa: E402

try:
    import yaml  # noqa: F401
    HAVE_YAML = True
except ImportError:  # pragma: no cover - depends on the environment
    HAVE_YAML = False

CODEX_CATALOG = {
    "gpt-6-luna": frozenset({"low", "medium", "high", "xhigh", "max"}),
    "gpt-6-sol": frozenset({"low", "medium", "high", "xhigh", "max", "ultra"}),
}

COMMENTED_CONFIG = """\
# Top comment stays.
code_cycle:
  issue_provider: github
  repository:
    selector: owner/api   # inline comment stays
    default_branch: main
  review:
    # Who may advance findings.
    trusted_authors:
      - someone

other_tool:
  key: value
"""


def defaults() -> dict:
    return pc.full_spec(router.load_profiles())


def proposal(preset=None, overrides=(), **kw) -> pc.Proposal:
    return pc.build_proposal(defaults(), preset=preset, overrides=list(overrides),
                             codex_catalog=CODEX_CATALOG, **kw)


def run(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = pc.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class PresetTests(unittest.TestCase):
    def test_every_preset_is_valid_and_matches_its_declaration(self) -> None:
        for name, preset in pc.PRESETS.items():
            with self.subTest(preset=name):
                result = proposal(name)
                self.assertEqual([], result.errors)
                resolved = pc.full_spec(router.load_profiles(
                    {"code_cycle": {"profiles": result.declaration}}))
                for profile, spec in preset.profiles.items():
                    self.assertEqual(spec["primary"], resolved[profile]["primary"])
                    self.assertEqual(spec.get("fallback"), resolved[profile]["fallback"])

    def test_the_defaults_preset_declares_nothing(self) -> None:
        self.assertEqual({}, proposal("defaults").declaration)

    @unittest.skipUnless(HAVE_YAML, "PyYAML is needed to read a configuration")
    def test_run_cycle_accepts_every_preset(self) -> None:
        import run_cycle
        for name in pc.PRESETS:
            with self.subTest(preset=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / ".code-cycle.yml"
                path.write_text(COMMENTED_CONFIG, encoding="utf-8")
                pc.write_declaration(path, proposal(name).declaration)
                args = SimpleNamespace(repo=None, task="API-7", cwd=tmp,
                                       config=None, no_config=False)
                repo, profiles, _ = run_cycle.plan_with_strategy(args)
                self.assertEqual("owner/api", repo)
                self.assertEqual(
                    pc.PRESETS[name].profiles["reviewer"]["primary"],
                    str(profiles["reviewer"].primary))

    def test_every_role_of_every_preset_routes_to_an_executor_that_honours_it(self) -> None:
        registry = executors.Registry()
        ready = {name: router.Availability.READY for name in ("codex", "claude", "orca")}
        with tempfile.TemporaryDirectory() as tmp:
            work, scratch = Path(tmp) / "work", Path(tmp) / "scratch"
            work.mkdir()
            scratch.mkdir()
            disposable = executors.DisposableWorkspace(str(scratch), str(work))
            for name in pc.PRESETS:
                profiles = router.load_profiles(
                    {"code_cycle": {"profiles": proposal(name).declaration}})
                for role in router.ROLE_CANDIDATES:
                    policy = role_contract(role).workspace_policy
                    kwargs = ({"cwd": str(scratch), "workspace": disposable}
                              if policy is executors.WorkspacePolicy.DISPOSABLE
                              else {"cwd": str(work)})
                    eligible = registry.compatible_executors(policy, **kwargs)
                    for difficulty in (1, 3):
                        for sensitive in (False, True):
                            signals = router.TaskSignals(
                                difficulty=difficulty, security_sensitive=sensitive)
                            with self.subTest(preset=name, role=role,
                                              difficulty=difficulty, sensitive=sensitive):
                                decision = router.route(
                                    role, signals, ready, profiles=profiles,
                                    eligible_executors=eligible)
                                self.assertFalse(decision.blocked, decision.explain())

    def test_balanced_presets_never_let_the_implementing_vendor_review(self) -> None:
        for name in ("balanced-openai-implements", "balanced-anthropic-implements"):
            with self.subTest(preset=name):
                spec = proposal(name).spec
                implementing = {router.parse_target(spec[p][k]).provider
                                for p in pc.IMPLEMENT_PROFILES
                                for k in ("primary", "fallback") if spec[p].get(k)}
                reviewing = {router.parse_target(spec[p][k]).provider
                             for p in pc.REVIEW_PROFILES
                             for k in ("primary", "fallback") if spec[p].get(k)}
                self.assertEqual(1, len(implementing))
                self.assertFalse(implementing & reviewing)

    def test_a_crossing_fallback_is_refused_unless_allowed(self) -> None:
        crossing = ["cheap_coder.fallback=claude:anthropic/claude-sonnet-5 high"]
        refused = proposal("balanced-openai-implements", crossing)
        self.assertTrue(any("cheap_coder.fallback uses anthropic" in e
                            for e in refused.errors))
        self.assertEqual({}, refused.declaration)

        allowed = proposal("balanced-openai-implements", crossing, allow_cross_split=True)
        self.assertEqual([], allowed.errors)
        self.assertTrue(any("review its own work" in w for w in allowed.warnings))

    def test_a_crossing_reviewer_is_refused(self) -> None:
        refused = proposal("balanced-anthropic-implements",
                           ["security.primary=claude:anthropic/claude-opus-5-5 high"])
        self.assertTrue(any(e.startswith("security.primary uses anthropic")
                            for e in refused.errors))


class ValidationTests(unittest.TestCase):
    def test_codex_model_and_effort_are_checked_against_the_catalog(self) -> None:
        unknown = proposal(overrides=["reviewer.primary=codex:openai/gpt-9 high"])
        self.assertTrue(any("does not list model 'gpt-9'" in e for e in unknown.errors))
        effort = proposal(overrides=["cheap_coder.primary=codex:openai/gpt-6-luna ultra"])
        self.assertTrue(any("not 'ultra'" in e for e in effort.errors))

    def test_without_a_catalog_the_check_is_reported_not_guessed(self) -> None:
        result = pc.build_proposal(defaults(), codex_catalog=None)
        self.assertEqual([], result.errors)
        self.assertTrue(any("no Codex models cache" in n for n in result.notes))
        self.assertTrue(any("claude publishes no local model list" in n
                            for n in result.notes))

    def test_a_provider_the_executor_cannot_run_is_an_error(self) -> None:
        result = proposal(overrides=["reviewer.primary=claude:openai/gpt-6-sol high"])
        self.assertTrue(any("claude runs anthropic models" in e for e in result.errors))

    def test_malformed_overrides_and_unknown_profiles_are_refused(self) -> None:
        for override in ("reviewr.primary=codex:openai/gpt-6-sol high",
                         "reviewer=codex:openai/gpt-6-sol high",
                         "reviewer.primary=none"):
            with self.subTest(override=override):
                with self.assertRaises(pc.ProfileConfigError):
                    proposal(overrides=[override])

    def test_a_fallback_can_be_removed(self) -> None:
        result = proposal(overrides=["security.fallback=none"])
        self.assertEqual({"security": {"fallback": None}}, result.declaration)

    def test_moving_verify_off_codex_is_reported(self) -> None:
        result = proposal(
            overrides=["auxiliary_tool.primary=claude:anthropic/claude-sonnet-5 low"])
        self.assertTrue(any(w.startswith("verify uses auxiliary_tool")
                            for w in result.warnings))

    def test_targets_on_an_unavailable_executor_are_flagged(self) -> None:
        probes = {
            "codex": executors.ProbeResult("codex", router.Availability.AUTHENTICATED, "ok"),
            "claude": executors.ProbeResult("claude", router.Availability.INSTALLED, "no login"),
        }
        spec = proposal("balanced-openai-implements").spec
        warnings = pc.executor_warnings(spec, probes)
        self.assertIn("reviewer.primary: claude is installed on this machine, so "
                      "that target cannot run here yet", warnings)
        self.assertFalse(any(w.startswith("cheap_coder") for w in warnings))
        self.assertEqual([], pc.executor_warnings(spec, None))

    def test_read_only_modes_match_the_adapters(self) -> None:
        registry = executors.Registry()
        self.assertTrue(registry.get("codex").enforces_read_only)
        self.assertFalse(registry.get("claude").enforces_read_only)
        self.assertEqual("enforced", pc.READ_ONLY_MODE["codex"])
        self.assertEqual("detected", pc.READ_ONLY_MODE["claude"])
        self.assertEqual(
            {name for name in pc.KNOWN_EXECUTORS
             if registry.get(name).enforces_workspace_boundary},
            set(pc.DISPOSABLE_EXECUTORS))

    def test_codex_catalog_is_read_from_the_models_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models_cache.json"
            path.write_text(json.dumps({"models": [
                {"slug": "gpt-6-luna", "supported_reasoning_levels": [
                    {"effort": "low"}, {"effort": "max"}]},
                {"slug": "plain", "supported_reasoning_levels": ["high"]},
            ]}), encoding="utf-8")
            self.assertEqual(
                {"gpt-6-luna": frozenset({"low", "max"}), "plain": frozenset({"high"})},
                pc.codex_models(path))
            self.assertIsNone(pc.codex_models(Path(tmp) / "absent.json"))


class WriteTests(unittest.TestCase):
    DECLARATION = {"reviewer": {"primary": "claude:anthropic/claude-sonnet-5 high"},
                   "security": {"fallback": None}}

    def test_insert_keeps_every_other_line(self) -> None:
        updated = pc.apply_declaration(COMMENTED_CONFIG, self.DECLARATION)
        for line in COMMENTED_CONFIG.splitlines():
            self.assertIn(line, updated.splitlines())
        self.assertIn('      primary: "claude:anthropic/claude-sonnet-5 high"', updated)
        self.assertIn("      fallback: null", updated)
        # The block stays inside code_cycle, before the next top-level key.
        self.assertLess(updated.index("  profiles:"), updated.index("other_tool:"))

    def test_replacing_and_removing_the_block_restores_the_original(self) -> None:
        once = pc.apply_declaration(COMMENTED_CONFIG, self.DECLARATION)
        twice = pc.apply_declaration(once, {"deep_coder": {"fallback": None}})
        self.assertNotIn("reviewer:", twice)
        self.assertIn("    deep_coder:", twice)
        self.assertEqual(COMMENTED_CONFIG, pc.apply_declaration(twice, {}))

    def test_a_block_before_another_key_is_replaced_in_place(self) -> None:
        text = ("code_cycle:\n  profiles:\n    reviewer:\n"
                "      primary: \"codex:openai/gpt-6-sol max\"\n"
                "  # about review\n  review:\n    trusted_authors: [a]\n")
        updated = pc.apply_declaration(text, self.DECLARATION)
        self.assertNotIn("gpt-6-sol max", updated)
        self.assertIn("  # about review\n  review:\n", updated)

    def test_a_file_without_code_cycle_gains_one(self) -> None:
        updated = pc.apply_declaration("other: 1\n", {"security": {"fallback": None}})
        self.assertEqual("other: 1\n\ncode_cycle:\n  profiles:\n    security:\n"
                         "      fallback: null\n", updated)

    def test_a_flow_style_section_is_left_for_a_human(self) -> None:
        with self.assertRaises(pc.ProfileConfigError):
            pc.apply_declaration("code_cycle: {profiles: {}}\n", self.DECLARATION)
        with self.assertRaises(pc.ProfileConfigError):
            pc.apply_declaration("code_cycle:\n  profiles: {}\n", self.DECLARATION)

    @unittest.skipUnless(HAVE_YAML, "PyYAML is needed to verify a write")
    def test_write_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".code-cycle.yml"
            path.write_text(COMMENTED_CONFIG, encoding="utf-8")
            code, _, err = run("write", "--cwd", tmp, "--preset",
                               "balanced-openai-implements", "--no-probe")
            self.assertEqual(2, code)
            self.assertIn("--confirmed", err)
            self.assertEqual(COMMENTED_CONFIG, path.read_text(encoding="utf-8"))

            code, _, _ = run("write", "--cwd", tmp, "--preset",
                             "balanced-openai-implements", "--no-probe", "--confirmed")
            self.assertEqual(0, code)
            written = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(proposal("balanced-openai-implements").declaration,
                             written["code_cycle"]["profiles"])
            self.assertEqual({"key": "value"}, written["other_tool"])

    @unittest.skipUnless(HAVE_YAML, "PyYAML is needed to read a configuration")
    def test_an_invalid_proposal_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".code-cycle.yml"
            path.write_text(COMMENTED_CONFIG, encoding="utf-8")
            code, out, _ = run(
                "write", "--cwd", tmp, "--preset", "balanced-openai-implements",
                "--set", "reviewer.primary=codex:openai/gpt-6-sol high",
                "--no-probe", "--confirmed")
            self.assertEqual(1, code)
            self.assertIn("Errors:", out)
            self.assertEqual(COMMENTED_CONFIG, path.read_text(encoding="utf-8"))


class ShowTests(unittest.TestCase):
    @unittest.skipUnless(HAVE_YAML, "PyYAML is needed to read a configuration")
    def test_show_marks_configured_and_default_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".code-cycle.yml"
            path.write_text(pc.apply_declaration(
                COMMENTED_CONFIG, {"reviewer": {"primary": "codex:openai/gpt-6-sol max"}}),
                encoding="utf-8")
            code, out, _ = run("show", "--cwd", tmp, "--no-probe", "--format", "json")
            self.assertEqual(0, code)
            rows = {row["profile"]: row for row in json.loads(out)}
            self.assertEqual("config", rows["reviewer"]["primary_source"])
            self.assertEqual("default", rows["deep_coder"]["primary_source"])
            self.assertEqual(["review", "rereview"], rows["reviewer"]["roles"])
            self.assertEqual({"codex": "enforced"}, rows["reviewer"]["read_only_mode"])
            self.assertEqual({}, rows["cheap_coder"]["read_only_mode"])

    def test_cycle_status_shows_the_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), patch.object(pc, "_probe", lambda _: None):
                code = cycle_status.main(["--profiles", "--cwd", tmp])
            self.assertEqual(0, code)
            self.assertIn("## Effective routing profiles (all toolkit defaults)",
                          out.getvalue())
            self.assertIn("| `reviewer` | review, rereview |", out.getvalue())


if __name__ == "__main__":
    unittest.main()
