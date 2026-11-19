# Local-socket grant spike for #509

Status: **not_verified**, 2026-11-19. No local-socket profile is enabled by
this partial implementation. AC-1 remains open.

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

The remaining host-capable spike must record, for the installed Codex version:

1. Baseline and candidate profiles, with exact argv and version.
2. TCP bind/connect on `127.0.0.1` and `::1`.
3. External network access, including direct sockets and proxy-aware requests.
4. AF_UNIX bind/connect to a disposable socket.
5. Whether `/var/run/docker.sock` remains inaccessible, without running Docker
   operations or widening Docker access.
6. `python3 -m unittest discover -s tests` in the chosen profile.

Implement `local_sockets` only if the measured profile grants the requested
local access while Docker stays closed. If enabling it also opens external
access, do not describe it as local-only. If Unix sockets require the reach
of `docker_host`, retain that distinction and disclose the real scope.

Documentation context, not execution evidence: the upstream
[Codex network-proxy README](https://github.com/openai/codex/blob/main/codex-rs/network-proxy/README.md)
documents `allow_local_binding`, exact domain rules, and Unix-socket options.
Those API descriptions cannot substitute for the required Linux runtime spike.
