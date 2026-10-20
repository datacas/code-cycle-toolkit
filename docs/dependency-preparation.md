# Dependency preparation and project tool choices

Starting a cycle authorizes routine frozen Node dependency installation before
native implementation, resolution, review and rereview. No per-project setup
configuration is needed. Python environment preparation is outside this change.

The runtime reads the lockfile and `packageManager` declaration, and installs
with lifecycle scripts disabled:

| Lockfile | Command |
| --- | --- |
| `pnpm-lock.yaml` | `pnpm install --frozen-lockfile --ignore-scripts --store-dir <cache> --package-import-method=copy` |
| `package-lock.json` or `npm-shrinkwrap.json` | `npm ci --ignore-scripts --no-audit --no-fund --cache <cache>` |
| `yarn.lock`, Yarn 1 | `yarn install --frozen-lockfile --ignore-scripts --cache-folder <cache>` |
| `yarn.lock`, Yarn 2+ | `YARN_ENABLE_SCRIPTS=false yarn install --immutable` |

Conflicting managers or a mismatched declared version are reported rather than
silently installing with another manager. Workspace package manifests determine
the package-local dependency directories, including `apps/web/node_modules`.
Independent nested lock roots are prepared separately. PnP outputs are supported.

Preparation runs through `codex sandbox` for Codex, and the installed `srt`
(Anthropic sandbox runtime) for Claude. Neither path falls back to a host install.
The install runs in a scratch copy of the lock root's tracked files. The sandbox
can write only that scratch directory and the download cache; the lockfile and
manifests stay read-only there too. The checkout and its Git metadata are never
writable. Codex mounts protected placeholders (`.agents`, `.codex`, `.git`) inside
every writable root, and `npm ci` cannot empty a `node_modules` that holds one.
After a successful frozen install, the host moves each package's `node_modules`
into the checkout, replacing the previous one, and does the same for untracked
Yarn outputs (`.pnp.cjs`, `.pnp.loader.mjs`, untracked `.yarn` entries). A failed
install leaves the checkout's outputs untouched. The sandbox permits the npm/Yarn registry
hosts plus explicitly declared private registry hosts. Credentials are not copied
into scratch and credential environment variables are not inherited. Registry
authentication that is not available in this environment is a concrete setup
failure, not a successful install or a model failure.

The host must supply the selected native sandbox and package manager. On Linux,
`srt` refuses to start without `rg` (ripgrep) on the `PATH`. Missing
binaries and sandbox startup failures are recorded. The runtime tries transient
failures/timeouts at most twice, reaps timed-out process groups before retrying,
and continues the stage with the setup evidence when preparation is exhausted.
The agent can continue source inspection and read CI for the exact HEAD. Pending
CI and unexecuted local checks remain pending/unverified.

Lifecycle scripts and native `binding.gyp` requirements produce
`scripts_disabled` / `incomplete` evidence with the affected manifests and an
isolated recovery instruction. This is deliberately not a claim that disabled
builds succeeded. Required builds must run within the same contained preparation
boundary, with their generated outputs authorized, before local verification can
be claimed. An install exit of zero without required dependency outputs also
remains incomplete.

Each preparation writes `report.json`, command arguments, stdout/stderr log
paths, exit statuses and the exact HEAD under
`~/.local/state/code-cycle-toolkit/preparation/`. These files outlive disposable
review clones. Download caches are keyed by lockfile/manifests, manager version
and platform; pnpm copies files so editing an installed dependency cannot alter
the shared store. Verified installs are reused only in the same checkout and
HEAD, after checking every package-local output and the installed tree digest.
Fresh clones always build their own dependency tree.

## Explicit user choices

Declare effective choices in the source checkout's `.code-cycle.yml`:

```yaml
code_cycle:
  user_choices:
    tools: [rg, bespoke-search]
    commands: [sha256sum, "company-tool inspect"]
    integrations: [mcp__company__query]
    registry_domains: [packages.example.org]
```

`tools` names executables; `commands` names simple command prefixes;
`integrations` names MCP tools/server patterns; `registry_domains` names exact
package-registry hosts, never a wildcard/general network grant. The toolkit
does not maintain a list of approved executable names for these choices.
Choices enter each stage's serialized policy and are retained for retries;
resumes read the same project configuration. The review clone cannot replace
the effective source policy with PR-controlled configuration.

Claude receives both the allowed-tool patterns and the command hook policy.
Codex retains normal tools/MCP access and receives the matching explicit network
profile. Default code-host domains are granted only to publishing stages;
non-publishing stages, including issue review and local-only writers, receive
only explicitly selected registry domains. Issue review must use host-supplied
issue context; missing context requires host-side reads, not a worker route
with write-capable credentials. Registry choices alone do not select a publication
profile.

The command hook checks direct Git, code-host and Docker invocations before
applying choices. Selecting an interpreter, wrapper or arbitrary-code runner
(such as `python3`, `bash`, `env`, `xargs` or `npx`), a command prefix that runs
code, or an MCP integration trusts everything that tool can execute. The hook
does not inspect nested commands or tool implementations: those selections can
bypass the command publication boundary, including force-push and merge guards.
The user must trust the selected tool and scope its own credentials and access.
Stages remain instructed to obey their publication contract, and OS sandbox
restrictions still apply. Actual command execution can still fail.

For an unselected/unavailable preferred search executable, an authorized source
read can use `git grep` or `grep`. A forbidden operation cannot use another tool
to bypass its boundary. Use native Write/Edit for multiline temporary bodies,
then `--body-file` in the current dispatch's TMPDIR (including Claude's owned
subdirectory); sibling and expired dispatch paths remain forbidden.

## Validation campaign

Automated regressions exercise real disposable clones and subprocess fixtures
for both native wrapper formats and both review roles. These fixtures establish
process and evidence contracts; they do not establish actual OS confinement.

Run the opt-in campaign from an authorized host executor:

```console
python3 -m unittest tests.native_dependency_campaign
```

It creates a clean npm workspace with an uncached pinned dependency, records its
HEAD, invokes each actual native preparation sandbox, and runs dependency lookup
inside that same sandbox. Preserve its report/log evidence and exit statuses.
Do not replace native failures with the subprocess fixture's passing result.
When invoked inside a Codex stage, the nested Codex app-server socket may be
masked; report that observed restriction once and use the cycle's configured
alternative executor rather than retrying sandbox variants.

The native campaign is a required release-validation item for #279. Its result
must be attached to the PR; the ordinary unit suite does not imply it ran.
