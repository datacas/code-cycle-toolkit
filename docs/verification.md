# Verification

The toolkit counts something as verified only after it has run it. This page explains what that means for each stage, when a stage stops instead of continuing, and how checks are reported.

## The rules

1. **Execution is the evidence.** A fix read in the diff is *present*, not *verified*. `cc-verify` reports what ran, what passed, what failed, and what couldn't be verified, and why.
2. **Unverified stays unverified.** It is reported with its concrete reason and carried into the verdict and residual risk. "Environment unavailable" with no cause isn't an acceptable reason.
3. **Nothing is invented.** Commands come from what the project defines (lockfiles, manifests, task runners, CI). If a command doesn't exist, it isn't run.
4. **Verification doesn't edit code** unless you ask.
5. **A pass that couldn't run isn't a passed check.** A missing delegated skill is reported as a degraded pass.

## What each stage verifies

| Stage | Verifies |
|---|---|
| `cc-implement-issue` | The narrowest related tests first, then the repository's required verification through `cc-verify` |
| `cc-initial-review` | Reproduces actionable findings when the environment allows it. Findings are marked as observed at runtime or found by code inspection. |
| `cc-resolve-comments` | Reproduces each original finding, applies the fix, and confirms it no longer reproduces. Then `cc-verify` over the fixes and related regressions. |
| `cc-rereview` | Reproduces every finding claimed as fixed. Only those it could verify become `resolved`. |

Depth follows the change:

- **Authorization:** both the allowed and the denied case.
- **API contracts:** status, payload, relevant headers, and error responses.
- **Frontend:** the affected flow in a browser, with console and network checks, desktop and reduced viewports, and loading, empty, error, disabled, and success states.
- **Persisted data:** migration state is checked before tests run.

## Stop conditions

`cc-verify` and `cc-run` **stop and ask** instead of marking something "not verified" and continuing when:

- runtime configuration is missing (for example, only an `.env.example` exists);
- a required database or service is unavailable;
- a needed port is held by an unknown process;
- migrations are pending that could change the result;
- permission to run a needed command is missing.

When one of these could be cleared by creating something (a config from its template, a local database, a results directory), the skill describes exactly what it would create and asks first.

Before invoking `cc-verify`, the cycle skills **preflight** these conditions without changing state. If preflight fails, verification isn't attempted, and the reason is recorded. In a resolution thread that reads, for example:

```text
Fix applied. Verification pending: environment unavailable (PostgreSQL on :5432 not running).
```

## Where verification runs

Stages that only verify or start services write their artefacts to a **disposable workspace**, never to the source tree. Reviews are read-only. See [Role workspace policy](role-workspace-policy.md).

`cc-run` starts services in dependency order and reports each one's process, PID, port, URL, and stop command. It never picks another port silently and never kills a process it didn't start.

## CI reporting

Every review reports the checks the repository actually defines, by their real names, and every state that isn't a pass: pending, failed, cancelled, skipped, and **missing**. A check the repository normally runs that didn't run on this PR is a finding, not a pass. When a repository policy skips a suite (for example, end-to-end tests on draft PRs), the review states it. `skipped` is never reported as `passed`.

For GitHub the reviewers use:

```bash
gh pr view <n> --json isDraft,headRefName,baseRefName,labels,statusCheckRollup
gh pr checks <n>          # output kept even on non-zero exit: it is evidence
```

## In telemetry

When the runtime drives the cycle, a verdict row records `tests_passed` only when a stage actually ran tests. A stage that stopped before running tests (`"tests": {"ran": false}`, or a `BLOCKED` result without `ran: true`) records no test outcome, rather than a failure. CI state stays in the PR comment and isn't copied into telemetry. See [Telemetry](telemetry.md).

---

[← Review lifecycle](review-cycle.md) · [↑ Documentation index](README.md) · [Routing and models →](routing.md)
