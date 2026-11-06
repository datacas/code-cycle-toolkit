"""#398: declared Docker Compose checks under the #409 exact-operation consent.

`docker compose config` is emulated: the fake reads the JSON Compose files the
command names in its directory, so the real identity code (global options,
environment prefixes, warnings, digest) runs without a Docker daemon.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import command_permissions as cp
import executors as ex
import grants
import stage_permissions as sp

CHECK = "docker compose -f docker-compose-local.yml run --rm backend-test pytest -q"
CLEAN = {"services": {"backend-test": {"image": "python:3.12",
                                       "volumes": [{"type": "bind", "source": "SRC", "target": "/app"}]}}}


def fake_compose(arguments, cwd=None, env=None, **_kwargs):
    """`docker compose [globals] config --format json`, from JSON files in cwd."""
    if arguments[-2:] != ["--format", "json"] or "config" not in arguments:
        return subprocess.CompletedProcess(arguments, 1, "", "unsupported")
    files, profiles, index = [], [], 2
    while index < len(arguments) and arguments[index] != "config":
        if arguments[index] in {"-f", "--file"}:
            files.append(arguments[index + 1])
            index += 2
        elif arguments[index].startswith("--file="):
            files.append(arguments[index].partition("=")[2])
            index += 1
        elif arguments[index] == "--profile":
            profiles.append(arguments[index + 1])
            index += 2
        else:
            index += 1
    config: dict = {}
    for name in files or ["compose.json"]:
        path = Path(cwd or ".") / name
        if not path.exists():
            return subprocess.CompletedProcess(arguments, 14, "", f"open {path}: no such file")
        text = path.read_text(encoding="utf-8").replace("SRC", str(Path(cwd).resolve()))
        config.update(json.loads(text))
    config["x-resolved"] = {"profiles": profiles,
                            "env": {key: (env or {}).get(key) for key in ("APP_ENV", "COMPOSE_PROFILES")}}
    return subprocess.CompletedProcess(arguments, 0, json.dumps(config), "")


class ComposeConsentCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="code-cycle-398-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = grants.ConsentStore(self.root / "consents.json")
        self.clone = self.make_clone("clone-a")
        self.tmpdir = self.make_tmpdir()
        patcher = patch.object(sp.subprocess, "run", side_effect=fake_compose)
        self.compose = patcher.start()
        self.addCleanup(patcher.stop)

    def make_clone(self, name: str, config: dict | None = None) -> Path:
        clone = self.root / name
        (clone / ".git").mkdir(parents=True)
        self.write(clone, config or CLEAN)
        return clone

    def make_tmpdir(self) -> str:
        scratch = tempfile.TemporaryDirectory(prefix="code-cycle-tmp-", dir=self.root)
        self.addCleanup(scratch.cleanup)
        return scratch.name

    @staticmethod
    def write(clone: Path, config: dict) -> None:
        (clone / "docker-compose-local.yml").write_text(json.dumps(config), encoding="utf-8")

    def policy(self, role="review", **changes) -> sp.StagePolicy:
        values = {"repo": "owner/repo", "tmpdir": self.tmpdir, "docker": True,
                  "test_command": "python3 -m unittest", "verify_prefixes": (CHECK,), **changes}
        return sp.StagePolicy(role, **values)

    def broker(self, **changes) -> grants.ConsentBroker:
        return grants.ConsentBroker(changes.pop("repo", "owner/repo"), platform="linux",
                                    store=self.store, **changes)

    def request(self, command=CHECK, policy=None, broker=None, cwd=None):
        broker = broker or self.broker()
        request = cp.refused_operation([command], policy or self.policy(), broker, "claude",
                                       str(cwd or self.clone))
        return request, broker

    def allowed(self, command=CHECK, policy=None, broker=None, cwd=None, role="review") -> bool:
        broker = broker or self.broker()
        policy = replace(policy or self.policy(role), command_permissions=broker.command_entries("claude", role))
        return sp.check(command, policy, cwd=str(cwd or self.clone)).allowed

    def answer(self, request, broker, answer="persistent"):
        grant = broker.grant_for(request)
        broker.apply_answer(grant, grant.scope_for("claude", "linux"), answer)
        return grant


class FirstConsentTests(ComposeConsentCase):
    def test_ac1_declared_compose_check_asks_instead_of_an_unconditional_deny(self):
        decision = sp.check(CHECK, self.policy(), cwd=str(self.clone))
        self.assertFalse(decision.allowed)
        self.assertFalse(decision.needs_docker)
        self.assertIn("exact operation consent", decision.reason)
        request, broker = self.request()
        self.assertEqual("verify:" + CHECK, request["check"])
        grant = broker.grant_for(request)
        self.assertEqual("pending", broker.request(grant, "claude", role="review", blocker=request))
        question = broker.pending[0].question
        self.assertIn(CHECK[:60], question["prompt"])
        self.assertIn("stays unverified and the cycle continues", question["prompt"])
        self.assertEqual(["this cycle", "this repository", "deny"], question["options"])
        record = broker.pending[0].record()
        self.assertEqual("verify:" + CHECK, record["check"])
        self.assertEqual(64, len(record["pattern"]["compose"]["config_sha256"]))

    def test_ac2_allow_runs_only_the_matching_normalized_command(self):
        request, broker = self.request()
        self.answer(request, broker)
        self.assertTrue(self.allowed(broker=broker))
        for other in (CHECK + " --extra", CHECK.replace("backend-test", "frontend"),
                      CHECK.replace("docker-compose-local.yml", "other.yml"),
                      "docker compose -f docker-compose-local.yml run --rm -v /:/host backend-test pytest -q",
                      "docker run --rm -v /:/host alpine"):
            with self.subTest(other=other):
                (self.clone / "other.yml").write_text(json.dumps(CLEAN), encoding="utf-8")
                self.assertFalse(self.allowed(other, broker=broker))
                # AC-6: each one is its own consent question, never a veto.
                other_request, _ = self.request(other, broker=broker)
                self.assertIsNotNone(other_request)
                self.assertNotEqual(request["resource"], other_request["resource"])

    def test_ac6_only_unparseable_shell_syntax_stays_refused(self):
        for command in (CHECK + "; rm -rf /", CHECK + " $(id)", CHECK + " | tee x"):
            with self.subTest(command=command):
                self.assertFalse(sp.check(command, self.policy(), cwd=str(self.clone)).allowed)
                self.assertIsNone(self.request(command)[0])

    def test_ac8_hook_round_trip_first_authorization_reuse_and_revocation(self):
        path = self.root / "policy.json"

        def hook(broker):
            policy = replace(self.policy(), command_permissions=broker.command_entries("claude", "review"))
            path.write_text(policy.to_json(), encoding="utf-8")
            output = io.StringIO()
            sp.hook_main([str(path)], io.StringIO(json.dumps(
                {"tool_name": "Bash", "tool_input": {"command": CHECK}, "cwd": str(self.clone)})), output)
            return output.getvalue()

        denied = hook(self.broker())
        reason = json.loads(denied)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("COMMAND_PERMISSION_OPERATION=", reason)
        stream = "\n".join(json.dumps(event) for event in [
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "x",
                "name": "Bash", "input": {"command": CHECK}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "x",
                "is_error": True, "content": reason}]}}])
        artifacts = ex.ClaudeAdapter().extra_artifacts(stream)
        broker = self.broker()
        request = cp.refused_operation(artifacts["command_refusals"], self.policy(), broker, "claude",
                                       str(self.clone))
        grant = self.answer(request, broker)
        self.assertEqual("", hook(self.broker()))            # first authorization, reused
        self.assertEqual("", hook(self.broker()))            # identical invocation: no new prompt
        self.assertTrue(self.store.revoke("owner/repo", grant, grant.scope_for("claude", "linux")))
        self.assertIn("deny", hook(self.broker()))           # revoked: asks again


class IdentityTests(ComposeConsentCase):
    def test_ac3_choice_persists_across_cycles_and_is_repository_scoped(self):
        request, broker = self.request(broker=self.broker(cycle_id="one", task_id="398"))
        self.answer(request, broker)
        self.assertTrue(self.allowed(broker=self.broker(cycle_id="two", task_id="399")))
        self.assertFalse(self.allowed(broker=self.broker(repo="owner/other")))
        listed = self.store.list("owner/repo")
        self.assertEqual(["GRANTED"], [entry["status"] for entry in listed])

    def test_ac15_review_and_rereview_share_one_repository_relative_identity(self):
        request, broker = self.request()
        self.answer(request, broker)
        other_clone = self.make_clone("clone-b")
        rereview = self.policy("rereview", tmpdir=self.make_tmpdir())
        self.assertTrue(self.allowed(policy=rereview, cwd=other_clone, role="rereview"))
        op = sp.operation("Bash", {"command": CHECK}, rereview, str(other_clone))
        self.assertEqual("review", op["role"])
        self.assertEqual(".", op["pattern"]["cwd"])

    def test_ac15_clone_path_and_tmpdir_values_normalize_but_prefixes_stay(self):
        def op(clone, tmpdir, command):
            return sp.operation("Bash", {"command": command}, self.policy(tmpdir=tmpdir), str(clone))
        clone_b, tmp_b = self.make_clone("clone-b"), self.make_tmpdir()
        for template in ("docker compose -f {clone}/docker-compose-local.yml run --rm backend-test",
                         "docker compose --file={clone}/docker-compose-local.yml run --rm backend-test",
                         "DOCKER_CONFIG={tmp}/docker docker compose -f docker-compose-local.yml run backend-test"):
            with self.subTest(template=template):
                first = op(self.clone, self.tmpdir, template.format(clone=self.clone, tmp=self.tmpdir))
                second = op(clone_b, tmp_b, template.format(clone=clone_b, tmp=tmp_b))
                self.assertEqual(grants.identifier(first), grants.identifier(second))
        base = op(self.clone, self.tmpdir, CHECK)
        for prefix in ("DOCKER_HOST=tcp://evil:2375", "COMPOSE_FILE=other.yml", "LD_PRELOAD=/x.so"):
            with self.subTest(prefix=prefix):
                self.assertNotEqual(base, op(self.clone, self.tmpdir, f"{prefix} {CHECK}"))
        # A pnpm check with a per-dispatch DOCKER_CONFIG (#439) matches across dispatches.
        pnpm = "DOCKER_CONFIG={tmp}/docker pnpm run test:unit"
        self.assertEqual(op(self.clone, self.tmpdir, pnpm.format(tmp=self.tmpdir)),
                         op(clone_b, tmp_b, pnpm.format(tmp=tmp_b)))

    def test_ac9_ac11_a_changed_effective_configuration_asks_again(self):
        request, broker = self.request()
        self.answer(request, broker)
        digest = request["config_sha256"]
        changes = {
            "mount": {"services": {"backend-test": {"image": "python:3.12", "volumes": [
                {"type": "bind", "source": "/etc", "target": "/host-etc"}]}}},
            "privileged": {"services": {"backend-test": {"image": "python:3.12", "privileged": True}}},
            "namespace": {"services": {"backend-test": {"image": "python:3.12", "pid": "host"}}},
            "socket": {"services": {"backend-test": {"image": "python:3.12", "volumes": [
                {"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"}]}}},
        }
        expected = {"mount": "from outside the checkout", "privileged": "runs privileged",
                    "namespace": "host pid namespace", "socket": "mounts the Docker socket"}
        for name, config in changes.items():
            with self.subTest(change=name):
                self.write(self.clone, config)
                # Modified between validation (consent) and execution: refused, asked again.
                self.assertFalse(self.allowed(broker=broker))
                changed, _ = self.request(broker=broker)
                self.assertNotEqual(digest, changed["config_sha256"])
                self.assertTrue(any(expected[name] in warning for warning in changed["warnings"]))
                grant = broker.grant_for(changed)
                broker.request(grant, "claude", role="review", blocker=changed)
                self.assertIn("WARNING:", broker.pending[-1].question["prompt"])
        self.write(self.clone, CLEAN)
        self.assertTrue(self.allowed(broker=broker))

    def test_ac11_variables_and_profiles_are_part_of_the_identity(self):
        request, broker = self.request()
        self.answer(request, broker)
        with patch.dict(os.environ, {"APP_ENV": "production"}):
            self.assertFalse(self.allowed(broker=broker))
        profiled = CHECK.replace("compose -f", "compose --profile debug -f")
        self.assertFalse(self.allowed(profiled, broker=broker))
        self.assertTrue(self.allowed(broker=broker))

    def test_ac12_after_consent_nothing_refuses_disclosed_host_access(self):
        self.write(self.clone, {"services": {"backend-test": {"image": "x", "privileged": True,
                                                              "network_mode": "host"}}})
        request, broker = self.request()
        self.assertEqual(["service backend-test runs privileged",
                          "service backend-test uses the host network namespace"], request["warnings"])
        self.answer(request, broker)
        self.assertTrue(self.allowed(broker=broker))

    def test_unresolvable_configuration_is_a_real_limit_not_a_consent(self):
        (self.clone / "docker-compose-local.yml").unlink()
        self.assertFalse(sp.check(CHECK, self.policy(), cwd=str(self.clone)).allowed)
        self.assertIsNone(self.request()[0])


class SocketAndContinuationTests(ComposeConsentCase):
    def test_ac5_socket_permission_is_separate_and_asked_first(self):
        ungranted = self.policy(docker=False)
        decision = sp.check(CHECK, ungranted, cwd=str(self.clone))
        self.assertTrue(decision.needs_docker)
        self.assertIsNone(self.request(policy=ungranted)[0])
        # Count only: the call record would print the whole environment.
        self.assertEqual(0, self.compose.call_count)
        request, broker = self.request()
        self.answer(request, broker)
        # Command consent never stands in for the socket.
        self.assertFalse(self.allowed(policy=ungranted, broker=broker))
        self.assertTrue(sp.check(CHECK, replace(ungranted, command_permissions=broker.command_entries(
            "claude", "review")), cwd=str(self.clone)).needs_docker)

    def test_ac4_ac10_deny_is_unverified_not_human_intervention(self):
        request, broker = self.request()
        self.answer(request, broker, "deny")
        policy = replace(self.policy(), command_permissions=broker.command_entries("claude", "review"))
        self.assertIn("denied", sp.check(CHECK, policy, cwd=str(self.clone)).reason)
        spoken = ("ORCHESTRATION_RESULT\n" + json.dumps({
            "status": "REVIEW_PASSED_AWAITING_CI", "tests": {"version": 2, "checks": [
                {"id": "verify-compose", "command": CHECK, "result": "pass"}]}})
                  + "\nEND_ORCHESTRATION_RESULT")
        result, essential = cp.reconcile_result(spoken, request, "deny", "review")
        payload = json.loads(result.split("ORCHESTRATION_RESULT\n", 1)[1].split("\nEND_", 1)[0])
        self.assertFalse(essential)
        self.assertEqual("REVIEW_PASSED_AWAITING_CI", payload["status"])
        self.assertNotIn("stop_reason", payload)
        self.assertEqual("unverified", payload["tests"]["checks"][0]["result"])
        outcome = payload["permission_outcomes"][0]
        self.assertEqual(("verify:" + CHECK, request["config_sha256"]),
                         (outcome["check"], outcome["config_sha256"]))
        # Even a stage that called itself BLOCKED over a declined check is not essential.
        blocked = spoken.replace("REVIEW_PASSED_AWAITING_CI", "BLOCKED")
        self.assertFalse(cp.reconcile_result(blocked, request, "deny", "review")[1])

    def test_ac3_remembered_deny_and_its_denial_context(self):
        request, broker = self.request()
        grant = self.answer(request, broker, "deny")
        later = self.broker(cycle_id="later", task_id="400")
        later.granted("claude")
        self.assertIn(grant.id, later.denied_ids)
        context = grants.denial_context(later.registry[grant.id])
        self.assertIn("not essential", context)
        self.assertIn("record it unverified", context)

    def test_ac7_resume_reloads_cycle_answers_only_for_the_same_work_item(self):
        request, broker = self.request(broker=self.broker(cycle_id="one", task_id="398"))
        self.answer(request, broker, "cycle")
        self.assertTrue(self.allowed(broker=self.broker(cycle_id="one", task_id="398")))
        self.assertFalse(self.allowed(broker=self.broker(cycle_id="two", task_id="401")))

    def test_ac7_partial_consent_covers_only_the_answered_check(self):
        second = "docker compose -f docker-compose-local.yml run --rm backend-test pytest -q tests/api"
        policy = self.policy(verify_prefixes=(CHECK, second))
        first, broker = self.request(policy=policy)
        self.answer(first, broker)
        self.assertTrue(self.allowed(policy=policy, broker=broker))
        self.assertFalse(self.allowed(second, policy=policy, broker=broker))

    def test_legacy_rereview_decision_still_matches(self):
        op = sp.operation("Bash", {"command": CHECK}, self.policy(), str(self.clone))
        legacy = {**op, "role": "rereview"}
        policy = replace(self.policy("rereview"), command_permissions=(
            {"operation": legacy, "decision": "allow"},))
        self.assertTrue(sp.check(CHECK, policy, cwd=str(self.clone)).allowed)


class IndirectAndGuardTests(ComposeConsentCase):
    def test_ac13_indirect_docker_is_stated_in_the_consent_text(self):
        policy = self.policy("implement", verify_prefixes=("pnpm run test:unit",))
        command = "DOCKER_CONFIG=$TMPDIR/docker pnpm run test:unit"
        self.assertFalse(sp.check(command, policy, cwd=str(self.clone)).allowed)
        request, broker = self.request(command, policy=policy)
        grant = broker.grant_for(request)
        scope = grant.scope_for("claude", "linux")
        self.assertIn("reach Docker without a per-operation check", scope.risks)
        self.assertEqual("verify:pnpm run test:unit", request["check"])
        broker.apply_answer(grant, scope, "persistent")
        allowed = replace(policy, command_permissions=broker.command_entries("claude", "implement"))
        self.assertTrue(sp.check(command, allowed, cwd=str(self.clone)).allowed)

    def test_the_host_side_resolver_never_runs_what_the_command_chooses(self):
        # SEC-1: the hook resolves outside the sandbox and before consent.
        command = (f"PATH={self.clone}/bin LD_PRELOAD={self.clone}/x.so DOCKER_CONFIG={self.clone}/d "
                   f"APP_ENV=staging {self.clone}/bin/docker compose -f docker-compose-local.yml run backend-test")
        sp.operation("Bash", {"command": command}, self.policy(), str(self.clone))
        arguments = self.compose.call_args.args[0]
        env = self.compose.call_args.kwargs["env"]
        self.assertEqual("docker", arguments[0])
        self.assertEqual(os.environ.get("PATH"), env.get("PATH"))
        self.assertEqual((os.environ.get("LD_PRELOAD"), os.environ.get("DOCKER_CONFIG")),
                         (env.get("LD_PRELOAD"), env.get("DOCKER_CONFIG")))
        self.assertEqual(("staging", "false"), (env["APP_ENV"], env["COMPOSE_EXPERIMENTAL_GIT_REMOTE"]))

    def test_interpolated_secrets_never_reach_the_warnings(self):
        # SEC-2: Compose interpolates the host environment into mount sources.
        self.write(self.clone, {"services": {"backend-test": {"image": "x", "volumes": [
            {"type": "bind", "source": "/srv/s3cr3t-token-value", "target": "/t"}]}}})
        with patch.dict(os.environ, {"SOME_TOKEN": "s3cr3t-token-value"}):
            request, _ = self.request()
        self.assertEqual(["service backend-test mounts /srv/<env:SOME_TOKEN> from outside the checkout"],
                         request["warnings"])

    def test_environment_prefixes_never_hide_git_guards(self):
        policy = self.policy("implement", base_branch="main")
        for command in ("GIT_DIR=/tmp/x git status", "FOO=1 git push --force origin topic",
                        "FOO=1 gh pr merge 1", "GH_HOST=evil.example gh pr view 1",
                        "HOME=/tmp/fake git status", "GITHUB_TOKEN=x gh pr view 1"):
            with self.subTest(command=command):
                self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, policy, str(self.clone)))


class CodexTests(ComposeConsentCase):
    def test_ac14_codex_asks_before_dispatch_and_reuses_the_answer(self):
        asked = []
        broker = self.broker(prompt=lambda question: asked.append(question) or "persistent")
        context, records = cp.codex_compose_consent(self.policy(), broker, "codex", "review", str(self.clone))
        self.assertEqual(1, len(asked))
        self.assertIn(f"exactly as `{CHECK}`", context)
        self.assertIn("nothing limits this dispatch", context.replace("On Codex n", "n"))
        self.assertEqual(("persistent", "verify:" + CHECK), (records[0]["answer"], records[0]["check"]))
        again = self.broker(prompt=lambda question: asked.append(question) or "deny")
        _, reused = cp.codex_compose_consent(self.policy("rereview"), again, "codex", "rereview",
                                             str(self.make_clone("clone-b")))
        self.assertEqual(1, len(asked))
        self.assertEqual("persistent", reused[0]["answer"])
        unchanged = cp.compose_drift(records, self.policy(), str(self.clone))
        self.assertNotIn("config_sha256_after", unchanged[0])
        self.write(self.clone, {"services": {"backend-test": {"image": "x", "privileged": True}}})
        drift = cp.compose_drift(records, self.policy(), str(self.clone))[0]
        self.assertNotEqual(drift["config_sha256_before"], drift["config_sha256_after"])
        self.assertIn("changed during the Codex run", drift["warning"])
        self.assertFalse(drift["essential"])

    def test_ac14_codex_deny_and_pending_leave_the_check_unverified(self):
        denied = self.broker(prompt=lambda _question: "deny")
        context, records = cp.codex_compose_consent(self.policy(), denied, "codex", "review", str(self.clone))
        self.assertEqual("deny", records[0]["answer"])
        self.assertIn("not essential", context)
        pending = self.broker(repo="owner/unanswered")
        context, records = cp.codex_compose_consent(self.policy(), pending, "codex", "review", str(self.clone))
        self.assertEqual("pending", records[0]["answer"])
        self.assertIn("awaits the user's consent", context)
        self.assertEqual(1, len(pending.pending))

    def test_outcomes_are_added_to_the_result(self):
        spoken = 'ORCHESTRATION_RESULT\n{"status": "REVIEW_PASSED_AWAITING_CI"}\nEND_ORCHESTRATION_RESULT'
        added = cp.add_outcomes(spoken, [{"grant": "g", "command": CHECK}])
        self.assertIn('"permission_outcomes"', added)
        self.assertEqual(spoken, cp.add_outcomes(spoken, []))


from test_run_cycle import RunCycleTestCase, Talker, block  # noqa: E402
import router  # noqa: E402
import run_cycle as rc  # noqa: E402


class ComposeReviewer(Talker):
    """A Claude reviewer whose configured Compose check the hook refuses until consented."""

    def __init__(self):
        super().__init__("claude")
        self.reviews = 0

    def dispatch(self, target, task, **kwargs):
        self.dispatched.append(task)
        policy = kwargs.get("command_policy")
        if policy is None or policy.role == "implement":
            return ex.DispatchResult(ex.DispatchOutcome.SUCCEEDED, "claude", target,
                start_state="started", process_exited=True, agent_output=block("IMPLEMENTED"))
        self.reviews += 1
        granted = replace(policy, docker="docker_host" in (kwargs.get("sandbox_grants") or ()))
        allowed = sp.check(CHECK, granted, cwd=kwargs.get("cwd")).allowed
        return ex.DispatchResult(ex.DispatchOutcome.SUCCEEDED, "claude", target,
            start_state="started", process_exited=True, agent_output=block("APPROVED"),
            artifacts={} if allowed else {"permission_denials": [CHECK]})


class ReviewCycleTests(RunCycleTestCase):
    def setUp(self):
        super().setUp()
        patcher = patch.object(sp, "COMPOSE_RESOLVER", lambda *_a, **_k: {
            "config_sha256": "a" * 64, "warnings": ["service backend-test runs privileged"]})
        patcher.start()
        self.addCleanup(patcher.stop)
        # An unchanged workspace, so an answered permission may rerun the stage.
        clean = {"dirty": False, "workspace": {"head": "a" * 40, "fingerprint": "b" * 64},
                 "remote": "same", "pulls": [], "comments": []}
        snapshot = patch("cycle.effect_snapshot", return_value=clean)
        snapshot.start()
        self.addCleanup(snapshot.stop)

    def run_review(self, prompt=None):
        claude = ComposeReviewer()
        broker = grants.ConsentBroker("owner/api", platform="linux", prompt=prompt,
                                      store=grants.ConsentStore(self.home / "consents.json"),
                                      preauthorized=frozenset({"docker_host"}))
        profiles = router.load_profiles({"code_cycle": {"profiles": {
            "deep_coder": {"primary": "claude:anthropic/claude-opus-5-5 high"},
            "reviewer": {"primary": "claude:anthropic/claude-sonnet-5 high"}}}})
        report = rc.run_cycle("owner/api", "398", router.TaskSignals(), self.store,
            registry=ex.Registry([claude]), availability={"claude": ex.Availability.READY},
            profiles=profiles, issue_review="off", consent=broker, allowed_verify_commands=(CHECK,))
        return report, claude, broker

    def test_ac1_unanswered_review_check_is_a_permission_question(self):
        report, claude, _ = self.run_review()
        self.assertEqual("permission_required", report.stop_reason, report.stopped_because)
        question = report.permission_requests[0].question
        self.assertNotIn("forever", question["options"])
        self.assertIn("WARNING: service backend-test runs privileged", question["prompt"])
        self.assertEqual(1, claude.reviews)

    def test_ac4_a_denied_review_check_never_stops_with_human_intervention(self):
        report, claude, _ = self.run_review(prompt=lambda _question: "deny")
        self.assertNotEqual("HUMAN_INTERVENTION", report.stop_reason)
        self.assertNotEqual("permission_required", report.stop_reason)
        self.assertIn(report.status, {rc.APPROVED_END, "VERIFICATION_INCOMPLETE"}, report.stopped_because)
        self.assertEqual(2, claude.reviews)  # refused, denied, rerun without it
        # The persisted denial is not asked again in a later cycle.
        later, _, _ = self.run_review(prompt=lambda _question: self.fail("asked again after a denial"))
        self.assertNotEqual("HUMAN_INTERVENTION", later.stop_reason)

    def test_ac3_an_allowed_review_check_is_reused_by_the_next_cycle(self):
        report, claude, _ = self.run_review(prompt=lambda _question: "persistent")
        self.assertEqual(rc.APPROVED_END, report.status, report.stopped_because)
        self.assertEqual(2, claude.reviews)  # refused, answered, rerun allowed
        later, claude, _ = self.run_review(prompt=lambda _question: self.fail("asked twice"))
        self.assertEqual(rc.APPROVED_END, later.status, later.stopped_because)
        self.assertEqual(1, claude.reviews)


if __name__ == "__main__":
    unittest.main()
