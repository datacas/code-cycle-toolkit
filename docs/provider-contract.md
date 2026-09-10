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
```

This file may contain selectors and non-secret defaults only. Never put access
tokens, passwords, private keys, or client secrets in it.

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
