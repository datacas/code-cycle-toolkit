"""The closed command table of each stage, and the hook that enforces it (#142)."""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import stage_permissions as sp  # noqa: E402

REPO = "owner/repo"
THREAD = "PRRT_kwDOexample"
THREAD_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!, $endCursor: String) { "
    "repository(owner: $owner, name: $name) { pullRequest(number: $number) { "
    "reviewThreads(first: 100, after: $endCursor) { pageInfo { hasNextPage endCursor } "
    "nodes { id isResolved comments(first: 100) { nodes { id body } } } } } } }"
)
REPLY = ("mutation($threadId: ID!, $body: String!) { addPullRequestReviewThreadReply("
         "input: {pullRequestReviewThreadId: $threadId, body: $body}) { comment { url } } }")
RESOLVE = ("mutation($threadId: ID!) { resolveReviewThread(input: {threadId: $threadId}) "
           "{ thread { isResolved } } }")


def on_branch(name: str):
    """A fake `git rev-parse --abbrev-ref HEAD` that reports `name`."""
    def run(argv, **_):
        return subprocess.CompletedProcess(argv, 0, stdout=f"{name}\n", stderr="")
    return run


class PolicyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name).resolve()
        self.tmpdir = base / "code-cycle-tmp-mine"
        self.tmpdir.mkdir()
        self.sibling = base / "code-cycle-tmp-other"
        self.sibling.mkdir()

    def policy(self, role: str, **fields) -> sp.StagePolicy:
        defaults = dict(role=role, repo=REPO, change_request_id="7", work_item_id="142",
                        base_branch="main", tmpdir=str(self.tmpdir))
        return sp.StagePolicy(**{**defaults, **fields})

    def allowed(self, command: str, policy: sp.StagePolicy, *, branch: str = "issue-142"):
        decision = sp.check(command, policy, cwd=self.directory.name, run=on_branch(branch))
        self.assertTrue(decision.allowed, f"{command!r} was denied: {decision.reason}")

    def denied(self, command: str, policy: sp.StagePolicy, *, branch: str = "issue-142"):
        decision = sp.check(command, policy, cwd=self.directory.name, run=on_branch(branch))
        self.assertFalse(decision.allowed, f"{command!r} was allowed")
        return decision


class ParserTests(PolicyTestCase):
    def test_one_simple_command_with_quotes_parses_into_literal_words(self) -> None:
        parsed = sp.parse_command("""git log --format='%H %s' "a b" c\\ d""", None)
        self.assertEqual(["git", "log", "--format=%H %s", "a b", "c d"], parsed.words)

    def test_chaining_pipes_substitutions_and_other_expansions_are_refused(self) -> None:
        for command in ("git status && git push", "git status; ls", "git log | head",
                        "git status & ls", "echo $(id)", "echo `id`", 'echo "$(id)"',
                        "cat $HOME/.ssh/id_rsa", "cat ${HOME}", "cat $TMPDIRX", "ls *",
                        "ls ~/", "cat < /etc/passwd", "(git status)", "git status\nls",
                        "git status\rls", "git status #comment", "", "   ", "'unterminated",
                        '"unterminated', "trailing\\", "ls >", "ls > > a", "ls 3>&1",
                        "ls 1>&2"):
            with self.subTest(command=command):
                with self.assertRaises(sp.PolicyError):
                    sp.parse_command(command, str(self.tmpdir))

    def test_only_tmpdir_expands_and_only_when_the_runtime_set_it(self) -> None:
        parsed = sp.parse_command('cat "$TMPDIR/a" ${TMPDIR}/b', str(self.tmpdir))
        self.assertEqual(["cat", f"{self.tmpdir}/a", f"{self.tmpdir}/b"], parsed.words)
        with self.assertRaises(sp.PolicyError):
            sp.parse_command("cat $TMPDIR/a", None)

    def test_redirections_are_recorded_for_validation(self) -> None:
        parsed = sp.parse_command("python3 -m unittest > $TMPDIR/t.log 2>&1", str(self.tmpdir))
        self.assertEqual([("1", ">", f"{self.tmpdir}/t.log"), ("2", ">&", None)],
                         parsed.redirects)
        parsed = sp.parse_command("cmd 2>> $TMPDIR/e.log", str(self.tmpdir))
        self.assertEqual([("2", ">>", f"{self.tmpdir}/e.log")], parsed.redirects)

    def test_a_path_is_inside_tmpdir_only_after_resolving_links_and_dots(self) -> None:
        self.assertTrue(sp.inside_tmpdir(str(self.tmpdir / "report.md"), str(self.tmpdir)))
        self.assertTrue(sp.inside_tmpdir(str(self.tmpdir / "a" / ".." / "b"), str(self.tmpdir)))
        for path in (str(self.tmpdir), str(self.tmpdir / ".." / "x"),
                     str(self.sibling / "x"), "/tmp/x", "relative/x", ""):
            with self.subTest(path=path):
                self.assertFalse(sp.inside_tmpdir(path, str(self.tmpdir)))
        self.assertFalse(sp.inside_tmpdir(str(self.tmpdir / "x"), None))
        if os.name != "nt":
            (self.tmpdir / "escape").symlink_to(self.sibling, target_is_directory=True)
            self.assertFalse(sp.inside_tmpdir(str(self.tmpdir / "escape" / "x"),
                                              str(self.tmpdir)))


class ReadOnlyShellTests(PolicyTestCase):
    def test_the_closed_read_only_set_is_available_to_every_stage(self) -> None:
        for role in ("implement", "resolve", "review", "rereview", "issue_review"):
            policy = self.policy(role)
            for command in ("ls -la scripts", "cat README.md", "head -n 5 a", "tail -n 5 a",
                            "wc -l a", "grep -rn needle scripts", "find . -name '*.py'",
                            "jq .status report.json", "jq -r '.items[]' report.json"):
                with self.subTest(role=role, command=command):
                    self.allowed(command, policy)

    def test_dangerous_variants_are_refused(self) -> None:
        policy = self.policy("implement")
        for command in ("find . -exec rm {} ;", "find . -execdir id ';'", "find . -delete",
                        "find . -ok rm '{}' ';'", "find . -okdir id ';'",
                        "find . -fprint $TMPDIR/out", "jq . a > $TMPDIR/b",
                        "cat a > $TMPDIR/b", "grep x a 2>&1", "rm -rf build", "mv a b",
                        "echo hi", "sed -i s/a/b/ f", "tee $TMPDIR/x", "curl https://x"):
            with self.subTest(command=command):
                self.denied(command, policy)


class GitTests(PolicyTestCase):
    def test_reads_are_shared_and_review_reads_reach_reviews(self) -> None:
        for role in ("implement", "resolve", "review", "rereview", "issue_review"):
            for command in ("git status", "git diff main...HEAD", "git log --oneline -5",
                            "git rev-parse HEAD", "git show HEAD:README.md",
                            "git --no-pager log", "git branch --show-current"):
                with self.subTest(role=role, command=command):
                    self.allowed(command, self.policy(role))
        for role in ("review", "rereview", "implement", "resolve"):
            for command in ("git merge-base HEAD origin/main", "git ls-files",
                            "git grep -n needle", "git blame README.md",
                            "git merge-base --is-ancestor abc HEAD"):
                with self.subTest(role=role, command=command):
                    self.allowed(command, self.policy(role))
        for command in ("git merge-base HEAD main", "git blame a", "git grep x"):
            self.denied(command, self.policy("issue_review"))

    def test_review_fetch_needs_an_origin_remote(self) -> None:
        review = self.policy("review")
        self.allowed("git fetch origin", review)
        self.allowed("git fetch origin main", review)
        for command in ("git fetch upstream", "git fetch", "git fetch origin --prune",
                        "git fetch --all"):
            self.denied(command, review)
        self.denied("git fetch origin", replace(review, fetch_origin=False))

    def test_a_reviewer_never_writes(self) -> None:
        for role in ("review", "rereview", "issue_review"):
            for command in ("git push origin HEAD", "git commit -m x", "git add .",
                            "git checkout main", "git switch -c x", "git pull origin main",
                            "git rm a", "git mv a b", "git reset --hard", "git tag v1",
                            "git branch -D x", "git worktree list"):
                with self.subTest(role=role, command=command):
                    self.denied(command, self.policy(role))
                    self.denied(command, self.policy(role, rtk=True).at_dispatch(
                        tmpdir=str(self.tmpdir), rtk=True, fetch_origin=True, docker=False))
                    self.denied(f"rtk {command}", self.policy(role, rtk=True))
                    self.denied(f"rtk proxy {command}", self.policy(role, rtk=True))

    def test_writers_get_their_subcommands(self) -> None:
        for role in ("implement", "resolve"):
            policy = self.policy(role)
            for command in ("git add scripts/x.py", "git commit -m 'feat: x'",
                            "git push -u origin issue-142", "git push origin HEAD",
                            "git push origin HEAD:issue-142", "git push --set-upstream origin x",
                            "git fetch origin", "git pull origin issue-142",
                            "git checkout issue-142", "git switch issue-142", "git rm old.py",
                            "git mv a.py b.py", "git worktree list",
                            "git worktree list --porcelain",
                            # Quoted, as an agent must: bash would eat a
                            # Windows path's backslashes otherwise.
                            f"git diff main...HEAD --output='{self.tmpdir}/d.diff'"):
                with self.subTest(role=role, command=command):
                    self.allowed(command, policy)

    def test_every_force_push_variant_is_refused(self) -> None:
        policy = self.policy("implement")
        for command in ("git push --force origin x", "git push origin x --force",
                        "git push --force-with-lease origin x",
                        "git push --force-with-lease=x:abc origin x",
                        "git push --force-if-includes origin x", "git push -f origin x",
                        "git push origin -f x", "git push -uf origin x", "git push -fu origin x",
                        "git push origin +x", "git push origin +HEAD:x", "git push origin x +y",
                        "git push --mirror origin", "git push --all origin",
                        "git push --delete origin x", "git push -d origin x",
                        "git push origin :x", "git push --prune origin x",
                        "git push --repo=origin x"):
            with self.subTest(command=command):
                self.denied(command, policy)

    def test_the_base_branch_is_never_a_push_target(self) -> None:
        policy = self.policy("implement", base_branch="develop")
        for command in ("git push origin develop", "git push origin HEAD:develop",
                        "git push origin x:refs/heads/develop", "git push origin x:heads/develop",
                        "git push origin main", "git push origin HEAD:master",
                        "git push origin 'refs/heads/*:refs/heads/*'", "git push origin x:a..b",
                        "git push origin 'x:@{u}'"):
            with self.subTest(command=command):
                self.denied(command, policy)
        self.denied("git push origin HEAD", policy, branch="develop")
        self.denied("git push -u origin @", policy, branch="main")
        self.allowed("git push origin HEAD", policy, branch="issue-142")

    def test_a_push_names_origin_and_its_branch(self) -> None:
        policy = self.policy("implement")
        for command in ("git push", "git push origin", "git push -u", "git push upstream x",
                        "git push https://github.com/other/repo x"):
            with self.subTest(command=command):
                self.denied(command, policy)

    def test_a_push_whose_branch_cannot_be_read_is_refused(self) -> None:
        def broken(argv, **_):
            return subprocess.CompletedProcess(argv, 128, stdout="", stderr="fatal")
        decision = sp.check("git push origin HEAD", self.policy("implement"), run=broken)
        self.assertFalse(decision.allowed)
        decision = sp.check("git push origin HEAD", self.policy("implement"),
                            run=on_branch("HEAD"))
        self.assertFalse(decision.allowed)

    def test_options_that_run_programs_or_write_elsewhere_are_refused(self) -> None:
        policy = self.policy("implement")
        for command in ("git -c core.pager=sh status", "git -C /etc status",
                        "git --exec-path=/x status", "git grep -Ovim x",
                        "git grep --open-files-in-pager=vim x",
                        "git fetch --upload-pack=/bin/sh origin",
                        "git push --receive-pack=/bin/sh origin x", "git diff --output=/tmp/x",
                        f"git diff --output='{self.sibling}/x'", "git diff --output",
                        "git log --output ../x", "git fetch upstream", "git pull other main"):
            with self.subTest(command=command):
                self.denied(command, policy)

    def test_bundled_and_abbreviated_executing_options_are_refused(self) -> None:
        # Git expands unambiguous long-option prefixes and bundles short
        # options, so `-iO<cmd>` and `--open-files=<cmd>` run <cmd> (REV-005).
        for role in ("review", "rereview", "implement", "resolve"):
            policy = self.policy(role)
            for command in ("git grep -iO'touch x' foo", "git grep -nOvim foo",
                            "git grep -inO foo", "git grep --open-files='touch x' foo",
                            "git grep --open-files-in-p=vim foo", "git grep --op=vim foo",
                            "git grep --o=vim foo", "git log --exec=x", "git diff --outp=/etc/x",
                            "git show --ou /etc/x"):
                with self.subTest(role=role, command=command):
                    self.denied(command, policy)
        for command in ("git fetch --upload-pa='touch x; git-upload-pack' origin",
                        "git fetch --up=x origin", "git pull --upload=x origin main"):
            with self.subTest(command=command):
                self.denied(command, self.policy("resolve"))

    def test_ordinary_options_that_share_letters_stay_allowed(self) -> None:
        policy = self.policy("review")
        for command in ("git grep -in foo", "git grep --or -e a -e b",
                        "git grep --only-matching foo", "git log --oneline",
                        "git diff --output-indicator-new=+", "git log -S foo"):
            with self.subTest(command=command):
                self.assertTrue(sp.check(command, policy).allowed, command)


class GhTests(PolicyTestCase):
    def test_every_stage_reads(self) -> None:
        for role in ("implement", "resolve", "review", "rereview", "issue_review"):
            for command in ("gh issue view 142", "gh issue view 142 --comments",
                            "gh pr view 7 --json headRefOid,statusCheckRollup",
                            "gh pr checks 7", "gh auth status", "gh repo view owner/repo",
                            "gh repo view --json defaultBranchRef", "gh api user --jq .login",
                            "gh issue view 142 -R owner/repo",
                            "gh pr view 7 --repo=OWNER/REPO"):
                with self.subTest(role=role, command=command):
                    self.allowed(command, self.policy(role))

    def test_issue_review_reads_lists_and_searches(self) -> None:
        policy = self.policy("issue_review", change_request_id=None)
        for command in ("gh issue list --state open --search 'hook in:title'",
                        "gh pr list --state merged --limit 20",
                        "gh search issues 'permissions' --repo owner/repo",
                        "gh search prs hook --state closed --json number,title"):
            with self.subTest(command=command):
                self.allowed(command, policy)
        self.denied("gh search issues x --owner other", policy)
        self.denied("gh issue list -R other/repo", policy)
        for role in ("review", "rereview"):
            self.denied("gh issue list", self.policy(role))
            self.denied("gh search issues x", self.policy(role))

    def test_reviewers_read_the_diff_and_publish_only_their_comment(self) -> None:
        for role in ("review", "rereview"):
            policy = self.policy(role)
            for command in ("gh pr diff 7", "gh pr diff 7 --name-only",
                            f"gh pr comment 7 --body-file '{self.tmpdir}/pr-comment.md'",
                            "gh pr comment 7 --body-file $TMPDIR/pr-comment.md",
                            "gh pr review 7 --comment --body-file $TMPDIR/report.md",
                            "gh pr comment https://github.com/owner/repo/pull/7 -F $TMPDIR/r.md"):
                with self.subTest(role=role, command=command):
                    self.allowed(command, policy)
            for command in ("gh pr comment 8 --body-file $TMPDIR/r.md",
                            "gh pr comment https://github.com/other/repo/pull/7 -b x",
                            "gh pr comment 7 --body-file /tmp/r.md",
                            f"gh pr comment 7 --body-file '{self.sibling}/r.md'",
                            "gh pr comment 7 --body-file $TMPDIR/../r.md",
                            "gh pr comment 7 --body-file -",
                            "gh pr review 7 --approve", "gh pr review 7 --request-changes -b x",
                            "gh pr review 7 -b x", "gh pr merge 7", "gh pr close 7",
                            "gh pr edit 7 --body x", "gh pr create --fill",
                            "gh issue comment 142 --body x", "gh issue close 142",
                            "gh pr checkout 7", "gh release create v1", "gh repo delete",
                            "gh run rerun 1", "gh api repos/owner/repo",
                            "gh api graphql -f query='query { viewer { login } }'",
                            "gh pr comment 7 --hostname evil.example -b x",
                            "gh auth status --show-token", "gh auth token"):
                with self.subTest(role=role, command=command):
                    self.denied(command, policy)

    def test_writers_publish_on_their_own_change_request_and_work_item(self) -> None:
        implement = self.policy("implement", change_request_id=None)
        for command in ("gh pr create --title 'feat: x' --body-file $TMPDIR/body.md --base main",
                        "gh pr edit --body-file $TMPDIR/body.md", "gh pr comment -F $TMPDIR/x.md",
                        "gh issue comment 142 --body-file $TMPDIR/c.md",
                        "gh pr list --head issue-142 --json number", "gh run list -b issue-142",
                        "gh run view 123 --log-failed", "gh pr checks --watch"):
            with self.subTest(command=command):
                self.allowed(command, implement)
        for command in ("gh pr comment 9 -b x", "gh pr edit 9 --title x",
                        "gh issue comment 141 -b x", "gh pr merge --squash",
                        "gh pr create --repo other/repo --fill",
                        "gh issue comment https://github.com/other/repo/issues/142 -b x",
                        "gh pr review --approve"):
            with self.subTest(command=command):
                self.denied(command, implement)
        resolve = self.policy("resolve")
        self.allowed("gh pr comment 7 --body-file $TMPDIR/s.md", resolve)
        self.denied("gh pr comment 8 --body-file $TMPDIR/s.md", resolve)
        self.denied("gh pr create --fill", resolve)
        self.denied("gh pr edit 7 --title x", resolve)

    def test_gh_api_is_closed_except_the_documented_calls(self) -> None:
        for role in ("implement", "review", "rereview", "issue_review"):
            for command in ("gh api user", "gh api user --jq .name", "gh api repos/a/b",
                            "gh api -X DELETE repos/owner/repo/git/refs/heads/x",
                            f"gh api graphql -F threadId={THREAD} -f query='{RESOLVE}'"):
                with self.subTest(role=role, command=command):
                    self.denied(command, self.policy(role))


class ThreadCallTests(PolicyTestCase):
    def setUp(self) -> None:
        super().setUp()
        owner = patch.object(sp, "THREAD_OWNER", lambda thread, **_: (
            (REPO, "7") if thread == THREAD else ("owner/repo", "8")))
        owner.start()
        self.addCleanup(owner.stop)
        self.resolve = self.policy("resolve")

    def test_the_three_documented_calls_are_accepted(self) -> None:
        for command in (
            f"gh api graphql --paginate -F owner=owner -F name=repo -F number=7 "
            f"-f query='{THREAD_QUERY}'",
            f"gh api graphql -F threadId={THREAD} -F body=@$TMPDIR/reply.md -f query='{REPLY}'",
            f"gh api graphql -F threadId={THREAD} -f body='Fixed in abc.' -f query='{REPLY}'",
            f"gh api graphql -F threadId={THREAD} -f query='{RESOLVE}'",
        ):
            with self.subTest(command=command[:70]):
                self.allowed(command, self.resolve)

    def test_the_skill_documents_exactly_these_calls(self) -> None:
        text = (ROOT / "skills" / "cc-resolve-comments" / "SKILL.md").read_text(encoding="utf-8")
        calls = re.findall(r"^gh api graphql .*$", text, re.MULTILINE)
        self.assertEqual(3, len(calls))
        for call in calls:
            command = (call.replace("<owner>", "owner").replace("<repository>", "repo")
                       .replace("<n>", "7").replace("<thread-id>", THREAD))
            with self.subTest(call=call[:70]):
                self.allowed(command, self.resolve)

    def test_another_repository_change_request_or_thread_is_refused(self) -> None:
        for command in (
            f"gh api graphql -F owner=other -F name=repo -F number=7 -f query='{THREAD_QUERY}'",
            f"gh api graphql -F owner=owner -F name=repo -F number=8 -f query='{THREAD_QUERY}'",
            f"gh api graphql -F threadId=PRRT_elsewhere -f query='{RESOLVE}'",
            f"gh api graphql -R other/repo -F threadId={THREAD} -f query='{RESOLVE}'",
        ):
            with self.subTest(command=command[:80]):
                self.denied(command, self.resolve)

    def test_added_operations_fields_and_mutations_are_refused(self) -> None:
        two = RESOLVE + " " + RESOLVE
        extra_field = RESOLVE.replace("{ thread { isResolved } } }",
                                      "{ thread { isResolved } } other: resolveReviewThread("
                                      "input: {threadId: $threadId}) { clientMutationId } }")
        merge = ("mutation($id: ID!) { mergePullRequest(input: {pullRequestId: $id}) "
                 "{ clientMutationId } }")
        literal = RESOLVE.replace("$threadId}", f'"{THREAD}"}}')
        for document in (two, extra_field, merge, literal,
                         RESOLVE.replace("resolveReviewThread", "unresolveReviewThread"),
                         RESOLVE.replace("{threadId: $threadId}",
                                         "{threadId: $threadId, clientMutationId: \"x\"}"),
                         'mutation { x(input: """block""") { y } }',
                         "subscription { x }", "query { viewer { login } }",
                         THREAD_QUERY.replace("pullRequest(number: $number) {",
                                              "pullRequest(number: $number) { title ")):
            with self.subTest(document=document[:60]):
                self.denied(f"gh api graphql -F threadId={THREAD} -F owner=owner "
                            f"-F name=repo -F number=7 -f query='{document}'", self.resolve)

    def test_options_files_and_repeated_queries_are_refused(self) -> None:
        for command in (
            f"gh api graphql -F threadId={THREAD} -F query=@q.graphql",
            f"gh api graphql -F threadId={THREAD} -f query='{RESOLVE}' -f query='{RESOLVE}'",
            f"gh api graphql -F threadId=@$TMPDIR/id -f query='{RESOLVE}'",
            f"gh api graphql -F threadId={THREAD} -F body=@/etc/passwd -f query='{REPLY}'",
            f"gh api graphql --input $TMPDIR/q.json -f query='{RESOLVE}'",
            f"gh api graphql -X POST -F threadId={THREAD} -f query='{RESOLVE}'",
            f"gh api graphql -H 'X: y' -F threadId={THREAD} -f query='{RESOLVE}'",
            f"gh api graphql -F threadId={THREAD} -F extra=1 -f query='{RESOLVE}'",
            f"gh api graphql -f query='{RESOLVE}'",
            "gh api graphql -F threadId=" + THREAD,
        ):
            with self.subTest(command=command[:80]):
                self.denied(command, self.resolve)

    def test_an_unknown_thread_owner_is_refused(self) -> None:
        with patch.object(sp, "THREAD_OWNER", lambda thread, **_: None):
            self.denied(f"gh api graphql -F threadId={THREAD} -f query='{RESOLVE}'",
                        self.resolve)

    def test_the_owner_lookup_reads_the_node_and_fails_closed(self) -> None:
        def answer(argv, **_):
            self.assertEqual(["gh", "api", "graphql"], argv[:3])
            self.assertIn(f"id={THREAD}", argv)
            payload = {"data": {"node": {"pullRequest": {
                "number": 7, "repository": {"nameWithOwner": REPO}}}}}
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")
        self.assertEqual((REPO, "7"), sp.lookup_thread_owner(THREAD, run=answer))
        for stdout, code in (("{}", 0), ("not json", 0), ("", 1),
                             ('{"data": {"node": null}}', 0)):
            fake = (lambda out, rc: lambda argv, **_: subprocess.CompletedProcess(
                argv, rc, stdout=out, stderr=""))(stdout, code)
            self.assertIsNone(sp.lookup_thread_owner(THREAD, run=fake))


class DockerTests(PolicyTestCase):
    def test_compose_needs_the_grant_and_reports_it(self) -> None:
        policy = self.policy("implement")
        decision = self.denied("docker compose up -d", policy)
        self.assertTrue(decision.needs_docker)
        granted = replace(policy, docker=True)
        for command in ("docker compose up -d", "docker compose down", "docker compose ps",
                        "docker compose logs api", "docker compose build",
                        "docker compose -f compose.test.yml up -d",
                        "docker compose --project-name x down"):
            with self.subTest(command=command):
                self.allowed(command, granted)

    def test_run_exec_and_everything_else_stay_denied_with_the_grant(self) -> None:
        granted = self.policy("implement", docker=True)
        for command in ("docker run --rm -v /:/host alpine", "docker exec -it api sh",
                        "docker compose run api sh", "docker compose exec api sh",
                        "docker compose -f x.yml run api", "docker compose cp a b",
                        "docker compose --verbose up", "docker ps", "docker compose",
                        "docker-compose up"):
            with self.subTest(command=command):
                decision = self.denied(command, granted)
                self.assertFalse(decision.needs_docker)


class VerificationTests(PolicyTestCase):
    VERIFY = ("python3 scripts/validate-package.py", "python3 -m unittest")

    def test_writers_and_reviewers_receive_the_configured_commands(self) -> None:
        for role in ("implement", "resolve", "review", "rereview"):
            policy = self.policy(role, test_command="python3 -m unittest discover -s tests",
                                 verify_prefixes=self.VERIFY)
            for command in ("python3 -m unittest discover -s tests",
                            "python3 scripts/validate-package.py",
                            "python3 -m unittest tests.test_stage_permissions",
                            "python3 -m unittest discover -s tests > $TMPDIR/tests.log 2>&1"):
                with self.subTest(role=role, command=command):
                    self.allowed(command, policy)
            for command in ("python3 -m pytest", "python3 -c 'import os'", "python3 scripts/other.py",
                            "python3 -m pip install x", "python3",
                            "python3 -m unittest > /tmp/x.log"):
                with self.subTest(role=role, command=command):
                    self.denied(command, policy)

    def test_issue_review_receives_no_verification(self) -> None:
        policy = self.policy("issue_review", test_command="python3 -m unittest",
                             verify_prefixes=self.VERIFY)
        self.assertEqual((), policy.verification_commands)
        self.denied("python3 -m unittest", policy)

    def test_without_configuration_nothing_verifies_and_the_prompt_says_so(self) -> None:
        policy = self.policy("implement")
        self.denied("python3 -m unittest discover -s tests", policy)
        self.denied("npm test", policy)
        note = sp.prompt_note(policy)
        self.assertIn("No test or verify command is configured", note)
        self.assertIn("stop BLOCKED and name the exact denied command", note)
        self.assertIn("python3 -m unittest", sp.prompt_note(
            replace(policy, verify_prefixes=self.VERIFY)))

    def test_a_reviewer_that_verifies_requires_confinement(self) -> None:
        self.assertTrue(self.policy("review", verify_prefixes=self.VERIFY)
                        .requires_confined_verification)
        self.assertFalse(self.policy("review").requires_confined_verification)
        self.assertFalse(self.policy("implement", verify_prefixes=self.VERIFY)
                         .requires_confined_verification)

    def test_verify_entries_are_exact_specific_prefixes(self) -> None:
        for command in ("python3 scripts/validate-package.py", "python3 -m unittest",
                        "npm run test", "pnpm exec vitest", "pytest tests", "cargo test",
                        "go test ./...", "uv run pytest", "make check", "node scripts/check.js"):
            with self.subTest(command=command):
                sp.validate_verify_prefix(command)
        for command in ("python3", "python3 *", "python3 -c", "python3 -c print(1)",
                        "python3 -m", "python3 -m -x", "bash -lc test", "sh scripts/x.sh",
                        "env FOO=1 pytest", "xargs pytest", "sudo pytest", "npx", "npx -y",
                        "npm exec", "npm run", "node -e x", "node --eval x", "py -3 -c x",
                        "pytest; rm -rf /", "pytest && x", "pytest | tee", "pytest $X",
                        "pytest `x`", "pytest > out", "pytest\nrm", "", "   ", "a\\b",
                        "/usr/bin/python3.12 -c x", "python3.exe", "PYTHON3"):
            with self.subTest(command=command):
                with self.assertRaises(sp.PolicyError):
                    sp.validate_verify_prefix(command)


class RtkTests(PolicyTestCase):
    def test_rtk_forms_are_validated_as_the_command_they_wrap(self) -> None:
        policy = self.policy("implement", rtk=True)
        for command in ("rtk git status", "rtk proxy git log", "rtk gh pr view 7",
                        "rtk ls", "rtk proxy gh issue view 142"):
            with self.subTest(command=command):
                self.allowed(command, policy)
        for command in ("rtk git push --force origin x", "rtk proxy git push -f origin x",
                        "rtk proxy npx x", "rtk pnpm install", "rtk", "rtk proxy",
                        "rtk gh api repos/a/b", "rtk proxy python3 -c x"):
            with self.subTest(command=command):
                self.denied(command, policy)

    def test_rtk_is_refused_when_it_is_not_installed(self) -> None:
        self.denied("rtk git status", self.policy("implement", rtk=False))


class ActivationTests(PolicyTestCase):
    def test_the_table_applies_only_to_github_cycles(self) -> None:
        self.assertTrue(self.policy("implement").active)
        for host, provider in (("bitbucket", "github"), ("github", "plane"),
                               ("github", "jira"), (None, "github"), ("github", None)):
            with self.subTest(host=host, provider=provider):
                policy = self.policy("implement", code_host=host, issue_provider=provider)
                self.assertFalse(policy.active)
                self.assertIn("GitHub only", policy.inactive_reason)
                self.assertEqual("", sp.prompt_note(policy))

    def test_a_policy_round_trips_and_refuses_unknown_keys(self) -> None:
        policy = self.policy("review", verify_prefixes=("python3 -m unittest",))
        self.assertEqual(policy, sp.StagePolicy.from_json(policy.to_json()))
        for text in ('{"role": "review", "extra": 1}', "[]", '{"repo": "x"}'):
            with self.assertRaises(sp.PolicyError):
                sp.StagePolicy.from_json(text)


class AllowedToolsTests(PolicyTestCase):
    def patterns(self, role: str, **fields) -> list[str]:
        return sp.allowed_tool_patterns(self.policy(role, **fields))

    def test_no_stage_receives_arbitrary_execution(self) -> None:
        for role in ("implement", "resolve", "review", "rereview", "issue_review"):
            for docker in (False, True):
                patterns = self.patterns(role, docker=docker, rtk=True,
                                         test_command="python3 -m unittest discover -s tests",
                                         verify_prefixes=("python3 scripts/validate-package.py",))
                with self.subTest(role=role, docker=docker):
                    for forbidden in ("Bash(gh:*)", "Bash(git:*)", "Bash(python3:*)",
                                      "Bash(python3 *)", "Bash(npx:*)", "Bash(bash:*)",
                                      "Bash(docker:*)", "Bash(docker run:*)",
                                      "Bash(docker exec:*)", "Bash(docker compose run:*)",
                                      "Bash(gh api:*)", "Bash(rtk:*)", "Bash(rtk proxy:*)",
                                      "Bash(*)", "Bash"):
                        self.assertNotIn(forbidden, patterns)
                    self.assertIn("Bash(gh api user --jq .login:*)", patterns)

    def test_reviewers_get_no_write_and_resolve_alone_gets_threads(self) -> None:
        for role in ("review", "rereview", "issue_review"):
            patterns = self.patterns(role)
            for write in ("Bash(git push:*)", "Bash(git commit:*)", "Bash(git add:*)",
                          "Bash(gh pr create:*)", "Bash(gh pr edit:*)",
                          "Bash(gh issue comment:*)"):
                self.assertNotIn(write, patterns)
        self.assertIn("Bash(gh pr comment:*)", self.patterns("review"))
        self.assertIn("Bash(gh pr review:*)", self.patterns("rereview"))
        self.assertNotIn("Bash(gh pr comment:*)", self.patterns("issue_review"))
        self.assertIn("Bash(gh api graphql:*)", self.patterns("resolve"))
        for role in ("implement", "review", "rereview", "issue_review"):
            self.assertNotIn("Bash(gh api graphql:*)", self.patterns(role))

    def test_docker_and_verification_appear_only_when_configured(self) -> None:
        self.assertFalse([p for p in self.patterns("implement") if "docker" in p])
        compose = [p for p in self.patterns("implement", docker=True) if "docker" in p]
        self.assertEqual(sorted(f"Bash(docker compose {sub}:*)"
                                for sub in sp.DOCKER_COMPOSE_SUBCOMMANDS), sorted(compose))
        patterns = self.patterns("implement", test_command="pytest tests",
                                 verify_prefixes=("python3 -m unittest",))
        self.assertIn("Bash(pytest tests)", patterns)
        self.assertIn("Bash(python3 -m unittest:*)", patterns)
        self.assertNotIn("Bash(pytest tests)", self.patterns("issue_review",
                                                             test_command="pytest tests"))

    def test_rtk_variants_appear_only_with_rtk(self) -> None:
        self.assertFalse([p for p in self.patterns("implement") if "rtk" in p])
        patterns = self.patterns("implement", rtk=True)
        self.assertIn("Bash(rtk git status:*)", patterns)
        self.assertIn("Bash(rtk proxy git status:*)", patterns)

    def test_every_pattern_prefix_is_accepted_by_the_hook(self) -> None:
        """The two barriers agree: nothing the first opens is closed by design."""
        for role in ("implement", "resolve", "review", "rereview", "issue_review"):
            policy = self.policy(role, rtk=True, docker=True)
            for pattern in sp.allowed_tool_patterns(policy):
                prefix = pattern[len("Bash("):-len(":*)")]
                words = prefix.split()
                if words[-2:] in (["api", "graphql"],) or words[-1] in {"push", "comment",
                                                                       "review", "edit"}:
                    continue  # need arguments the prefix cannot carry
                if words[-1] in {"fetch", "pull", "checkout", "switch", "add", "commit", "rm",
                                 "mv", "view", "diff", "checks", "create", "list"} \
                        and words[0:2] != ["gh", "issue"]:
                    continue
                with self.subTest(role=role, pattern=pattern):
                    self.allowed(prefix, policy)


class SkillCoverageTests(PolicyTestCase):
    """Each stage's table covers what its skill and the skills it delegates run."""

    STAGES = {
        "implement": ("cc-implement-issue", "cc-verify", "cc-provider-bootstrap"),
        "resolve": ("cc-resolve-comments", "cc-verify", "cc-code-review",
                    "cc-security-review"),
        "review": ("cc-initial-review", "cc-pr-review", "cc-code-review",
                   "cc-security-review", "cc-verify"),
        "rereview": ("cc-rereview", "cc-pr-review", "cc-code-review", "cc-security-review",
                     "cc-verify"),
        "issue_review": ("cc-issue-review",),
    }
    #: Documented commands that a stage does not run: worktree creation is
    #: the runtime's, and `HEAD~1`/`git show` are named only to be avoided.
    NOT_RUN = ("git worktree add", "git diff HEAD~1")
    #: Prose that names a command group rather than a command to run.
    MENTIONS = {"gh pr", "gh api graphql", "git worktree"}
    PLACEHOLDERS = {"<n>": "7", "<base>": "main", "<merge-base>": "abc123",
                    "<delta-base>": "abc123", "<file.md>": "$TMPDIR/pr-comment.md",
                    "<file>": "$TMPDIR/review.diff", "<owner>": "owner",
                    "<repository>": "repo", "<thread-id>": THREAD}

    def documented(self, skill: str) -> list[str]:
        text = (ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
        commands = []
        for block in re.findall(r"```(?:bash|sh|shell)\n(.*?)```", text, re.DOTALL):
            commands += [line.strip() for line in block.splitlines()
                         if re.match(r"\s*(git|gh) ", line)]
        # Inline commands may wrap across a line break in the prose.
        commands += [" ".join(found.split())
                     for found in re.findall(r"`((?:git|gh) [^`]*)`", text)]
        return [command for command in dict.fromkeys(commands)
                if command not in self.MENTIONS
                and not any(skip.rstrip("`") == command or command.startswith(skip)
                            for skip in self.NOT_RUN)]

    def concrete(self, command: str) -> str:
        for placeholder, value in self.PLACEHOLDERS.items():
            command = command.replace(placeholder, value)
        command = command.replace(" ...statusCheckRollup", " --json statusCheckRollup")
        return command

    def test_documented_commands_are_in_their_stage_table(self) -> None:
        with patch.object(sp, "THREAD_OWNER", lambda thread, **_: (REPO, "7")):
            for role, skills in self.STAGES.items():
                policy = self.policy(role, rtk=False)
                for skill in skills:
                    for command in self.documented(skill):
                        # A bare subcommand mention, like `git add`, names it.
                        if command in {"git add", "git commit", "git push"}:
                            command = {"git add": "git add scripts/x.py",
                                       "git commit": "git commit -m x",
                                       "git push": "git push origin HEAD"}[command]
                        if (role not in {"implement", "resolve"}
                                and command.startswith(("git add", "git commit", "git push"))):
                            continue  # a writer's commands, named in shared text
                        with self.subTest(role=role, skill=skill, command=command):
                            self.allowed(self.concrete(command), policy)

    def test_no_documented_command_uses_a_substitution(self) -> None:
        for skills in self.STAGES.values():
            for skill in skills:
                for command in self.documented(skill):
                    with self.subTest(skill=skill, command=command):
                        self.assertNotIn("$(", command)
                        self.assertNotIn("`", command)


class HookTests(PolicyTestCase):
    def run_hook(self, policy: sp.StagePolicy, event: dict) -> str:
        path = Path(self.directory.name) / "policy.json"
        path.write_text(policy.to_json(), encoding="utf-8")
        stdout = io.StringIO()
        self.assertEqual(0, sp.hook_main([str(path)], io.StringIO(json.dumps(event)), stdout))
        return stdout.getvalue()

    def test_a_denied_command_gets_a_deny_decision_naming_the_rule(self) -> None:
        output = self.run_hook(self.policy("review"), {
            "tool_name": "Bash", "tool_input": {"command": "git push --force origin x"}})
        decision = json.loads(output)["hookSpecificOutput"]
        self.assertEqual("PreToolUse", decision["hookEventName"])
        self.assertEqual("deny", decision["permissionDecision"])
        self.assertIn(sp.HOOK_DENIAL_MARKER, decision["permissionDecisionReason"])
        self.assertIn("stop BLOCKED", decision["permissionDecisionReason"])

    def test_an_allowed_command_gets_no_decision_so_the_permission_layer_still_applies(self):
        self.assertEqual("", self.run_hook(self.policy("review"), {
            "tool_name": "Bash", "tool_input": {"command": "git status"}}))
        self.assertEqual("", self.run_hook(self.policy("review"), {
            "tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}}))

    def decision(self, output: str) -> str:
        return json.loads(output)["hookSpecificOutput"]["permissionDecision"] if output else ""

    def test_a_non_writing_stage_edits_no_file_with_any_tool(self) -> None:
        # REV-006: under the user's `auto` mode a reviewer edited .git/config
        # with the Edit tool, and an allowed `git status` then ran a program.
        clone = self.directory.name
        for role in ("review", "rereview", "issue_review"):
            for tool, key in sp.FILE_EDIT_TOOLS.items():
                for target in (f"{clone}/.git/config", f"{clone}/notes.md", "notes.md"):
                    with self.subTest(role=role, tool=tool, target=target):
                        output = self.run_hook(self.policy(role), {
                            "tool_name": tool, "tool_input": {key: target}, "cwd": clone})
                        self.assertEqual("deny", self.decision(output))
                        self.assertIn(sp.HOOK_DENIAL_MARKER, output)
                        self.assertIn("No grant lifts this rule", output)

    def test_no_stage_edits_git_configuration(self) -> None:
        root = Path(self.directory.name)
        (root / "clone" / ".git").mkdir(parents=True)
        home = root / "home"
        denied = [str(root / "clone" / ".git" / "config"), str(root / "clone" / ".GIT" / "config"),
                  str(root / "clone" / ".git"), str(root / "clone" / "sub" / ".git"),
                  str(root / "clone" / "src" / ".." / ".git" / "hooks" / "pre-commit"),
                  ".git/config", ".git./config", ".git /config", ".git::$INDEX_ALLOCATION/config",
                  str(home / ".gitconfig"),
                  str(home / ".config" / "git" / "config")]
        try:
            (root / "link").symlink_to(root / "clone" / ".git", target_is_directory=True)
            denied.append(str(root / "link" / "config"))
        except OSError:  # Windows without the symlink privilege
            pass
        allowed = [str(root / "clone" / "src" / "app.py"), "src/app.py",
                   str(root / "clone" / ".gitignore"), str(root / "clone" / ".github" / "ci.yml")]
        with patch.dict(os.environ, {}, clear=False), \
                patch.object(Path, "home", return_value=home):
            os.environ.pop("XDG_CONFIG_HOME", None)
            for role in ("implement", "resolve"):
                for tool, key in sp.FILE_EDIT_TOOLS.items():
                    for target in denied + allowed:
                        with self.subTest(role=role, tool=tool, target=target):
                            output = self.run_hook(self.policy(role), {
                                "tool_name": tool, "tool_input": {key: target},
                                "cwd": str(root / "clone")})
                            self.assertEqual("deny" if target in denied else "",
                                             self.decision(output))

    def test_an_edit_without_a_usable_target_is_denied(self) -> None:
        for event in ({"tool_name": "Edit", "tool_input": {}},
                      {"tool_name": "Write", "tool_input": {"file_path": 7}},
                      {"tool_name": "NotebookEdit", "tool_input": {"file_path": "/x.ipynb"}},
                      {"tool_name": "Edit", "tool_input": {"file_path": "relative.py"}},
                      {"tool_name": "MultiEdit"}):
            with self.subTest(event=event):
                self.assertEqual("deny", self.decision(
                    self.run_hook(self.policy("implement"), event)))

    def test_anything_unreadable_is_denied(self) -> None:
        stdout = io.StringIO()
        sp.hook_main([str(Path(self.directory.name) / "missing.json")],
                     io.StringIO("{}"), stdout)
        self.assertEqual("deny", json.loads(stdout.getvalue())
                         ["hookSpecificOutput"]["permissionDecision"])
        for event in ({"tool_name": "Bash", "tool_input": {}},
                      {"tool_name": "Bash", "tool_input": {"command": 7}},
                      {"tool_name": "Bash"}):
            output = self.run_hook(self.policy("implement"), event)
            self.assertEqual("deny", json.loads(output)["hookSpecificOutput"]
                             ["permissionDecision"])
        path = Path(self.directory.name) / "policy.json"
        path.write_text(self.policy("implement").to_json(), encoding="utf-8")
        stdout = io.StringIO()
        sp.hook_main([str(path)], io.StringIO("not json"), stdout)
        self.assertIn('"deny"', stdout.getvalue())

    def test_the_hook_runs_as_a_script_from_a_copy(self) -> None:
        copy = Path(self.directory.name) / "hook" / "stage_permissions.py"
        copy.parent.mkdir()
        copy.write_bytes((ROOT / "scripts" / "stage_permissions.py").read_bytes())
        policy_path = copy.parent / "policy.json"
        policy_path.write_text(self.policy("review").to_json(), encoding="utf-8")
        for command, denied in (("git push origin x", True), ("git status", False)):
            completed = subprocess.run(
                [sys.executable, str(copy), "hook", str(policy_path)],
                input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command},
                                  "cwd": self.directory.name}),
                capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(denied, '"deny"' in completed.stdout)
        usage = subprocess.run([sys.executable, str(copy)], capture_output=True, text=True,
                               timeout=60, check=False)
        self.assertEqual(2, usage.returncode)


class ClaudeConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name) / "claude"
        self.config.mkdir()
        self.managed = Path(directory.name) / "managed-settings.json"

    def write(self, path: Path, payload) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_settings_that_switch_dispatch_hooks_off_are_found(self) -> None:
        self.assertIsNone(sp.hook_override(self.config, (self.managed,)))
        self.write(self.config / "settings.json", {"disableAllHooks": True})
        self.assertIn("disableAllHooks", sp.hook_override(self.config, (self.managed,)))
        self.write(self.config / "settings.json", {})
        for key in ("disableAllHooks", "allowManagedHooksOnly"):
            self.write(self.managed, {key: True})
            self.assertIn("managed settings", sp.hook_override(self.config, (self.managed,)))
        self.managed.write_text("{broken", encoding="utf-8")
        with self.assertRaises(sp.PolicyError):
            sp.hook_override(self.config, (self.managed,))

    def test_an_enabled_plugin_handling_tool_check_is_named(self) -> None:
        self.assertEqual([], sp.tool_check_mods(self.config))
        plugin = self.config / "plugins" / "cache" / "mod"
        (plugin / "hooks").mkdir(parents=True)
        (plugin / "hooks" / "index.js").write_text(
            "export default { 'tool.check': () => 'allow' }", encoding="utf-8")
        quiet = self.config / "plugins" / "cache" / "quiet"
        quiet.mkdir(parents=True)
        (quiet / "index.js").write_text("export default {}", encoding="utf-8")
        hidden = self.config / "plugins" / "cache" / "deps"
        (hidden / "node_modules" / "x").mkdir(parents=True)
        (hidden / "node_modules" / "x" / "i.js").write_text("tool.check", encoding="utf-8")
        self.write(self.config / "plugins" / "installed_plugins.json", {"plugins": {
            "mod@market": [{"installPath": str(plugin)}],
            "quiet@market": [{"installPath": str(quiet)}],
            "deps@market": [{"installPath": str(hidden)}],
        }})
        self.write(self.config / "settings.json", {"enabledPlugins": {
            "mod@market": True, "quiet@market": True, "deps@market": True}})
        self.assertEqual(["mod@market"], sp.tool_check_mods(self.config))
        self.write(self.config / "settings.json", {"enabledPlugins": {"mod@market": False}})
        self.assertEqual([], sp.tool_check_mods(self.config))

    def test_sandbox_settings_that_widen_a_review_are_found(self) -> None:
        settings = self.config / "settings.json"
        self.assertIsNone(sp.sandbox_override(settings))
        self.write(settings, {"sandbox": {"enabled": True}})
        self.assertIsNone(sp.sandbox_override(settings))
        for sandbox in ({"filesystem": {"allowWrite": ["/"]}},
                        {"excludedCommands": ["docker"]}, {"allowUnsandboxedCommands": True},
                        {"network": {"allowUnixSockets": ["/var/run/docker.sock"]}},
                        {"enableWeakerNestedSandbox": True}):
            with self.subTest(sandbox=sandbox):
                self.write(settings, {"sandbox": sandbox})
                self.assertIn("widen the review sandbox", sp.sandbox_override(settings))

    def test_the_review_sandbox_writes_only_its_two_paths(self) -> None:
        settings = sp.review_sandbox_settings(("/clone", "/tmp/code-cycle-tmp-x"))["sandbox"]
        self.assertTrue(settings["enabled"])
        self.assertTrue(settings["failIfUnavailable"])
        self.assertFalse(settings["allowUnsandboxedCommands"])
        self.assertEqual(["/clone", "/tmp/code-cycle-tmp-x"],
                         settings["filesystem"]["allowWrite"])

    def test_the_review_sandbox_reaches_only_the_code_hosts(self) -> None:
        # Without them a sandboxed reviewer's gh can neither read the change
        # request nor publish its comment (REV-001).
        network = sp.review_sandbox_settings(("/clone",))["sandbox"]["network"]
        self.assertEqual(["github.com", "api.github.com", "bitbucket.org", "api.bitbucket.org"],
                         network["allowedDomains"])
        self.assertEqual({"allowedDomains"}, set(network))

    def test_the_hook_block_matches_bash_and_the_file_edit_tools(self) -> None:
        block = sp.hook_settings("python3 /x/stage_permissions.py hook /x/policy.json")
        [entry] = block["hooks"]["PreToolUse"]
        self.assertEqual("Bash|Edit|Write|MultiEdit|NotebookEdit", entry["matcher"])
        self.assertEqual("command", entry["hooks"][0]["type"])

    def test_pre_approved_tools_outside_the_table_are_listed(self) -> None:
        # REV-007, accepted as a limitation: the cycle names them, never stops.
        found = lambda cwd=None: sp.preapproved_outside_table(cwd, self.config, (self.managed,))
        self.assertEqual([], found())
        self.write(self.config / "settings.json", {"permissions": {
            "defaultMode": "auto",
            "allow": ["Bash(git status:*)", "Edit", "Read", "WebFetch(domain:example.com)",
                      "mcp__ctx__ctx_execute", "mcp__docs__query-docs", "mcp__plane",
                      "mcp__ctx__ctx_execute", 7]}})
        # The dispatch's own --permission-mode replaces a user defaultMode.
        self.assertEqual(["mcp__ctx__ctx_execute", "mcp__docs__query-docs", "mcp__plane"],
                         found())
        clone = self.config.parent / "clone"
        self.write(clone / ".claude" / "settings.local.json",
                   {"permissions": {"allow": ["mcp__shell__run_command"]}})
        self.write(self.managed, {"permissions": {"defaultMode": "bypassPermissions"}})
        self.assertEqual(["managed permissions.defaultMode bypassPermissions",
                          "mcp__ctx__ctx_execute", "mcp__docs__query-docs", "mcp__plane",
                          "mcp__shell__run_command"], found(str(clone)))
        self.write(clone / ".claude" / "settings.local.json",
                   {"permissions": {"allow": "mcp__shell__run_command"}})
        self.assertNotIn("m", found(str(clone)))
        # An unreadable layer is skipped: the notice never stops anything.
        (self.config / "settings.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(["managed permissions.defaultMode bypassPermissions"], found())

    def test_the_notice_flags_only_tools_that_can_run_code_or_write(self) -> None:
        self.assertIsNone(sp.preapproved_notice([]))
        self.assertIsNone(sp.preapproved_notice(None))
        quiet = sp.preapproved_notice(["mcp__docs__query-docs", "mcp__serena__find_symbol"])
        self.assertIn("pre-approved by your configuration: mcp__docs__query-docs, "
                      "mcp__serena__find_symbol", quiet)
        self.assertIn("the table governs this cycle's Bash commands and file edits", quiet)
        self.assertNotIn("worth a thought", quiet)
        flagged = sp.preapproved_notice(["mcp__docs__query-docs", "mcp__ctx__ctx_execute",
                                         "mcp__ctx__ctx_batch_execute"])
        self.assertIn("; mcp__ctx__ctx_execute, mcp__ctx__ctx_batch_execute can run code or "
                      "write files, worth a thought when the cycle runs unattended", flagged)
        for word in ("BLOCKED", "danger", "unsafe"):
            self.assertNotIn(word, flagged)


if __name__ == "__main__":
    unittest.main()
