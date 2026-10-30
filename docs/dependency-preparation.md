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

On Linux/WSL2 preparation runs through the installed `srt` (Anthropic sandbox
runtime) for both executors. There is no Linux Codex fallback. Until the `srt`
path is validated on macOS and Windows, Codex retains `codex sandbox` there,
with the same credential denials. Neither path falls back to a host install.
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

The host supplies the native executor and package manager. Before native stage
dispatch, Linux/WSL2 recovery reuses working `srt` and `rg` binaries. Missing or
unusable tools are installed in immutable bundles below
`~/.local/lib/code-cycle-tools/`; both executors require `srt` for Linux preparation.
The stage, host isolation probe and preparation process receive their
managed executable directories prepended to their own `PATH`, preserving the
original host directories in their existing order. Tools already selected from
the host `PATH` do not promote their directories. This does not change the runtime's
environment or edit shell profiles. Preparation still omits host credentials.
Existing managed bundles
are reused without resolving a newer version. The bundle's `receipt.json`
records version, source and integrity; stage observations also enter the existing
cycle journal, rather than a new telemetry store. Observations use the existing
`stage_end` kind with component `sandbox_preflight`: one summary row, one row
per tool and one per functional check, correlated by stage and attempt. Each
row keeps its probe identity, observed path, outcome and bounded, redacted
diagnostic separately, so a long diagnostic cannot truncate later probes.
Journal summaries keep one summary row for successful preflight and show
individual probes only for other outcomes. When space is limited, preflight
lines are omitted before stage results, stops, failures and refusals; the full
per-probe rows remain queryable. Preflight labels keep these observations
distinct from the stage's actual end.

`srt` comes only from the official `@anthropic-ai/sandbox-runtime` npm package.
Recovery selects the newest stable published version whose Node engine range
matches the host, checks npm SHA-512 integrity before installation, and invokes
npm with `--ignore-scripts` and `--engine-strict`. No project scripts or project
npm configuration participate. Missing Node/npm, unsupported engine syntax,
integrity errors and failed installs leave the check unverified. `rg` comes
from the official ripgrep GitHub release for Linux x86_64 or aarch64, with its
published SHA-256 checksum checked before extracting the regular executable.
Both tools must run before their scratch bundle is atomically renamed into
place. Recovery uses no `sudo`, never auto-updates, and never falls back to an
unconfined dependency install. A failed replacement leaves existing bundles
untouched. Concurrent installers reuse the winning committed bundle after
checking its receipt and executable; other publication errors stay unverified.

The [upstream srt documentation](https://github.com/anthropics/sandbox-runtime#how-it-works)
describes macOS Seatbelt, Linux bubblewrap and Windows isolation. Automatic
recovery here is validated only on Linux/WSL2; other platforms keep their
existing native sandbox behavior. An unvalidated platform by itself does not
make a concrete check fail or become unverified.

Presence alone does not prove isolation. Claude stages needing confined Bash
verification run a fixed host-owned probe of allowed writes, denied reads and
writes, and denied local network access. Preparation separately probes `srt`.
These probes run no repository code. If the required Bash boundary cannot
start, the command hook removes verification and arbitrary-code grants; the
stage continues source work with checks recorded as `not_verified`. Reviews
retain the disposable clone and publication guards and cover static findings
only. Missing `bwrap`/`socat` need host installation; the runtime never installs
system packages. An unavailable preparation boundary skips preparation and
carries the cause into the stage. The runtime tries transient
failures/timeouts at most twice, reaps timed-out process groups before retrying,
and continues the stage with the setup evidence when preparation is exhausted.
The agent can continue source inspection and read CI for the exact HEAD. Pending
CI and unexecuted local checks remain pending/unverified.

An install exit of zero without required dependency outputs remains incomplete.

### Lifecycle builds

After the scripts-disabled install, preparation looks for lifecycle scripts
(`preinstall`, `install`, `postinstall`, `prepare`) and native `binding.gyp`
builds. It checks the workspace manifests and the installed packages, and lists
the affected manifests as `needs_build`. Only then does it run a contained
lifecycle build: the same frozen install without `--ignore-scripts`
(`YARN_ENABLE_SCRIPTS=true` for Yarn 2+). It runs in a second scratch copy of the
tracked files, through the same native sandbox, with the same registry-only
network and reduced environment. The package manager's own build policy still
applies, such as pnpm's `onlyBuiltDependencies` or Yarn's `dependenciesMeta`.

Project scripts can write only three places: the build copy, its own `HOME`, and
a private copy of the download cache. They cannot write the checkout, the shared
cache or the scripts-disabled tree, and the lockfile and manifests stay
read-only. The host then checks those inputs. A build that changed them, failed,
timed out after its bounded retry, or produced no required dependency output is
recorded as `lifecycle_build.status: failed`. In that case the scripts-disabled
install is published and the item stays `scripts_disabled` / `incomplete`, with
the build's logs as evidence. A failed build is never reported as prepared, and
it never falls back to the host.

After a successful build (`lifecycle_build.status: built`), the host moves the
built dependency outputs into the checkout. It also moves any new entries the
build created beside a package manifest, such as a Nuxt `.nuxt` directory, but
only those the checkout's own Git ignore rules ignore, and never tracked paths,
links, `.git`, sandbox placeholders or configuration that host tools load
outside the sandbox (such as `.envrc`, `.vscode`, `.idea`, `.claude`,
`.mcp.json`, package-manager configuration such as `.npmrc` or `.pnpmfile.cjs`,
or shell profiles). These are listed in
`lifecycle_build.generated`. Ignore rules are read from the read-only checkout,
never from the build copy. A new untracked entry that is not ignored stays out.
The build copy holds no untracked checkout content, so an existing ignored
entry, such as a developer's `.env` or `dist/`, was not created by the build.
It is replaced only when an earlier preparation of the same checkout published
it and its content is unchanged since; otherwise it is kept and listed in
`lifecycle_build.preserved`. The content digests of published entries are kept
beside the receipts under the preparation state directory.
Native builds that download something other than registry packages, such as
prebuilt binaries from another host or Node headers, fail inside the sandbox.
They then stay incomplete, rather than gaining wider network access.

Each preparation writes `report.json`, command arguments, stdout/stderr log
paths, exit statuses and the exact HEAD under
`~/.local/state/code-cycle-toolkit/preparation/`. These files outlive disposable
review clones. Download caches are keyed by lockfile/manifests, manager version
and platform; pnpm copies files so editing an installed dependency cannot alter
the shared store. Verified installs are reused only in the same checkout and
HEAD, after checking every package-local output, the installed tree digest and
any published lifecycle-build outputs.
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
profile. Default code-host domains are granted to publishing stages. Per the
owner's #279 decision, Codex issue review also receives `github.com` and
`api.github.com`. Under #294 its sandboxed commands need the explicit
`credential_reads: ["~/.config/gh"]` exception to use stored gh credentials;
its workspace and publication contract remain read-only. The domain grant itself
does not make an HTTP credential read-only. Other
non-publishing roles receive only registry access, without a publication profile.

### Credential-read exceptions

Every Codex stage sandbox, existing Claude Bash sandbox and preparation sandbox
denies reads of `~/.ssh`, `~/.aws`,
`~/.codex/auth.json`, `~/.claude/.credentials.json` and `~/.claude.json`.
`~/.config/gh` is readable by publishing stages only; preparation always denies
it. These rules restrict child commands, so the agent can still authenticate
outside its command sandbox. Rules name existing resolved targets, including
symlink targets; absent paths are skipped to avoid sandbox mount failures and
persistent placeholder files. A sandbox startup failure never retries without
the denials. Claude's sandbox scope is unchanged: active GitHub policies with
confined review verification, registry domains, publication commands or a Docker
grant. Inactive policies and ordinary Claude issue review keep their earlier
tooling. Static-only recovery disables execution and does not prove denial.

A project whose tests need a particular store, or whose remote is SSH-only,
must opt out explicitly in the trusted source checkout:

```yaml
code_cycle:
  user_choices:
    credential_reads: ["~/.ssh"]
```

Only the six store names above are accepted, without wildcards or arbitrary
paths. Each exception removes only that store's denial in stages and preparation;
it does not grant network access or make raw SSH work through the HTTPS proxy.
The stage artifacts, preparation report and stage prompt record the exceptions.
Never put credentials in this configuration. Review clones cannot supply their
own exceptions. See [host permissions](host-permissions.md) for the remaining
trust boundary around publishing credentials.

When runtime preparation detects Node dependencies, the worker inherits the
same npm/Yarn registry domains as preparation, plus explicitly selected registry
domains. This also applies after incomplete setup: package-manager launchers and
configured verification can need the registry after the frozen install. Both
native executors receive this route before dispatch; projects without detected
Node setup gain no automatic registry access. Transient package-manager bootstrap
failures (`--version`, which may download the declared manager) get the same
bounded retry as installation. Neither networking nor retries count as successful
verification or enable lifecycle scripts.

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
inside that same sandbox. A second fixture per executor adds a project
`postinstall`. That script must run only in the contained lifecycle build. It
generates an ignored output, which must reach the checkout, and tries to write
the checkout directly, which the sandbox must deny. Preserve its report/log evidence and exit statuses.
Do not replace native failures with the subprocess fixture's passing result.
When invoked inside a Codex stage, the nested Codex app-server socket is masked
by the sandbox; classify affected checks as `not_verified` with evidence. Do not
retry sandbox variants, launch nested Codex or `srt` commands, or reroute for
this known limitation. Other nested host/executor combinations remain
unverified until a functional probe succeeds.

The native campaign is a required release-validation item for #279. Its result
must be attached to the PR; the ordinary unit suite does not imply it ran.
