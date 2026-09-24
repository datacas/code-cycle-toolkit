"""Run one stage against an installed runtime and prove it left a row.

The failure this exists for is not a broken function: it is a runtime that was
never installed at all. Every other test in this repository imports from
`scripts/`, which is exactly the checkout a real installation does not have, so
none of them can tell whether an installation records anything.

So this one refuses to import from the repository. It is handed the installed
runtime directory, puts only that on `sys.path`, and drives a full stage through
it with a scripted executor — no provider, no network, no credential. If the
installers ever stop carrying a module, or carry one whose import is missing,
this stops at the import instead of months later when the rows are absent.

Usage: python3 tests/installed_stage_check.py <runtime-dir> <database-path>
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2

    runtime, database = Path(argv[1]).resolve(), Path(argv[2]).resolve()
    if not runtime.is_dir():
        print(f"installed runtime not found: {runtime}", file=sys.stderr)
        return 1

    # Only the installed directory. A repository checkout leaking onto the path
    # would let this pass while the installation is incomplete.
    sys.path[:] = [str(runtime)] + [p for p in sys.path if p and not _is_repo(p)]

    import cycle as cy
    import cycle_status as cs
    import executors as ex
    import router
    import telemetry as tm

    class Scripted(ex.Adapter):
        """An executor that runs nothing and reports success."""

        provable_ceiling = ex.Availability.AUTHENTICATED

        def __init__(self, name: str) -> None:
            self.name = name

        def probe(self):
            return ex.ProbeResult(self.name, ex.Availability.AUTHENTICATED,
                                  "scripted", provable_ceiling=self.provable_ceiling)

        def publication_access(self, probe, *, writes):
            return True, "scripted adapter permission contract"

        def dispatch(self, target, task, **kw):
            return ex.DispatchResult(
                ex.DispatchOutcome.SUCCEEDED, self.name, target,
                model_resolved=target.model,
                readiness_policy=ex.ReadinessPolicy.ATTEMPT,
                dispatched_from=ex.Availability.AUTHENTICATED,
            )

    store = tm.Telemetry(database)
    recorder = cy.CycleRecorder(
        store, "owner/example", "INSTALL-1",
        router.TaskSignals(difficulty=2, verifiability="auto"),
        availability={"codex": ex.Availability.READY, "claude": ex.Availability.READY},
        registry=ex.Registry([Scripted("codex"), Scripted("claude")]),
    )

    outcome = recorder.stage("implement", "a task that runs nowhere")
    status = cs.CycleStatusWriter(database, "cycle-installed-check",
                                  "owner/example", "INSTALL-1")
    status.start()
    status.stage_started("implement", outcome.decision)
    status.activity(text="installed runtime status")
    status.stage_finished(outcome.result, "IMPLEMENTED")
    status.finish("READY_FOR_MANUAL_MERGE")
    recorder.record_verdict("review", "APPROVED", findings_total=0)
    recorder.close("READY_FOR_MANUAL_MERGE")

    if not outcome.succeeded:
        print(f"the stage did not run: {outcome.result}", file=sys.stderr)
        return 1

    rows = store.rows("owner/example")
    if len(rows) != 3:
        print(f"expected three rows from the installed runtime, got {len(rows)}",
              file=sys.stderr)
        return 1
    if rows[0]["executor"] != "codex" or rows[0]["outcome"] != "succeeded":
        print(f"the dispatch row is wrong: {rows[0]}", file=sys.stderr)
        return 1
    if not status.path.is_file():
        print("the installed runtime did not write a cycle status file", file=sys.stderr)
        return 1

    print(f"installed runtime recorded {len(rows)} rows from {runtime}")
    return 0


def _is_repo(entry: str) -> bool:
    """True for a path that would give this script the repository's own copy."""
    try:
        resolved = Path(entry).resolve()
    except OSError:  # pragma: no cover - unreadable path entries
        return False
    return (resolved / "cycle.py").is_file() or (resolved / "scripts" / "cycle.py").is_file()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
