"""Task-worktree selection and setup ownership."""
from __future__ import annotations
from typing import TYPE_CHECKING, Any
from pathlib import Path


if TYPE_CHECKING:
    from .workspace_setup import current_setup
else:
    from workspace_setup import current_setup

def _ref(api, root, name, run):
    result = api._selection_result(
        ["git", "rev-parse", "--verify", "--quiet", name], root, run)
    return result.stdout.strip() if result.returncode == 0 else None


def _own_ref(api, root, name, run, *, fetched=False):
    """Delete only an unchanged ref created by this setup and now unused."""
    setup = current_setup()
    sha = _ref(api, root, name, run)
    if setup is None or sha is None:
        return

    def compensate():
        if _ref(api, root, name, run) != sha:
            raise RuntimeError("ref changed since setup; retained")
        if fetched:
            users = api._selection_text(
                ["git", "for-each-ref", f"--contains={sha}", "--format=%(refname)", "refs/heads/"],
                root, "check fetched-ref users", run)
            worktrees = api._worktree_records(root, run)
            detached_users = [item for item in worktrees if not item["branch"] and
                              api._selection_result(
                                  ["git", "merge-base", "--is-ancestor", sha, "HEAD"],
                                  Path(item["path"]), run).returncode == 0]
            if users or detached_users:
                raise RuntimeError("fetched ref is in use; retained")
        else:
            branch = name.removeprefix("refs/heads/")
            if any(item["branch"] == branch for item in api._worktree_records(root, run)):
                raise RuntimeError("branch is in use by a worktree; retained")
        api._selection_text(["git", "update-ref", "-d", name, sha], root,
                            "roll back created ref", run)

    setup.record("fetched ref" if fetched else "branch", name,
                 "GitHub PR fetch" if fetched else "task worktree setup", compensate)


def _own_worktree(api, root, target, run, herdr_active):
    setup = current_setup()
    registered = any(Path(item["path"]) == target for item in api._worktree_records(root, run))
    if setup is not None and registered:
        setup.record("worktree", target, "Herdr" if herdr_active else "git worktree add",
                     lambda: api._selection_text(
                         ["git", "worktree", "remove", str(target)], root,
                         "roll back created worktree", run))

def select_workspace(api: Any, work_item_id: str, repo_id: str, config: dict[str, Any], *,
                     workspace: str = "task", cwd: str | None = None,
                     change_request_id: str | None = None,
                     continue_work: bool = False, run=None) -> tuple[str, Path | None]:
    """Select one task worktree before any cycle stage can mutate the checkout."""
    run = run or api.subprocess.run
    current = api.Path(cwd or api.Path.cwd()).expanduser().resolve()
    if not current.is_dir():
        raise api.CycleDriverError(f"BLOCKED: workspace does not exist: {current}")
    if workspace not in api.WORKSPACE_MODES:
        raise api.CycleDriverError(f"unknown workspace mode: {workspace}")
    if workspace == "current":
        return str(current), None
    root_text = api._selection_text(
        ["git", "rev-parse", "--show-toplevel"], current,
        "locate the Git repository", run,
    )
    repository_root = api.Path(root_text).resolve()
    main_root = api._main_repository_root(repository_root, run)
    herdr_active = api.os.environ.get("HERDR_ENV") == "1"
    requested_branch = (
        api._pull_request_branch(repo_id, change_request_id, work_item_id, repository_root, run)
        if change_request_id else None
    )
    records = api._worktree_records(main_root, run)
    matches = [
        api.Path(item["path"]) for item in records
        if item["branch"] and api._is_linked_worktree(api.Path(item["path"]))
        and ((item["branch"] == requested_branch) if requested_branch
             else api.branch_identifies_work_item(item["branch"], work_item_id))
    ]
    if len(matches) > 1:
        names = ", ".join(str(path) for path in matches)
        raise api.CycleDriverError(
            f"BLOCKED: more than one linked worktree matches work item {work_item_id}: "
            f"{names}. Pass --cwd for the intended worktree or --workspace current."
        )
    if matches:
        selected = matches[0]
        if herdr_active and selected != repository_root:
            api._selection_text(
                ["herdr", "worktree", "open", "--cwd", str(main_root),
                 "--path", str(selected), "--no-focus"],
                main_root, f"open linked worktree {selected} in Herdr", run,
            )
        # Resolve the configured base from the main root even when reusing a
        # linked checkout, so worker worktrees are not nested inside it.
        return str(selected), api.resolve_worktree_dir(config, main_root)
    if continue_work:
        raise api.CycleDriverError(
            "BLOCKED: no active linked worktree matches the interrupted task. "
            "Provide its path with --cwd, or explicitly opt into the invoking "
            "checkout with --workspace current."
        )

    worktree_dir = api.resolve_worktree_dir(config, main_root)
    slug = api.re.sub(r"[^A-Za-z0-9._-]+", "-", str(work_item_id).strip()).strip(".-")
    if not slug:
        raise api.CycleDriverError(f"BLOCKED: work item {work_item_id!r} has no safe worktree name")
    branch = f"task-{slug}"
    target = (worktree_dir / branch).resolve()
    try:
        target.relative_to(worktree_dir.resolve())
    except ValueError as error:
        raise api.CycleDriverError("BLOCKED: task worktree path escapes its configured base") from error
    if target.exists():
        raise api.CycleDriverError(
            f"BLOCKED: task worktree path already exists but is not an active matching "
            f"worktree: {target}. Inspect it, or opt into the current checkout with "
            "--workspace current."
        )
    try:
        missing = []
        parent = worktree_dir
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        worktree_dir.mkdir(parents=True, exist_ok=True)
        setup = current_setup()
        if setup is not None:
            for directory in reversed(missing):
                setup.record("directory", directory, "worktree base", directory.rmdir)
    except OSError as error:
        raise api.CycleDriverError(
            f"BLOCKED: cannot create worktree base {worktree_dir}: {error}. "
            "Use --workspace current to opt into the invoking checkout."
        ) from error
    branches = set(api._selection_text(
        ["git", "branch", "--format=%(refname:short)"], main_root,
        "inspect local branches", run,
    ).splitlines())
    base_ref = None
    create_branch = False
    if requested_branch:
        branch = requested_branch
        if branch not in branches:
            number = (change_request_id or "").lstrip("#")
            if not api.re.fullmatch(r"[1-9][0-9]*", number):
                raise api.CycleDriverError(
                    f"BLOCKED: cannot fetch pull request branch {branch} without a numeric "
                    "GitHub change-request ID. Provide its linked worktree with --cwd."
                )
            fetched_ref = f"refs/code-cycle/pulls/{number}/head"
            existed = _ref(api, main_root, fetched_ref, run) is not None
            try:
                api._selection_text(
                    ["git", "fetch", "origin",
                     f"+refs/pull/{number}/head:{fetched_ref}"],
                    main_root, f"fetch pull request {number}'s head", run,
                )
            finally:
                if not existed:
                    _own_ref(api, main_root, fetched_ref, run, fetched=True)
            base_ref = fetched_ref
            create_branch = True
    elif branch not in branches:
        base_ref = api._default_base_ref(main_root, config, run)
        create_branch = True

    if herdr_active:
        command = [
            "herdr", "worktree", "create", "--cwd", str(main_root),
            "--branch", branch, "--path", str(target), "--no-focus",
        ]
        if base_ref:
            command.extend(["--base", base_ref])
    else:
        command = ["git", "worktree", "add"]
        if create_branch:
            command.extend(["-b", branch])
        command.extend([str(target), base_ref or branch])
    try:
        api._selection_text(command, main_root, f"create linked worktree {target}", run)
    finally:
        # A manager may create Git resources and then fail. Record only the
        # identities absent in our pre-setup snapshot, including on that path.
        if branch not in branches:
            _own_ref(api, main_root, f"refs/heads/{branch}", run)
        _own_worktree(api, main_root, target, run, herdr_active)
    if not api._is_linked_worktree(target):
        raise api.CycleDriverError(
            f"BLOCKED: worktree manager did not create a linked worktree at {target}. "
            "Use --workspace current only if you explicitly accept editing the invoking checkout."
        )
    return str(target), worktree_dir
