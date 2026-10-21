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
`api.github.com`, with the same existing gh credentials, while its workspace and
publication contract remain read-only. This is a credential trust decision:
the domain grant itself does not make an HTTP credential read-only. Other
non-publishing roles receive only registry access, without a publication profile.

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
When invoked inside a Codex stage, the nested Codex app-server socket may be
masked; report that observed restriction once and use the cycle's configured
alternative executor rather than retrying sandbox variants.

The native campaign is a required release-validation item for #279. Its result
must be attached to the PR; the ordinary unit suite does not imply it ran.
