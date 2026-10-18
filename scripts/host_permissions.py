"""The opt-in global host permissions for interactive Claude and Codex sessions.

A dispatched stage never needs this: the runtime gives each native stage its own
command table (`stage_permissions`). This is for a person who also runs the
skills interactively and wants fewer prompts there, and it is deliberately
conservative because it affects every session on the machine (#142):

- it allows only reading and Git queries, never a verification, write, commit,
  push or pull request, which are granted per stage and per project;
- it denies force-pushes, merges and the deletion of branches or remote refs;
- it preserves every hook, setting and rule the user already has.

Nothing is written unless asked: `plan` shows the change, `apply --yes` makes
it, after a backup, and `revert --yes` removes only what the toolkit added.

`~/.claude/settings.json` is JSON whose entries are plain strings, so it cannot
carry a marker; the entries the toolkit created are recorded in a registry
outside the repository, `<config>/code-cycle-toolkit/host-permissions.json`.
In Codex's `default.rules` the toolkit's rules sit between marker comments,
and the registry keeps the block's hash. An entry or block edited by hand is
never overwritten or removed silently: it is reported as a conflict.

Codex's rules are checked by hand with `codex execpolicy check --rules
<file> -- <command>`; Claude has no equivalent, so its generated settings are
validated as JSON with well-formed entries, which is all that can honestly be
claimed for them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

#: Read-only Git and GitHub queries, and nothing else. A prefix rule allows
#: every option after it, so `git diff`, `log` and `show` (whose `--output`
#: writes any file) and `git grep` (whose `-O` runs a program) stay out: no
#: prefix deny catches every spelling of those options.
READ_COMMANDS = (
    ("git", "status"),
    ("git", "rev-parse"), ("git", "branch", "--show-current"), ("git", "merge-base"),
    ("git", "ls-files"), ("git", "blame"),
    ("gh", "issue", "view"), ("gh", "issue", "list"), ("gh", "pr", "view"),
    ("gh", "pr", "list"), ("gh", "pr", "checks"), ("gh", "pr", "diff"),
    ("gh", "auth", "status"), ("gh", "repo", "view"),
)
#: Force-pushes, merges and deletions of branches or remote refs. A prefix rule
#: cannot see every spelling (`-uf`, a `+refspec`, flags after the refspec), so
#: these narrow interactive sessions; they are not the stage hook's guarantee.
DENIED_COMMANDS = (
    ("git", "push", "--force"), ("git", "push", "-f"), ("git", "push", "--force-with-lease"),
    ("git", "push", "--force-if-includes"), ("git", "push", "--mirror"),
    ("git", "push", "--delete"), ("git", "push", "-d"), ("git", "push", "--prune"),
    ("git", "push", "origin", "--force"), ("git", "push", "origin", "-f"),
    ("git", "push", "origin", "--delete"), ("git", "push", "origin", "-d"),
    ("git", "branch", "-D"), ("git", "branch", "-d"), ("git", "branch", "--delete"),
    ("git", "update-ref", "-d"), ("gh", "pr", "merge"),
)

BEGIN_MARKER = "# >>> code-cycle-toolkit managed rules (edit outside this block) >>>"
END_MARKER = "# <<< code-cycle-toolkit managed rules <<<"


class HostPermissionError(RuntimeError):
    """A file the change would touch cannot be read or written safely."""


def claude_entries() -> dict[str, list[str]]:
    return {"allow": [f"Bash({' '.join(words)}:*)" for words in READ_COMMANDS],
            "deny": [f"Bash({' '.join(words)}:*)" for words in DENIED_COMMANDS]}


def codex_block() -> str:
    lines = [BEGIN_MARKER]
    for words in READ_COMMANDS:
        lines.append(f"prefix_rule(pattern={json.dumps(list(words))}, decision=\"allow\")")
    for words in DENIED_COMMANDS:
        lines.append(f"prefix_rule(pattern={json.dumps(list(words))}, decision=\"forbidden\", "
                     "justification=\"code-cycle-toolkit: force-pushes, merges and ref "
                     "deletions are never run from an agent session\")")
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Paths:
    claude_settings: Path
    codex_rules: Path
    registry: Path

    @classmethod
    def default(cls) -> "Paths":
        claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        codex = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        if os.name == "nt":
            state = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        else:
            state = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        return cls(claude / "settings.json", codex / "rules" / "default.rules",
                   state / "code-cycle-toolkit" / "host-permissions.json")


@dataclass
class Report:
    host: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    backup: str | None = None
    written: bool = False

    def lines(self) -> list[str]:
        out = [f"{self.host}: " + ("written" if self.written else "unchanged")]
        out += [f"  + {entry}" for entry in self.added]
        out += [f"  - {entry}" for entry in self.removed]
        out += [f"  = {entry} (already the user's; left as it is)" for entry in self.kept]
        out += [f"  ! conflict: {entry}" for entry in self.conflicts]
        if self.backup:
            out.append(f"  backup: {self.backup}")
        return out


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise HostPermissionError(f"{path} cannot be read as JSON ({error}); nothing was "
                                  "changed") from error
    if not isinstance(payload, dict):
        raise HostPermissionError(f"{path} is not a JSON object; nothing was changed")
    return payload


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.code-cycle-tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _backup(path: Path) -> str | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.code-cycle-backup-{stamp}")
    shutil.copy2(path, target)
    return str(target)


class HostPermissions:
    def __init__(self, paths: Paths | None = None) -> None:
        self.paths = paths or Paths.default()

    # --- registry ---------------------------------------------------------

    def _registry(self) -> dict:
        return _read_json(self.paths.registry)

    def _save_registry(self, registry: dict) -> None:
        _write_atomically(self.paths.registry, json.dumps(registry, indent=2, sort_keys=True)
                          + "\n")

    # --- Claude -------------------------------------------------------------

    def claude(self, *, apply: bool, write: bool) -> Report:
        """Add (`apply`) or remove the managed entries; write only when `write`."""
        report = Report("claude")
        settings = _read_json(self.paths.claude_settings)
        permissions = settings.get("permissions")
        if permissions is not None and not isinstance(permissions, dict):
            raise HostPermissionError(f"{self.paths.claude_settings}: permissions is not an "
                                      "object; nothing was changed")
        permissions = dict(permissions or {})
        registry = self._registry()
        owned = registry.get("claude", {}).get("entries", {})
        desired = claude_entries()
        changed = False
        new_owned: dict[str, list[str]] = {}
        for kind in ("allow", "deny"):
            current = permissions.get(kind, [])
            if not isinstance(current, list):
                raise HostPermissionError(f"{self.paths.claude_settings}: permissions.{kind} "
                                          "is not a list; nothing was changed")
            current = list(current)
            mine = [entry for entry in owned.get(kind, [])]
            missing_mine = [entry for entry in mine if entry not in current]
            # A managed entry that is gone was removed or edited by hand.
            report.conflicts += [f"permissions.{kind} {entry} was removed or edited by hand; "
                                 "it is not restored" for entry in missing_mine]
            kept_mine = [entry for entry in mine if entry in current]
            if apply:
                for entry in desired[kind]:
                    if entry in current:
                        if entry not in mine:
                            report.kept.append(f"permissions.{kind} {entry}")
                        continue
                    if entry in missing_mine:
                        continue
                    current.append(entry)
                    kept_mine.append(entry)
                    report.added.append(f"permissions.{kind} {entry}")
                    changed = True
                # An entry an earlier version added and this one no longer
                # wants is withdrawn, so a re-apply narrows the set too.
                for entry in [entry for entry in kept_mine if entry not in desired[kind]]:
                    current.remove(entry)
                    kept_mine.remove(entry)
                    report.removed.append(f"permissions.{kind} {entry}")
                    changed = True
                new_owned[kind] = kept_mine + [entry for entry in missing_mine]
            else:
                # A hand-edited entry is reported above and left alone; once
                # reverted, nothing in this host is the toolkit's any more.
                for entry in kept_mine:
                    current.remove(entry)
                    report.removed.append(f"permissions.{kind} {entry}")
                    changed = True
            if current or kind in permissions:
                permissions[kind] = current
        if write and changed:
            report.backup = _backup(self.paths.claude_settings)
            settings["permissions"] = permissions
            _write_atomically(self.paths.claude_settings,
                              json.dumps(settings, indent=2) + "\n")
            report.written = True
        if write and (changed or new_owned != owned):
            if any(new_owned.values()):
                registry["claude"] = {"settings": str(self.paths.claude_settings),
                                      "entries": new_owned}
            else:
                registry.pop("claude", None)
            self._save_registry(registry)
        return report

    # --- Codex -------------------------------------------------------------

    def codex(self, *, apply: bool, write: bool) -> Report:
        report = Report("codex")
        path = self.paths.codex_rules
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        except OSError as error:
            raise HostPermissionError(f"{path} cannot be read ({error})") from error
        registry = self._registry()
        recorded = registry.get("codex", {}).get("block_sha256")
        begin, end = text.find(BEGIN_MARKER), text.find(END_MARKER)
        block = None
        if begin >= 0 and end > begin:
            stop = end + len(END_MARKER)
            stop += 1 if text[stop:stop + 1] == "\n" else 0
            block = text[begin:stop]
        elif begin >= 0 or end >= 0:
            report.conflicts.append(f"{path} has only one of the toolkit's markers; "
                                    "fix it by hand")
            return report
        wanted = codex_block()
        if block is not None and _sha(block) != recorded and block != wanted:
            report.conflicts.append(f"the managed block in {path} was edited by hand; it is "
                                    "neither replaced nor removed")
            return report
        if apply:
            if block == wanted:
                new_text = text
            elif block is not None:
                new_text = text.replace(block, wanted)
            else:
                separator = "" if not text or text.endswith("\n") else "\n"
                new_text = text + separator + wanted
            if new_text != text:
                report.added.append(f"{path}: managed block ({len(READ_COMMANDS)} allow, "
                                    f"{len(DENIED_COMMANDS)} forbidden)")
        else:
            new_text = text.replace(block, "") if block is not None else text
            if block is not None:
                report.removed.append(f"{path}: managed block")
        if write and new_text != text:
            report.backup = _backup(path)
            _write_atomically(path, new_text)
            report.written = True
        if write:
            if apply:
                registry["codex"] = {"rules": str(path), "block_sha256": _sha(wanted)}
            else:
                registry.pop("codex", None)
            self._save_registry(registry)
        return report

    def validate_claude(self) -> list[str]:
        """Problems with the Claude settings as written, or an empty list."""
        settings = _read_json(self.paths.claude_settings)
        permissions = settings.get("permissions") or {}
        if not isinstance(permissions, dict):
            return ["permissions is not an object"]
        problems = []
        lists = {}
        for kind in ("allow", "deny"):
            entries = permissions.get(kind, [])
            if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
                problems.append(f"permissions.{kind} is not a list of strings")
                entries = []
            lists[kind] = entries
        owned = self._registry().get("claude", {}).get("entries", {})
        for kind in ("allow", "deny"):
            for entry in owned.get(kind, []):
                if entry in lists[kind] and not re.fullmatch(r"Bash\([a-z][^()]*:\*\)", entry):
                    problems.append(f"malformed managed entry {entry!r}")
        for entry in sorted(set(owned.get("allow", [])) & set(lists["deny"])):
            problems.append(f"{entry} is both allowed and denied")
        return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Opt-in global host permissions for interactive sessions (#142).")
    parser.add_argument("action", choices=("plan", "apply", "revert", "validate"))
    parser.add_argument("--host", choices=("claude", "codex", "both"), default="both")
    parser.add_argument("--yes", action="store_true",
                        help="confirm the change shown by plan; required to write")
    args = parser.parse_args(argv)
    manager = HostPermissions()
    hosts = ("claude", "codex") if args.host == "both" else (args.host,)
    if args.action in {"apply", "revert"} and not args.yes:
        parser.error(f"{args.action} changes global files for every session; run plan, "
                     "then repeat with --yes")
    try:
        if args.action == "validate":
            problems = manager.validate_claude() if "claude" in hosts else []
            for problem in problems:
                print(f"claude: {problem}")
            if "codex" in hosts:
                print("codex: check by hand with codex execpolicy check --rules "
                      f"{manager.paths.codex_rules} -- git push --force origin x")
            return 1 if problems else 0
        conflicts = False
        for host in hosts:
            report = getattr(manager, host)(apply=args.action in {"plan", "apply"},
                                            write=args.action != "plan")
            print("\n".join(report.lines()))
            conflicts |= bool(report.conflicts)
        return 3 if conflicts else 0
    except HostPermissionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
