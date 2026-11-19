# Local-socket grant spike for #509

Status: **host-reported measurement**, 2026-11-19, Codex CLI 0.170.0 on Linux.
The supplied [host evidence](local-sockets-509-host-evidence.md) reports the
matrix below. This stage did not execute that spike locally; local boundary
verification remains `not_verified` because nested Codex cannot start here.

The implementation stage is itself a Codex sandbox. A metadata-only read,
`rtk proxy ls -ld /tmp/codex-daemon-1000`, returned
mode `d---------` (000), owned by the invoking user, on the daemon directory.
The runtime explicitly identifies this mode-000 mask as the nested Codex
app-server limit and prohibits starting `codex sandbox`, `codex exec`, or
`srt` within this boundary. No nested command was launched, no isolation was
disabled, and no socket reachability result is inferred from documentation.

Recovery evaluated: the active host exposes shell execution only within this
stage's existing sandbox, with escalation unavailable. Changing TMPDIR,
CODEX_HOME, or daemon options cannot recover this known limit. The selected
executor remains Codex. This is unavailable boundary verification, not a
successful spike and not evidence that a host-side Codex profile is unsafe.

The host report used `codex sandbox -c <profile overrides> -- python3 -I probe.py`
with three-second socket timeouts. Each profile selected
`default_permissions="p"` and `permissions.p.extends=":workspace"`:

- A: no network override.
- B: `features.network_proxy=true`, `permissions.p.network.enabled=true`,
  no external domain rules, no Unix-socket option.
- C: B plus `permissions.p.network.dangerously_allow_all_unix_sockets=true`.

| Probe | A | B | C |
|---|---|---|---|
| TCP loopback, IPv4 and IPv6 | EPERM | allowed | allowed |
| Direct external TCP | denied | network unreachable | network unreachable |
| HTTPS through proxy | not run | CONNECT refused, HTTP 403 | not run |
| Disposable AF_UNIX bind/connect | denied | EPERM | allowed |
| Docker socket connect only | denied | EPERM | **allowed** |

The host user could perform all socket probes outside isolation. The report
also records `python3 -m unittest discover -s tests` inside B: 2,676 tests in
540 seconds, OK with one skipped. This is supplied evidence on the preceding
commit, not a locally executed test or proof of the current head.

Decision: `local_sockets` uses B and grants loopback TCP only. It adds neither
external domains nor AF_UNIX access. Existing separately authorized publication
or registry domains remain in force. Unix sockets need the broader
`docker_host` grant: C opens Docker, ssh-agent and other reachable Unix sockets.
The grant currently has a Codex/Linux scope only; Claude reports it unavailable
rather than claiming an equivalent unmeasured boundary.

Documentation context, not execution evidence: the upstream
[Codex network-proxy README](https://github.com/openai/codex/blob/main/codex-rs/network-proxy/README.md)
documents `allow_local_binding`, exact domain rules, and Unix-socket options.
Those API descriptions cannot substitute for the required Linux runtime spike.
