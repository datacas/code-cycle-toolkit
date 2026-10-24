"""The permission decisions in force are readable from the SQLite store alone (#311)."""
import contextlib
import io
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cycle_journal as cj
import grants
import router
import run_cycle as rc
import stage_permissions as sp
from test_run_cycle import RunCycleTestCase, Talker, block

REPO = "owner/repo"
DOCKER = grants.GRANTS["docker_host"]
CODEX = DOCKER.scope_for("codex", "linux")
CLAUDE = DOCKER.scope_for("claude", "linux")


def git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=cwd, check=True, capture_output=True, text=True)


class PermissionViewCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "telemetry.sqlite"
        self.tick = datetime(2026, 10, 1, tzinfo=timezone.utc)

    def clock(self):
        self.tick += timedelta(minutes=1)
        return self.tick

    def journal(self, cycle="cycle-1", repo=REPO):
        return cj.Journal(self.path, repo, "311", cycle, clock=self.clock)

    def view(self, branch="main", repo=REPO, now=None):
        return cj.permissions_in_force(self.path, repo, branch, now=now or self.tick)

    def rows(self, repo=REPO, branch="main", **kw):
        return [(d["source"], d["scope"], d["decision"], d["level"])
                for d in self.view(branch, repo, **kw)["decisions"]]

    def stored(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT source, scope, decision, branch, level "
                              "FROM journal_permissions ORDER BY permission_id").fetchall()


class InForceTests(PermissionViewCase):
    def test_nothing_recorded_reads_as_empty(self):
        self.assertEqual({"repo": REPO, "branch": "main", "decisions": []}, self.view())
        self.assertIn("No permission decisions", cj.render_permissions(self.view()))
        # A store that never held a journal is read without creating one.
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TABLE unrelated (x)")
        self.assertEqual([], self.view()["decisions"])
        with closing(sqlite3.connect(self.path)) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual({"unrelated"}, tables)

    def test_newest_decision_per_source_and_scope_wins(self):
        first = self.journal("cycle-1")
        first.permission("user_choices", "tools: rg", "allow", branch="main")
        first.permission("user_choices", "tools: rg", cj.WITHDRAWN, branch="main")
        self.assertEqual([], self.rows())
        self.journal("cycle-2").permission("user_choices", "tools: rg", "allow", branch="main")
        self.assertEqual([("user_choices", "tools: rg", "allow", "branch")], self.rows())
        # The same decision again is not a change, so it is not written twice.
        self.journal("cycle-3").permission("user_choices", "tools: rg", "allow", branch="main")
        self.assertEqual(3, len(self.stored()))

    def test_every_decision_carries_its_source_scope_decision_and_date(self):
        self.journal().permission("consents.json", "docker_host: x", "allow")
        (item,) = self.view()["decisions"]
        self.assertEqual(("consents.json", "docker_host: x", "allow", "repository"),
                         (item["source"], item["scope"], item["decision"], item["level"]))
        self.assertEqual(self.tick.isoformat(), item["since"])
        self.assertIn(self.tick.isoformat(), cj.render_permissions(self.view()))

    def test_the_documented_query_agrees_with_the_command(self):
        text = (Path(__file__).resolve().parents[1] / "docs" / "telemetry.md").read_text(encoding="utf-8")
        query = text[text.index("SELECT p.source"):].split(";", 1)[0]
        journal = self.journal()
        journal.permission("consents.json", "docker_host: x", "allow")
        journal.permission("consents.json", "docker_host: old", "allow")
        journal.permission("consents.json", "docker_host: old", cj.WITHDRAWN)
        journal.permission("user_choices", "tools: rg", "allow", branch="main")
        journal.permission("user_choices", "tools: jq", "allow", branch="other")
        with closing(sqlite3.connect(self.path)) as db:
            found = {(r[0], r[1]) for r in db.execute(query, (REPO, "main"))}
        self.assertEqual({(r[0], r[1]) for r in self.rows()}, found)
        self.assertEqual({("consents.json", "docker_host: x"), ("user_choices", "tools: rg")}, found)

    def test_repositories_do_not_mix(self):
        self.journal(repo="owner/one").permission("consents.json", "docker_host: one", "allow")
        self.journal(repo="owner/two").permission("consents.json", "docker_host: two", "allow")
        self.assertEqual(["docker_host: one"], [r[1] for r in self.rows(repo="owner/one")])
        self.assertEqual(["docker_host: two"], [r[1] for r in self.rows(repo="owner/two")])
        self.assertEqual([], self.rows(repo="owner/three"))

    def test_a_branch_sees_its_own_user_choices_only(self):
        journal = self.journal()
        journal.permission("user_choices", "tools: rg", "allow", branch="main")
        journal.permission("user_choices", "tools: jq", "allow", branch="feature/x")
        journal.permission("consents.json", "docker_host: x", "allow")
        self.assertEqual(["docker_host: x", "tools: rg"], sorted(r[1] for r in self.rows()))
        self.assertEqual(["docker_host: x", "tools: jq"],
                         sorted(r[1] for r in self.rows(branch="feature/x")))
        # Without a branch only the shared repository decisions are known.
        self.assertEqual(["docker_host: x"], [r[1] for r in self.rows(branch=None)])
        marked = {d["scope"]: d["branch"] for d in self.view()["decisions"]}
        self.assertEqual({"docker_host: x": None, "tools: rg": "main"}, marked)

    def test_cycle_and_run_decisions_end_with_their_cycle(self):
        journal = self.journal("live")
        journal.permission("--grant", "docker_host: x", "cycle")
        journal.permission("--grant", "docker_host: y", "deny")
        self.assertEqual([("--grant", "docker_host: x", "cycle", "cycle"),
                          ("--grant", "docker_host: y", "deny", "run")], self.rows())
        journal.end_cycle()
        # Expiry is not a withdrawal: nothing is recorded for it, it just stops showing.
        self.assertEqual([], self.rows())
        self.assertNotIn(cj.WITHDRAWN, [row[2] for row in self.stored()])

    def test_a_cycle_that_never_ended_stops_showing_after_a_day(self):
        self.journal("crashed").permission("--grant", "docker_host: x", "cycle")
        self.assertEqual(1, len(self.rows()))
        later = self.tick + timedelta(hours=cj.CYCLE_LIVE_HOURS, seconds=1)
        self.assertEqual([], self.rows(now=later))

    def test_a_run_denial_never_revokes_a_persistent_allow(self):
        self.journal("one").permission("consents.json", "docker_host: x", "allow")
        denying = self.journal("two")
        denying.permission("--grant", "docker_host: x", "deny")
        self.assertEqual([("consents.json", "docker_host: x", "allow", "repository"),
                          ("--grant", "docker_host: x", "deny", "run")],
                         sorted(self.rows(), key=lambda row: row[3] != "repository"))
        denying.end_cycle()
        self.assertEqual([("consents.json", "docker_host: x", "allow", "repository")], self.rows())

    def test_values_are_redacted_when_stored_and_when_shown(self):
        secret = "ghp_abcdefghijklmnopqrstuvwx"
        self.journal().permission("user_choices", f"commands: deploy --token {secret}",
                                  "allow", branch="main")
        self.assertNotIn(secret, str(self.stored()))
        # A value that reached the table unredacted is still redacted on the way out.
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE journal_permissions SET scope = 'commands: deploy ' || ?", (secret,))
            db.commit()
        shown = json.dumps(self.view()) + cj.render_permissions(self.view())
        self.assertNotIn(secret, shown)
        self.assertIn(cj.REDACTED, shown)

    def test_a_store_from_before_the_view_is_migrated_and_read(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(
                "CREATE TABLE journal_permissions (permission_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "repo_id TEXT NOT NULL, cycle_id TEXT NOT NULL, source TEXT NOT NULL, "
                "scope TEXT NOT NULL, decision TEXT NOT NULL, occurred_at TEXT NOT NULL);"
                f"INSERT INTO journal_permissions (repo_id, cycle_id, source, scope, decision, occurred_at) "
                f"VALUES ('{REPO}', 'old', 'consents.json', 'docker_host: x', 'persistent', '2026-09-01T00:00:00+00:00'), "
                f"('{REPO}', 'old', 'user_choices', 'tools: rg', 'allow', '2026-09-01T00:00:00+00:00');")
        # Read-only access works on the old shape: user_choices has no known branch.
        self.assertEqual(["docker_host: x"], [r[1] for r in self.rows()])
        # The first write migrates it.
        self.journal().permission("user_choices", "tools: jq", "allow", branch="main")
        self.assertEqual(["docker_host: x", "tools: jq"], sorted(r[1] for r in self.rows()))


class ReconcileTests(PermissionViewCase):
    def test_a_user_choice_removed_from_the_configuration_is_withdrawn_once(self):
        journal = self.journal("one")
        journal.reconcile_permissions(user_choices={"tools": ("rg", "jq")}, branch="main")
        self.assertEqual(["tools: jq", "tools: rg"], sorted(r[1] for r in self.rows()))
        later = self.journal("two")
        later.reconcile_permissions(user_choices={"tools": ("rg",)}, branch="main")
        self.assertEqual(["tools: rg"], [r[1] for r in self.rows()])
        withdrawn = [row for row in self.stored() if row[2] == cj.WITHDRAWN]
        self.assertEqual([("user_choices", "tools: jq", cj.WITHDRAWN, "main", "branch")], withdrawn)
        # The same configuration again changes nothing, so nothing more is recorded.
        before = self.stored()
        self.journal("three").reconcile_permissions(user_choices={"tools": ("rg",)}, branch="main")
        self.assertEqual(before, self.stored())

    def test_a_removal_on_one_branch_does_not_touch_another(self):
        self.journal("one").reconcile_permissions(user_choices={"tools": ("rg",)}, branch="main")
        self.journal("two").reconcile_permissions(user_choices={"tools": ("rg",)}, branch="feature")
        self.journal("three").reconcile_permissions(user_choices={}, branch="feature")
        self.assertEqual([], self.rows(branch="feature"))
        self.assertEqual(["tools: rg"], [r[1] for r in self.rows(branch="main")])

    def test_user_choices_not_supplied_or_without_a_branch_infer_nothing(self):
        self.journal("one").reconcile_permissions(user_choices={"tools": ("rg",)}, branch="main")
        self.journal("two").reconcile_permissions(user_choices=None, branch="main")
        self.journal("three").reconcile_permissions(user_choices={}, branch=None)
        self.assertEqual(["tools: rg"], [r[1] for r in self.rows()])

    def test_a_consent_entry_that_is_missing_or_changed_is_withdrawn(self):
        store = grants.ConsentStore(self.root / "consents.json")
        store.save(REPO, DOCKER, CODEX, "allow")
        scope = f"docker_host: {CODEX.effective}"
        self.journal("one").reconcile_permissions(consents=store.allowed_scopes(REPO))
        self.assertEqual([scope], [r[1] for r in self.rows()])
        # A stored entry whose scope no longer matches stops applying (the
        # fingerprint is what the runtime compares), and the view follows.
        payload = json.loads((self.root / "consents.json").read_text())
        entry = next(iter(payload["repos"][REPO]["docker_host"].values()))
        entry["fingerprint"] = "changed"
        (self.root / "consents.json").write_text(json.dumps(payload))
        self.journal("two").reconcile_permissions(consents=store.allowed_scopes(REPO))
        self.assertEqual([], self.rows())
        # Re-allowing is a new decision; deleting the entry withdraws it again.
        store.save(REPO, DOCKER, CODEX, "allow")
        self.journal("three").reconcile_permissions(consents=store.allowed_scopes(REPO))
        self.assertEqual([scope], [r[1] for r in self.rows()])
        (self.root / "consents.json").write_text(json.dumps({"schema": 1, "repos": {}}))
        self.journal("four").reconcile_permissions(consents=store.allowed_scopes(REPO))
        self.assertEqual([], self.rows())
        self.assertEqual(["allow", cj.WITHDRAWN, "allow", cj.WITHDRAWN],
                         [row[2] for row in self.stored()])

    def test_an_unreadable_consent_store_withdraws_nothing(self):
        store = grants.ConsentStore(self.root / "consents.json")
        store.save(REPO, DOCKER, CODEX, "allow")
        self.journal("one").reconcile_permissions(consents=store.allowed_scopes(REPO))
        (self.root / "consents.json").write_text("{not json")
        with self.assertRaises(grants.GrantError):
            store.allowed_scopes(REPO)
        # The runtime passes None when the store could not be read or validated.
        self.journal("two").reconcile_permissions(consents=None)
        self.assertEqual(1, len(self.rows()))
        self.assertEqual(["allow"], [row[2] for row in self.stored()])

    def test_executor_scopes_are_listed_without_replacing_each_other(self):
        store = grants.ConsentStore(self.root / "consents.json")
        store.save(REPO, DOCKER, CODEX, "allow")
        self.journal("one").reconcile_permissions(consents=store.allowed_scopes(REPO))
        store.save(REPO, DOCKER, CLAUDE, "allow")
        self.journal("two").reconcile_permissions(consents=store.allowed_scopes(REPO))
        self.assertEqual({f"docker_host: {CODEX.effective}", f"docker_host: {CLAUDE.effective}"},
                         {r[1] for r in self.rows()})

    def test_a_persistent_answer_shows_as_a_consent_the_moment_it_is_stored(self):
        store = grants.ConsentStore(self.root / "consents.json")
        broker = grants.ConsentBroker(REPO, platform="linux", store=store,
                                      answers={"docker_host": grants.ALLOW_PERSISTENT},
                                      observer=self.journal().consent_event)
        broker.request(DOCKER, "codex", role="implement")
        self.assertEqual({"--grant", "consents.json"}, {r[0] for r in self.rows()})
        self.assertEqual(grants.ALLOW_PERSISTENT, next(r[2] for r in self.rows() if r[0] == "--grant"))


class IgnoredConfigurationTests(RunCycleTestCase):
    """A run that could not read the configuration withdraws nothing (#314)."""

    def setUp(self):
        super().setUp()
        self.root = Path(self.store.path).parent
        repository = self.root / "repository"
        repository.mkdir()
        git(repository, "init", "-q", "-b", "base")
        git(repository, "commit", "-q", "--allow-empty", "-m", "initial")
        # --local-only needs a linked worktree; the view is keyed by its branch.
        self.checkout = self.root / "checkout"
        git(repository, "worktree", "add", "-q", "-b", "main", str(self.checkout))

    def scopes(self):
        return [d["scope"] for d in cj.permissions_in_force(
            self.store.path, "owner/api", "main", now=datetime.now(timezone.utc))["decisions"]]

    def stored(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            return db.execute("SELECT source, scope, decision, branch FROM journal_permissions "
                              "WHERE source = 'user_choices' ORDER BY permission_id").fetchall()

    def cycle(self, choices):
        self.run_cycle(Talker("claude"), Talker("codex"), cwd=str(self.checkout),
                       local_only=True, user_tool_choices=choices)

    def passed_choices(self, *argv, config=None):
        if config is not None:
            (self.checkout / ".code-cycle.yml").write_text(config, encoding="utf-8")
        report = rc.CycleReport(repo_id="owner/api", task_id="API-7",
                                status=rc.APPROVED_END, verdict="APPROVED")
        with mock.patch.object(rc, "run_cycle", return_value=report) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            rc.main(["--repo", "owner/api", "--task", "API-7", "--workspace", "current",
                     "--cwd", str(self.checkout), "--database", str(self.root / "cli.sqlite"),
                     *argv])
        return run.call_args.kwargs["user_tool_choices"]

    def test_the_cli_supplies_no_choices_when_the_configuration_was_not_read(self):
        self.assertIsNone(self.passed_choices("--no-config"))
        self.assertIsNone(self.passed_choices())  # no configuration file beside the work
        # With a file it supplies what the file says, even when that is nothing.
        self.assertEqual(("rg",), self.passed_choices(
            config="code_cycle:\n  user_choices:\n    tools: [rg]\n")["tools"])
        self.assertEqual({"tools": (), "commands": (), "integrations": (), "registry_domains": ()},
                         self.passed_choices(config="code_cycle: {}\n"))
        self.assertIsNone(self.passed_choices("--no-config"))

    def test_a_no_config_run_leaves_recorded_choices_in_force(self):
        self.cycle({"tools": ("rg", "jq")})
        recorded = self.stored()
        self.assertEqual(["tools: jq", "tools: rg"], sorted(self.scopes()))
        self.cycle(None)  # what `--no-config` and an absent configuration pass
        self.assertEqual(recorded, self.stored())
        self.assertEqual(["tools: jq", "tools: rg"], sorted(self.scopes()))
        # The next normal run finds them held and records nothing new.
        self.cycle({"tools": ("rg", "jq")})
        self.assertEqual(recorded, self.stored())

    def test_a_normal_run_still_records_a_removal(self):
        self.cycle({"tools": ("rg", "jq")})
        self.cycle(None)
        self.cycle({"tools": ("rg",)})
        self.assertEqual(["tools: rg"], self.scopes())
        self.assertEqual([("user_choices", "tools: jq", cj.WITHDRAWN, "main")],
                         [row for row in self.stored() if row[2] == cj.WITHDRAWN])


class StageAndWorktreeTests(PermissionViewCase):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "main-checkout"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "initial")
        self.second = self.root / "second"
        git(self.repo, "worktree", "add", "-q", "-b", "feature", str(self.second))

    def cli(self, *args, cwd):
        out = subprocess.run(
            [sys.executable, str(Path(cj.__file__)), "--database", str(self.path), *args],
            cwd=cwd, capture_output=True, text=True)
        self.assertEqual(0, out.returncode, out.stderr)
        return out.stdout

    def test_a_second_linked_worktree_reads_what_the_first_recorded(self):
        # The first checkout records its run; the store is the user's, not the checkout's.
        first = self.journal("from-main")
        first.reconcile_permissions(user_choices={"tools": ("rg",)}, branch="main")
        first.permission("consents.json", "docker_host: x", "allow")
        self.journal("from-feature").reconcile_permissions(
            user_choices={"tools": ("jq",)}, branch="feature")
        shown = json.loads(self.cli("permissions", "--repo", REPO, "--json", cwd=self.second))
        self.assertEqual("feature", shown["branch"])
        self.assertEqual(["docker_host: x", "tools: jq"], sorted(d["scope"] for d in shown["decisions"]))
        main = json.loads(self.cli("permissions", "--repo", REPO, "--json", cwd=self.repo))
        self.assertEqual(["docker_host: x", "tools: rg"], sorted(d["scope"] for d in main["decisions"]))

    def test_two_worktrees_on_one_branch_share_one_identity(self):
        third = self.root / "third"
        git(self.repo, "worktree", "add", "-q", "-f", str(third), "feature")
        self.journal("from-second").reconcile_permissions(
            user_choices={"tools": ("rg",)}, branch="feature")
        for checkout in (self.second, third):
            text = self.cli("permissions", "--repo", REPO, cwd=checkout)
            self.assertIn("tools: rg", text)
            self.assertIn("branch feature", text)
        # A removal seen from the other worktree of the branch is the branch's removal.
        self.journal("from-third").reconcile_permissions(user_choices={}, branch="feature")
        self.assertIn("No permission decisions", self.cli("permissions", "--repo", REPO, cwd=self.second))

    def test_the_command_is_read_only_and_names_a_missing_store(self):
        self.journal().permission("consents.json", "docker_host: x", "allow")
        before = self.path.read_bytes()
        self.cli("permissions", "--repo", REPO, "--branch", "main", cwd=self.repo)
        self.assertEqual(before, self.path.read_bytes())
        out = subprocess.run(
            [sys.executable, str(Path(cj.__file__)), "--database", str(self.root / "absent.sqlite"),
             "permissions", "--repo", REPO], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(2, out.returncode)

    def policy(self, **kw):
        values = dict(role="implement", repo=REPO, journal_path=str(self.path),
                      checkout_branch="feature", tmpdir=str(self.root))
        values.update(kw)
        return sp.StagePolicy(**values)

    def test_a_stage_may_run_exactly_the_runtime_command(self):
        policy = self.policy()
        command = shlex.join(policy.permissions_command)
        self.assertTrue(sp.check(command, policy).allowed)
        for forbidden in (
                command + " --repo other/repo",
                command + " --json",
                command.replace("feature", "main"),
                command.replace(REPO, "other/repo"),
                command.replace(str(self.path), str(self.root / "other.sqlite")),
                command + f" > {self.root}/../out",
                command + " ; id",
        ):
            with self.subTest(command=forbidden):
                self.assertFalse(sp.check(forbidden, policy).allowed)
        # Without a journal or a branch the table offers no such command.
        for missing in ({"journal_path": None}, {"checkout_branch": None}):
            self.assertEqual((), self.policy(**missing).permissions_command)
            self.assertFalse(sp.check(command, self.policy(**missing)).allowed)

    def test_the_command_is_announced_and_pre_allowed_only_for_a_stage_that_has_it(self):
        policy = self.policy()
        command = shlex.join(policy.permissions_command)
        self.assertIn(command, sp.prompt_note(policy))
        self.assertIn(f"Bash({command}:*)", sp.allowed_tool_patterns(policy))
        bare = self.policy(checkout_branch=None)
        self.assertNotIn("permissions", sp.prompt_note(bare))

    def test_the_policy_round_trips_through_the_hook_file(self):
        policy = self.policy()
        again = sp.StagePolicy.from_json(policy.to_json())
        self.assertEqual(policy.permissions_command, again.permissions_command)

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "file modes do not restrict this user")
    def test_a_sandboxed_stage_reads_the_store_without_write_access(self):
        journal = self.journal()
        journal.permission("consents.json", "docker_host: x", "allow")
        journal.permission("user_choices", "tools: rg", "allow", branch="feature")
        policy = self.policy()
        command = list(policy.permissions_command)
        # Nothing in the store's directory or file may be created or changed.
        database_dir = self.path.parent
        listing = sorted(p.name for p in database_dir.iterdir())
        os.chmod(self.path, 0o444)
        os.chmod(database_dir, 0o555)
        self.addCleanup(os.chmod, database_dir, 0o755)
        self.addCleanup(os.chmod, self.path, 0o644)
        # A clean environment: no HOME, no inherited state, only what the policy names.
        out = subprocess.run(command, cwd=self.second, capture_output=True, text=True,
                             env={"PATH": os.environ.get("PATH", "")})
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertIn("docker_host: x", out.stdout)
        self.assertIn("tools: rg", out.stdout)
        self.assertEqual(listing, sorted(p.name for p in database_dir.iterdir()))


class RuntimeRecordingTests(RunCycleTestCase):
    def setUp(self):
        super().setUp()
        self.checkout = Path(self.home).parent / "checkout"
        self.checkout.mkdir(parents=True)
        git(self.checkout, "init", "-q", "-b", "main")
        git(self.checkout, "commit", "-q", "--allow-empty", "-m", "initial")
        self.linked = Path(self.home).parent / "linked"
        git(self.checkout, "worktree", "add", "-q", "-b", "task-311", str(self.linked))

    profiles = router.load_profiles({"code_cycle": {"profiles": {
        "cheap_coder": {"primary": "claude:anthropic/claude-sonnet-5 high"},
        "reviewer": {"primary": "codex:openai/gpt-6-luna high"},
    }}})

    def run_in_linked(self, implementer=None, **kw):
        return self.run_cycle(implementer or Talker("claude", block("BLOCKED")),
                              Talker("codex", block("BLOCKED")), profiles=self.profiles,
                              cwd=str(self.linked), local_only=True, **kw)

    def view(self, branch="task-311"):
        return cj.permissions_in_force(self.store.path, "owner/api", branch)["decisions"]

    def test_a_run_records_its_branch_choices_and_withdraws_a_removed_one(self):
        self.run_in_linked(user_tool_choices={"tools": ("rg", "jq"), "commands": ()})
        self.assertEqual(["tools: jq", "tools: rg"], sorted(d["scope"] for d in self.view()))
        self.run_in_linked(user_tool_choices={"tools": ("rg",)})
        self.assertEqual(["tools: rg"], [d["scope"] for d in self.view()])
        self.assertEqual([], self.view("main"))

    def test_a_run_without_supplied_choices_withdraws_nothing(self):
        self.run_in_linked(user_tool_choices={"tools": ("rg",)})
        self.run_in_linked()
        self.assertEqual(["tools: rg"], [d["scope"] for d in self.view()])

    def test_a_cycle_scoped_grant_shows_while_it_runs_and_not_after(self):
        store = grants.ConsentStore(self.home / "consents.json")
        broker = grants.ConsentBroker("owner/api", platform="linux", store=store,
                                      answers={"docker_host": grants.ALLOW_CYCLE})
        seen = []

        class Watching(Talker):
            def dispatch(inner, target, task, **kw):
                broker.request(DOCKER, "codex", role="implement")
                seen.append(self.view())
                return super().dispatch(target, task, **kw)

        self.run_in_linked(Watching("claude", block("BLOCKED")), consent=broker)
        self.assertEqual(["cycle", "cycle"], [d["level"] for d in seen[0]])
        self.assertEqual([], self.view())

    def test_the_stage_policy_carries_the_checkout_branch(self):
        claude = Talker("claude", block("BLOCKED"))
        seen = []
        original = claude.dispatch

        def dispatch(target, task, **kw):
            seen.append(kw.get("command_policy"))
            return original(target, task, **kw)

        claude.dispatch = dispatch
        self.run_in_linked(claude)
        policy = seen[0]
        self.assertEqual("task-311", policy.checkout_branch)
        self.assertEqual("owner/api", policy.repo)
        self.assertEqual(str(self.store.path), policy.journal_path)
        self.assertIn("permissions", policy.permissions_command)


if __name__ == "__main__":
    unittest.main()
