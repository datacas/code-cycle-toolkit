#!/usr/bin/env python3
"""Manual native parity runs for #455 and #456 (PA-19 to PA-24).

Run by a person, outside any cycle stage, with their own credentials. See
docs/native-parity-runs.md for the procedure and what to compare.

  run.py setup                      create the fixture repository and Compose file
  run.py toolkit                    toolkit side: table, consent, exact grant, hook
  run.py native claude|codex        native side: the agent runs each baseline directly
  run.py cleanup                    docker compose down for the fixture

PA-22 writes real issues to --fixture-repo. Use a private, disposable repository.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT = ("Run exactly this shell command in the current directory, nothing else, then reply "
          "with its exit code and its stdout/stderr verbatim: ")
PA19_COMMANDS = (
    "git log --oneline | head -5",
    'printf \'PA19_TARGET=%s\\n\' "$TMPDIR/pa19.txt"\n'
    'cat <<EOF > "$TMPDIR/pa19.txt"\nPA-19 fixture\nEOF',
)


def cases(args):
    """(row, directory, command) as the audit lists them."""
    return [
        *(("PA-19", "fx", command) for command in PA19_COMMANDS),
        ("PA-20", "fx", "git ls-tree HEAD"),
        ("PA-20", "fx", "git stash list"),
        ("PA-21", "fx", f"gh issue list --repo {args.read_repo} --state open --limit 3"),
        ("PA-22", "fx", f'gh issue create --repo {args.fixture_repo} --title "native-parity PA-22 {{executor}}" '
                        '--body "Native parity check. Safe to close."'),
        ("PA-23", "fx", "git -c core.pager=cat log -1"),
        ("PA-23", "fx", "GIT_PAGER=cat git log -1"),
        ("PA-24", "dk", "docker compose run --rm fixture true"),
    ]


def setup(args):
    work = Path(args.workdir)
    fx, dk = work / "fx", work / "dk"
    fx.mkdir(parents=True, exist_ok=True)
    dk.mkdir(parents=True, exist_ok=True)
    (fx / "tmp").mkdir(exist_ok=True)
    if not (fx / ".git").exists():
        for command in (["git", "init", "-q"],
                        ["git", "config", "user.email", "fixture@example.invalid"],
                        ["git", "config", "user.name", "fixture"]):
            subprocess.run(command, cwd=fx, check=True)
        (fx / "README.md").write_text("fixture\n")
        subprocess.run(["git", "add", "."], cwd=fx, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=fx, check=True)
    (dk / "compose.yaml").write_text('services:\n  fixture:\n    image: alpine:3\n    command: "true"\n')
    print(f"fixtures ready in {work}")


def toolkit(args):
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import command_permissions as cp
    import grants
    import stage_permissions as sp

    out = []
    for executor in ("claude", "codex"):
        for row, directory, command in cases(args):
            if args.rows and row not in args.rows:
                continue
            command = command.replace("{executor}", executor)
            cwd = str(Path(args.workdir) / directory)
            tmp = Path(tempfile.mkdtemp())
            policy = sp.StagePolicy("implement", tmpdir=str(Path(cwd) / "tmp") if row == "PA-19" else str(tmp), repo=args.read_repo,
                                    change_request_id="1", docker=True)
            record = {"executor": executor, "row": row, "command": command,
                      "table_allowed": sp.check(command, policy, cwd=cwd).allowed}
            broker = grants.ConsentBroker(args.read_repo, store=grants.ConsentStore(tmp / "consents.json"),
                                          platform="linux", cycle_id="cycle", task_id="manual")
            request = cp.refused_operation([command], policy, broker, executor, cwd)
            record["consent_offered"] = request is not None
            if request is not None:
                grant = broker.operations[request["resource"]]
                broker.apply_answer(grant, grant.scopes[0], "cycle")
                allowed = replace(policy, command_permissions=broker.command_entries(executor, "implement"))
                record["allowed_after_exact_grant"] = sp.check(command, allowed, cwd=cwd).allowed
                record["variant_refused"] = not sp.check(command + " --different", allowed, cwd=cwd).allowed
                policy_file = tmp / "policy.json"
                policy_file.write_text(allowed.to_json())
                hook = subprocess.run([sys.executable, sp.__file__, "hook", str(policy_file)], cwd=cwd,
                                      capture_output=True, text=True, timeout=60,
                                      input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command},
                                                        "cwd": cwd}))
                record["hook_allows_after_grant"] = hook.returncode == 0 and not hook.stdout
            out.append(record)
            print(row, executor, {k: v for k, v in record.items() if k not in ("row", "executor", "command")},
                  flush=True)
    Path(args.workdir, "toolkit.json").write_text(json.dumps(out, indent=1))


def native(args):
    executor = args.executor
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    out = []
    for row, directory, command in cases(args):
        if args.rows and row not in args.rows:
            continue
        command = command.replace("{executor}", executor)
        case_env = dict(env)
        if row == "PA-19":
            case_env["TMPDIR"] = str(Path(args.workdir).resolve() / directory / "tmp")
        if executor == "claude":
            argv = ["claude", "-p", PROMPT + command, "--model", args.claude_model,
                    "--allowedTools", "Bash", "--output-format", "json"]
        else:
            argv = ["codex", "exec", "-s", "workspace-write", "-c", "model_reasoning_effort=low",
                    "--skip-git-repo-check"]
            if row in ("PA-21", "PA-22"):
                argv += ["-c", "sandbox_workspace_write.network_access=true"]
            argv.append(PROMPT + command)
        started = time.time()
        try:
            run = subprocess.run(argv, cwd=Path(args.workdir) / directory, env=case_env, capture_output=True,
                                 text=True, timeout=300, stdin=subprocess.DEVNULL)
            text, code = run.stdout, run.returncode
            if executor == "claude":
                try:
                    data = json.loads(text)
                    text = f"{data.get('result', '')} | denials={json.dumps(data.get('permission_denials'))}"
                except ValueError:
                    pass
        except subprocess.TimeoutExpired:
            text, code = "", -1
        out.append({"executor": executor, "row": row, "command": command, "cli_rc": code,
                    "seconds": round(time.time() - started), "output": text.strip()[-900:]})
        print(row, command[:50], "cli_rc", code, flush=True)
    Path(args.workdir, f"native_{executor}.json").write_text(json.dumps(out, indent=1))


def cleanup(args):
    subprocess.run(["docker", "compose", "down"], cwd=Path(args.workdir) / "dk")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workdir", default=str(Path(tempfile.gettempdir()) / "native-parity"))
    parser.add_argument("--read-repo", default="datacas/code-cycle-toolkit",
                        help="repository PA-21 reads (owner/name)")
    parser.add_argument("--fixture-repo", help="disposable repository PA-22 writes to (owner/name)")
    parser.add_argument("--rows", type=lambda s: s.split(","), help="limit to these rows, e.g. PA-20,PA-23")
    parser.add_argument("--claude-model", default="claude-haiku-5-5")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup").set_defaults(func=setup)
    sub.add_parser("toolkit").set_defaults(func=toolkit)
    nat = sub.add_parser("native")
    nat.add_argument("executor", choices=("claude", "codex"))
    nat.set_defaults(func=native)
    sub.add_parser("cleanup").set_defaults(func=cleanup)
    args = parser.parse_args()
    if args.command in ("toolkit", "native") and not args.fixture_repo and not (args.rows and "PA-22" not in args.rows):
        parser.error("--fixture-repo is required unless --rows excludes PA-22")
    args.func(args)


if __name__ == "__main__":
    main()
