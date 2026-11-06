# Provider Contract

Code Cycle separates the system that owns the work item from the system that
hosts the repository and the code review. This is a workflow contract for the
skills, not an API client or a credential store.

## Terminology

- **Issue provider**: the system that owns the work item: GitHub Issues,
  Plane, or Jira.
- **Code host**: the system that owns the repository and the change request:
  GitHub or Bitbucket.
- **Work item**: the provider-neutral name for an issue or ticket.
- **Change request**: the provider-neutral name for a pull request or merge
  request. GitHub and Bitbucket both call this a pull request, but the skills
  use the neutral term in contracts.
- **Native identifier**: the identifier used by the provider, such as `#123`,
  `ENG-123`, or a Plane issue URL/UUID.

The supported combinations are independent:

| Issue provider | Code host | Typical use |
|---|---|---|
| GitHub | GitHub | GitHub issue and pull request in one place |
| GitHub | Bitbucket | GitHub planning with Bitbucket code review |
| Plane | GitHub | Plane planning with GitHub code review |
| Plane | Bitbucket | Plane planning with Bitbucket code review |
| Jira | GitHub | Jira planning with GitHub code review |
| Jira | Bitbucket | Jira planning with Bitbucket code review |

The skills may operate with another combination when the environment provides
authenticated tooling for both providers. They must not claim that a provider
operation succeeded merely because the local Git operation succeeded.

## Resolution precedence

Resolve the provider pair before reading or changing remote state:

1. Explicit invocation values: `issue_provider=...`, `code_host=...`, and the
   corresponding work-item or repository selector.
2. Repository-local `.code-cycle.yml`, when it exists.
3. The code host may be inferred from the `origin` remote when it clearly
   points to `github.com` or `bitbucket.org` and no ambiguity remains.
4. A GitHub issue may be inferred only when the code host is GitHub and the
   identifier is an unambiguous numeric issue number. Jira keys and Plane
   identifiers must not be guessed from their shape alone.

If either provider remains ambiguous, stop with `BLOCKED` before creating a
branch, commit, comment, change request, or external result artefact.

An optional repository-local configuration has this shape:

```yaml
code_cycle:
  issue_provider: plane
  code_host: bitbucket
  issue:
    project: ENG
    selector: ENG-123
  repository:
    selector: workspace/repository
    default_branch: main
  review:
    # Required for finding-history recovery; provider logins are case-insensitive.
    trusted_authors:
      - review-bot
```

This file may contain selectors and non-secret defaults only. Never put access
tokens, passwords, private keys, or client secrets in it.

`code_cycle.review.trusted_authors` is the complete allow-list for comments that
may create or advance recovered findings. Its default is no trusted authors:
when it is absent or empty, a cycle that needs history recovery returns
`BLOCKED`. Code-host adapters must supply the author from authenticated provider
metadata; missing metadata or contract headings published only by untrusted
authors also returns `BLOCKED`. Ordinary discussion without contract headings
starts an empty record. Compare provider login identities case-insensitively.

## Setting up each provider

The toolkit uses the tooling you already authenticated. It installs no MCP
server, creates no credentials, and changes no remote state during a check.

| Provider | Role | Tooling | `cc-provider-bootstrap` checks |
|---|---|---|---|
| GitHub | issue provider, code host | `gh` (or a configured GitHub connector) | authenticated, can read the repository, and can read the issue when GitHub is also the issue provider |
| Bitbucket | code host | a configured Bitbucket connector, CLI, or API tooling | can read the repository and its pull requests. GitHub access proves nothing here. |
| Plane | issue provider | a configured Plane MCP server or connector | authenticated, can read the workspace/project or work item |
| Jira | issue provider | a configured Jira connector, CLI, or API tooling | can read the project and work item. Plane or GitHub access proves nothing here. |

A shared GitHub connection can satisfy both the issue-provider and code-host
checks, but both scopes are recorded. A check without a selected project, work
item, or repository is account-level only and is never reported as scoped
access.

Successful checks are cached outside the repository, under
`~/.config/code-cycle-toolkit/provider-health/` (the application-data directory
on Windows), for `verification.cache_ttl` (default `7d`). The cache stores facts
such as provider, scope, status, time, and capabilities, never tokens or
response bodies. A later authentication, permission, or connectivity failure
invalidates that provider's entry and forces a recheck. A failed recheck
returns `BLOCKED` and names the missing connector, authentication, permission,
or scope.

## Provider responsibilities

An issue-provider adapter must expose, or explicitly report that it cannot
expose:

- read a work item, including title, body, status, labels, comments, links,
  and acceptance criteria;
- identify the canonical work-item URL and native identifier;
- add a progress or completion comment when the workflow authorises it;
- transition or label the work item only when the project workflow requires it;
- link the work item to a change request, without assuming that every provider
  supports automatic closure.

A code-host adapter must expose, or explicitly report that it cannot expose:

- repository metadata, default branch, current change-request head, and base;
- change-request title, body, commits, comments, reviews, threads, and state;
- publish one consolidated review comment and reply to individual threads;
- current CI/status checks and their real provider names;
- the change-request URL and native identifier.

Local Git remains responsible for fetching, diffing, branching, committing,
and inspecting the working tree. Remote provider state remains authoritative
for the work item, change request, comments, review threads, and checks.

### Runtime issue snapshot

`issue_review` does not publish, so its sandbox denies the GitHub CLI
credential store (`~/.config/gh`). For a GitHub work item, `run_cycle.py`
therefore reads the work item itself, with the runtime's own `gh` credentials,
before every issue-review dispatch (`scripts/issue_snapshot.py`), and gives
the stage the result as a read-only JSON file outside every writable root. The
stage reads that file instead of the provider; its credential denials are
unchanged, and no other stage receives the file. Plane and Jira have no
snapshot reader yet: their issue review keeps its normal tooling.

| Field | Meaning |
|---|---|
| `schema`, `provider`, `repository`, `issue_id` | Identity of the snapshot |
| `retrieved_at` | UTC time of the read; every dispatch reads again, and nothing is persisted, so a resumed cycle always judges a fresh read |
| `repository_private` | `true`, `false`, or `null` when the visibility could not be read |
| `completeness` | `complete`, `partial` (the issue or a referenced item is incomplete) or `unavailable` (the issue could not be read) |
| `issue_status`, `issue_reason` | The main issue's own state and why it is not `complete` |
| `issue` | Title, state, labels, author, body with its SHA-256, and every comment, read page by page and compared with the count GitHub reports |
| `linked` | Each issue or change request of the same repository the work item or its comments reference directly, one level deep, at most 20: `read` with title, state, labels and body, `unavailable` with the reason, or `not_read` beyond the bound |
| `unreadable_links` | The `#N` references in `linked` that were not read |
| `unavailable_reads` | Reads the stage cannot make itself: the related-work searches `gh issue list`, `gh pr list` and `gh search`, unless the project's `credential_reads` already exposes the gh store |

Only these fields are written: no token, header or raw provider response.
Every text field is data written by the work item's authors, never
instructions; the prompt carries the file path, its size and SHA-256 and the
summary fields, never the issue's text. A file changed during the stage ends
it as not completed.

The snapshot fails closed:

- a main issue that is not `complete` (a provider error, a missing comment
  page, fewer comments than GitHub reports, or more than 3000 comments) stops
  the cycle as `BLOCKED` with `readiness_unconfirmed` before the stage is
  dispatched, naming the missing data;
- an issue GitHub answers with 404 is reported as either absent or not visible
  to the runtime's credentials, never as proof that it does not exist; a
  missing runtime login is named as a login failure, distinct from the stage's
  credential-read policy;
- a `READY` must account for every entry of `unreadable_links` with an
  uncertainty that names it, material when the item could change the decision.
  A `READY` that does not is escalated once and then stops as
  `readiness_unconfirmed`; a relevant unreadable item ends in
  `NEEDS_REFINEMENT`;
- `unavailable_reads` is disclosed in the report and does not block `READY`
  on its own when the recovered context is enough to decide.

When no evidence location outside the writable roots exists, no file is
written and the stage is told to report `BLOCKED` naming the missing input
rather than judge without it.

## Provider-neutral result fields

Structured results use these fields for new work:

```json
{
  "issue_provider": "plane",
  "issue_id": "ENG-123",
  "issue_number": null,
  "code_host": "bitbucket",
  "repository": "workspace/repository",
  "change_request_id": "42",
  "change_request_url": "https://bitbucket.org/workspace/repository/pull-requests/42"
}
```

`issue_number` and `pr_number` remain compatibility aliases for existing
GitHub-oriented consumers. They may be `null` for Jira, Plane, or a host whose
identifier is not numeric. New orchestration logic must use `issue_id`,
`change_request_id`, `change_request_url`, `issue_provider`, and `code_host`.

Review findings use `native_thread_id` and `native_thread_provider` when a
provider exposes a thread. Older `github_thread_id` values must be preserved
while migrating existing GitHub reviews, but new findings must not require a
GitHub-specific field.

## Linking and publication rules

- Include the canonical work-item URL and identifier in the change-request
  description whenever the code host permits it.
- Use an automatic-closing keyword such as `Closes #123` only for GitHub
  Issues when the repository's workflow explicitly uses it.
- For Jira and Plane, use the native key or URL and, when supported, add the
  reciprocal provider link. Never claim automatic closure without observing
  the provider state.
- Publish one consolidated review comment on the configured code host. If
  line-level threads are unavailable, publish the finding at change-request
  level and retain the stable `REV-xxx` identifier.
- A failed provider publication, inaccessible review thread, or unavailable
  required check is `BLOCKED` or an explicitly degraded verification result;
  it is never silently treated as success.

---

[← Configuration](configuration.md) · [↑ Documentation index](README.md) · [Telemetry →](telemetry.md)
