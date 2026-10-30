"""CLI parsing and application wiring."""
from __future__ import annotations
import argparse
from typing import Any

def main(api: Any, argv: list[str] | None = None) -> int:
    api.apply_gh_environment_defaults()
    api._configure_stdout()
    parser: argparse.ArgumentParser = api.argparse.ArgumentParser(
        prog="run_cycle",
        description="Run one work item through a recorded cycle.",
    )
    parser.add_argument("--repo", default=None,
                        help=("repository identifier, owner/name; defaults to "
                              f"code_cycle.repository.selector in {api.CONFIG_NAME}"))
    parser.add_argument("--task", required=True, help="work item identifier")
    parser.add_argument(
        "--work-item-repository", default=None,
        help="provider repository or project when it differs from the configured default",
    )
    # Labelled before routing, never after: choosing a model from a judgement
    # and then measuring by model measures the routing rather than the models.
    parser.add_argument("--difficulty", type=int, choices=(1, 2, 3), default=2)
    parser.add_argument("--verifiability", choices=("auto", "partial", "human"),
                        default="auto")
    parser.add_argument("--security-sensitive", action="store_true")
    parser.add_argument("--verification", choices=("available", "unavailable"),
                        default=None,
                        help=("whether the change can be verified automatically; "
                              "recorded as a pre-routing signal, unknown when omitted"))
    parser.add_argument("--mode", choices=("production", "calibration"),
                        default="production")
    parser.add_argument("--from", dest="start_from", choices=api.CYCLE_STARTS,
                        default="implement",
                        help=("stage to start at; anything but implement resumes "
                              "the change request named by --pr (default: implement)"))
    parser.add_argument("--pr", dest="change_request", default=None,
                        help="existing change request a resumed cycle works on")
    parser.add_argument("--issue-review", choices=("auto", "off"), default=None,
                        help=("review the work item before implementing it: auto "
                              "reviews all but declared trivial, non-sensitive "
                              "work; off keeps the original flow (default: "
                              f"code_cycle.issue_review.mode in {api.CONFIG_NAME}, "
                              "else auto). A resumed cycle never runs it"))
    parser.add_argument("--continue", dest="continue_work", action="store_true",
                        help=("continue an interrupted implementation from the partial "
                              "work in --cwd instead of starting it again; skips the "
                              "issue review"))
    parser.add_argument("--max-iterations", type=int, default=3)
    parser.add_argument("--ci-policy", choices=api.CI_POLICIES, default=None,
                        help=("how the cycle treats the code host's checks: strict waits "
                              "for them in each writing stage; parallel reviews while "
                              "they run and approves only after every required check "
                              "of the reviewed SHA passed (default: "
                              f"code_cycle.ci.policy in {api.CONFIG_NAME}, else strict)"))
    parser.add_argument("--ci-timeout", type=int, default=None,
                        help=("seconds the parallel policy's CI gate waits at most for "
                              f"one SHA (default: code_cycle.ci.timeout, else "
                              f"{api.ci_gate.DEFAULT_TIMEOUT})"))
    parser.add_argument("--cwd", default=None,
                        help="working directory the executor runs in")
    parser.add_argument("--workspace", choices=api.WORKSPACE_MODES, default="task",
                        help="workspace policy: task worktree by default, or current checkout")
    parser.add_argument("--local-only", action="store_true",
                        help=("run only in the explicit --cwd linked worktree "
                              "with --workspace current; "
                              "stop after implementation without publishing"))
    parser.add_argument("--timeout", type=int, default=None,
                        help="seconds one dispatch may take")
    parser.add_argument("--detach", action="store_true",
                        help=("start the cycle in its own session and return at once, "
                              "so a host's task time limit cannot stop it; follow it "
                              "with cycle_status.py and read its report in the printed log"))
    parser.add_argument("--verbose", action="store_true",
                        help="show stage starts, live progress, and stage results")
    parser.add_argument("--progress-interval", type=float, default=60,
                        help="initial heartbeat seconds (default: 60; later intervals adapt)")
    parser.add_argument("--database", default=None,
                        help=f"telemetry database (default: {api.default_database_path()})")
    parser.add_argument("--config", default=None,
                        help=f"path to {api.CONFIG_NAME} (default: alongside the work, else the main worktree's)")
    parser.add_argument("--no-config", action="store_true",
                        help="run on the built-in defaults, ignoring any configuration")
    parser.add_argument("--grant", action="append", default=[], metavar="ID=ANSWER",
                        help="answer a sensitive sandbox permission in advance: "
                             "docker_host=cycle|persistent|deny (repeatable); persistent "
                             "and deny are stored only for a scope an earlier run asked about")
    parser.add_argument("--repair-approval", default=None,
                        help="approve exactly one proposed comment repair: COMMENT_ID:SHA256")
    parser.add_argument("--execution-profile", choices=api.EXECUTION_PROFILES, default=None,
                        help=("how deep the cycle goes (default: code_cycle.execution.profile "
                              f"in {api.CONFIG_NAME}, else standard); separate from the model "
                              "routing profiles"))
    parser.add_argument("--objective", choices=api.EXECUTION_OBJECTIVES, default=None,
                        help=("the outcome the cycle must reach (default: "
                              "code_cycle.execution.objective, else the profile's); "
                              "--local-only is local_change"))
    parser.add_argument("--stage", action="append", default=[], metavar="STAGE=POLICY",
                        help=("set one stage's policy: disabled, auto, enabled or "
                              "required (repeatable); an override never removes an "
                              "obligation"))
    parser.add_argument("--dry-run", action="store_true",
                        help=("print the execution plan and exit; runs no stage and "
                              "changes nothing"))
    parser.add_argument("--json", action="store_true",
                        help="with --dry-run, print the plan as its JSON contract")
    args = parser.parse_args(argv)
    if args.progress_interval <= 0:
        parser.error("--progress-interval must be greater than zero")
    if args.json and not args.dry_run:
        parser.error("--json needs --dry-run")
    if args.dry_run and args.detach:
        parser.error("--dry-run runs nothing to detach")

    try:
        api.validate_start(args.start_from, args.change_request, local_only=args.local_only,
                       continue_work=args.continue_work)
        if args.dry_run:
            planned = api.dry_run(args)
            print(api.json.dumps(planned.to_dict(), indent=2) if args.json
                  else api.render_plan(planned))
            return 0
        repo, profiles, routing_strategy = api.plan_with_strategy(args)
        config = api.resolve_config(args, report=print)
        execution = api.execution_of(args, config)
        api.execution_plan_of(args, config, repo, provenance="run")
        api.check_current_flow(execution)
        if execution.objective.value == "local_change" and not args.local_only:
            # The current flow reaches local_change only as a local-only run,
            # with the same explicit worktree it requires.
            args.local_only = True
            api.validate_start(args.start_from, args.change_request, local_only=True,
                           continue_work=args.continue_work)
        grant_answers = api.parse_grant_options(args.grant)
        if args.local_only:
            api.validate_local_only_cwd(args.cwd)
            if args.workspace != "current":
                raise api.CycleDriverError(
                    "--local-only requires --workspace current to use the "
                    "explicitly supplied linked worktree"
                )
        if args.continue_work and not args.cwd:
            raise api.CycleDriverError(
                "--continue needs --cwd: the checkout the interrupted run used"
            )
        ci_policy = api.ci_policy_of(config, args.ci_policy)
        ci_timeout = api.ci_timeout_of(config, args.ci_timeout)
        api.check_ci_policy(ci_policy, (config.get("code_cycle") or {}).get("code_host"))
        if args.start_from in api.RESUMES:
            host = (config.get("code_cycle") or {}).get("code_host")
            if host not in (None, "github"):
                raise api.CycleDriverError(
                    f"resuming reads the pull request through GitHub; code_host "
                    f"{host} is not supported")
        selected_cwd, worktree_dir = api.select_workspace(
            args.task, repo, config, workspace=args.workspace, cwd=args.cwd,
            change_request_id=(args.change_request if args.start_from in api.RESUMES else None),
            continue_work=args.continue_work,
        )
        if (args.local_only or args.continue_work) and (
                not args.cwd or api.Path(args.cwd).resolve() != api.Path(selected_cwd).resolve()):
            raise api.CycleDriverError(
                "BLOCKED: the explicitly supplied --cwd does not identify the selected "
                "task worktree. Pass its path with --cwd or use --workspace current."
            )
        args.cwd = selected_cwd
        if args.continue_work:
            api.check_continuation(args.cwd, config)
        if args.start_from in api.RESUMES:
            database = api.Path(args.database) if args.database else api.default_database_path()
            if database.is_file() and args.start_from == "resolve":
                api.check_change_request(repo, args.change_request, args.cwd,
                                     history_store=api.CycleHistoryStore(database))
            else:
                api.check_change_request(repo, args.change_request, args.cwd)
    except (api.CycleDriverError, api.GrantError) as error:
        parser.error(str(error))

    if args.detach:
        # Everything that can be refused was refused above, where a person is
        # still reading; the detached run repeats those checks on its own.
        return api.detach(list(api.sys.argv[1:] if argv is None else argv), args.database)

    change_bases = api.change_bases_of(config)
    # Already validated by `load_config`; read here so the run carries it.
    jev = api.load_jev_config(config)
    # `--issue-review`, `--stage issue_review=…` and the file's mode were layered
    # into one policy; the current flow honours auto and disabled.
    issue_review = (api.IssueReviewMode.OFF
                    if execution.stages["issue_review"].policy is api.PlanStagePolicy.DISABLED
                    else api.IssueReviewMode.AUTO)
    work_item_provider, work_item_repository = api._work_item_scope(
        config, repo, args.work_item_repository
    )
    code_host = (config.get("code_cycle") or {}).get("code_host")
    policy_code_host = api.code_host_of(config, args.cwd)
    # Every publishing stage is audited on `origin` (#261). Only GitHub can be
    # read for change-request state and protected branches until #238; any
    # other code host still has its refs audited and the rest is `unverified`.
    publication_auditor = (None if args.local_only or not args.cwd else api.PublicationAuditor(
        args.cwd, repo,
        api.GitHubHostReader(args.cwd) if (code_host or policy_code_host) == "github" else None,
        # Only a configured default branch; otherwise `origin`'s live HEAD.
        default_branch=api.base_branch_of(config, None)))
    # Asked on a terminal only; a run nobody watches never widens a sandbox
    # without an answer given in advance.
    consent = api.ConsentBroker(
        repo, store=api.ConsentStore(), answers=grant_answers,
        preauthorized=frozenset({"docker_host"} if api.docker_host_socket_access(config) else ()),
        prompt=(api.terminal_prompt() if api.sys.stdin.isatty() and api.sys.stderr.isatty() else None),
    )
    telemetry = api.Telemetry(api.Path(args.database) if args.database else None)
    try:
        with api.interruptible():
            report = api.run_cycle(
                repo, args.task,
                api.TaskSignals(difficulty=args.difficulty,
                            verifiability=args.verifiability,
                            security_sensitive=args.security_sensitive),
                telemetry,
                profiles=profiles,
                mode=api.RoutingMode[args.mode.upper()],
                routing_strategy=routing_strategy,
                max_iterations=args.max_iterations,
                cwd=args.cwd,
                timeout=args.timeout,
                verbose=args.verbose,
                progress_interval=args.progress_interval,
                local_only=args.local_only,
                change_bases=change_bases,
                verification_available=(
                    None if args.verification is None
                    else args.verification == "available"
                ),
                jev=jev,
                start_from=args.start_from,
                change_request_id=args.change_request,
                issue_review=issue_review,
                continue_work=args.continue_work,
                worktree_dir=worktree_dir,
                workspace=args.workspace,
                work_item_provider=work_item_provider,
                work_item_repository=work_item_repository,
                code_host=code_host,
                policy_code_host=policy_code_host,
                publication_auditor=publication_auditor,
                trusted_authors=tuple(((config.get("code_cycle") or {}).get("review") or {}).get(
                    "trusted_authors", [])),
                resolve_effort=((config.get("code_cycle") or {}).get("resolve") or {}).get("effort"),
                allowed_test_command=api.allowed_test_command(config),
                allowed_verify_commands=api.allowed_verify_commands(config),
                # None, not empty tuples, when no file was read: the permission
                # view infers a withdrawal only from a source it could read (#314).
                user_tool_choices=(api.user_choices.load(config) if api.configuration_read(args)
                                   else None),
                base_branch=api.base_branch_of(config, args.cwd),
                ci_policy=ci_policy,
                ci_timeout=ci_timeout,
                repair_approval=args.repair_approval,
                consent=consent,
            )
    except (api.CycleDriverError, api.GrantError) as error:
        parser.error(str(error))
    except api.CycleInterrupted as interruption:
        # Before the recorder existed, or after it closed: nothing is running
        # and there is no stage to record.
        print(f"interrupted by {interruption.signal_name} before any stage ran",
              file=api.sys.stderr)
        return 1

    if consent.notes:
        report.notes[:0] = [(0, f"  permission {api.readable(line, 300)}")
                            for line in consent.notes]
    api.save_decisions(report, api.Path(telemetry.path).parent / "decisions")
    print(report.explain())
    print(f"recorded in {telemetry.path}")
    return 0 if report.status == api.APPROVED_END else 1
