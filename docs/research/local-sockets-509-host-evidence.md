# Host spike evidence for #509 `local_sockets` (input for the implementation)

Status: **measured on the host, outside any stage sandbox**, 2026-11-19,
`codex-cli 0.170.0`, Linux. This evidence document was supplied in the resumed
checkout. Its measurements are host-reported, not locally re-executed by the
implementation stage. The implementation decision is recorded in
`docs/research/local-sockets-509.md`.

Method: `codex sandbox -c <profile overrides> -- python3 -I probe.py`, where the
probe attempts, with 3 s timeouts, TCP bind/connect on `127.0.0.1` and `::1`, a
TCP connect to `1.1.1.1:443`, an `AF_UNIX` bind/connect on a disposable socket,
and an `AF_UNIX` connect to `/var/run/docker.sock` (connect only, no Docker
operation). On this host the unsandboxed user can do all five (the user is in
the `docker` group), so each denial below is the sandbox's doing.

Profiles (all `default_permissions="p"`, `permissions.p.extends=":workspace"`):

- A: no network override.
- B: `features.network_proxy=true`, `permissions.p.network.enabled=true`, no
  domains, no Unix-socket option.
- C: B plus `permissions.p.network.dangerously_allow_all_unix_sockets=true`
  (what `docker_host` applies today).

| Probe | A | B | C |
|---|---|---|---|
| TCP loopback `127.0.0.1` and `::1` | denied (EPERM) | ok | ok |
| external TCP (direct) | denied | `Network is unreachable` | `Network is unreachable` |
| external HTTPS through the proxy (`curl https://example.com`) | not run | `403` from proxy after CONNECT | not run |
| `AF_UNIX` bind/connect | denied | denied (EPERM) | ok |
| `/var/run/docker.sock` connect | denied | denied (EPERM) | **ok (open)** |

Suite: `python3 -m unittest discover -s tests` run inside profile B passed,
2,676 tests in 540 s, OK (skipped=1).

## Decision (follows the criteria in `docs/research/local-sockets-509.md`)

- `local_sockets` = profile B: loopback TCP only. It opens neither external
  access (no domains, proxy refuses CONNECT) nor the Docker socket. It does not
  grant `AF_UNIX`.
- `AF_UNIX` needs `dangerously_allow_all_unix_sockets`, which opened
  `/var/run/docker.sock` here: its reach equals `docker_host`. Keep that
  distinction and disclose it; an `AF_UNIX`-only grant is not offered.
- The grant is a Codex profile change only; the Claude sandbox settings keep
  their own mapping (record any Claude-side limit instead of assuming parity).
- Tests: cover the profile built for `local_sockets` (network enabled, no
  domain rule, no Unix-socket option) next to the `docker_host` one.
