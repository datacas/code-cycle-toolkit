"""List effective sensitive permissions or revoke one stored scope.

This entry point never grants access. Configuration and stored decisions
remain separate sources; revoking a stored decision returns it to UNKNOWN.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from grants import ConsentBroker, ConsentStore, GRANTS, GrantError, current_platform
from run_cycle import load_config, resolve_config_path, repository_of, docker_host_socket_access


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("list", "revoke"))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--executor", choices=("codex", "claude"), required=True)
    parser.add_argument("--platform", default=current_platform())
    parser.add_argument("--grant-id", choices=tuple(GRANTS))
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    try:
        config_path = args.config or resolve_config_path(Path.cwd())
        config = load_config(config_path) if config_path is not None else {}
        preauthorized = (frozenset({"docker_host"}) if repository_of(config) == args.repo
                         and docker_host_socket_access(config) else frozenset())
        store = ConsentStore()
        if args.action == "revoke":
            if args.grant_id is None:
                parser.error("revoke requires --grant-id")
            grant = GRANTS[args.grant_id]
            scope = grant.scope_for(args.executor, args.platform)
            if scope is None:
                raise GrantError("no registered scope for this executor/platform")
            store.revoke(args.repo, grant, scope)
        broker = ConsentBroker(args.repo, platform=args.platform, store=store,
                               preauthorized=preauthorized)
        print(json.dumps(broker.listing(args.executor), indent=2))
        return 0
    except (GrantError, ValueError, OSError) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
