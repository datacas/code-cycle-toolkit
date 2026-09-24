# Documentation

[← Back to the project README](../README.md)

Guides are listed in reading order. Each one has one main topic and links to the others instead of repeating them.

## Learn and use

| # | Guide | Read it when you want to… |
|---|---|---|
| 1 | [Getting started](getting-started.md) | install the toolkit, authenticate providers, and run a first cycle |
| 2 | [Workflows](workflows.md) | choose between single skills, the manual cycle, the orchestrators, and the runtime driver |
| 3 | [Skills reference](skills.md) | know what each of the thirteen skills takes, does, and returns |
| 4 | [Review lifecycle](review-cycle.md) | understand `REV-xxx` findings, severities, statuses, dispositions, and run lines |
| 5 | [Verification](verification.md) | know what counts as evidence and when a stage stops to ask |

## Configure and operate

| # | Guide | Read it when you want to… |
|---|---|---|
| 6 | [Routing and models](routing.md) | change models, executors, efforts, or fallbacks, or try Jev or a calibration |
| 7 | [Configuration](configuration.md) | look up every `.code-cycle.yml` key, invocation parameter, CLI flag, and environment variable |
| 8 | [Providers](provider-contract.md) | combine GitHub, Plane, or Jira with GitHub or Bitbucket |
| 9 | [Telemetry and `cc-stats`](telemetry.md) | know what is recorded locally and read the report |

## Reference and design

| Document | Contents |
|---|---|
| [Role workspace policy](role-workspace-policy.md) | The write permission each dispatched role has, and which executors can enforce it |
| [Instrumentation internals](instrumentation.md) | Design reference: the comment record, paired review, routing v1, executor dispatch, telemetry schema |
| [Codex plugin adapter contract](../skills/cc-orchestrator/references/codex-plugin-cc.md) | Discovery, handoff, and failure rules for Claude + Codex mode |
| [Plan de instrumentación y routing](Plan%20de%20instrumentaci%C3%B3n%20y%20routing.md) | Historical design plan, in Spanish. Superseded by the documents above where they differ. |

## Project

- [CHANGELOG](../CHANGELOG.md): released and unreleased changes
- [CONTRIBUTING](../CONTRIBUTING.md): authoring rules, validator, release checklist
- [SECURITY](../SECURITY.md): how to report a vulnerability

---

[↑ Project README](../README.md) · [Getting started →](getting-started.md)
