"""Show, propose and write a repository's routing profiles.

`cc-profile-config` drives this module; it also runs on its own. It never
writes unless asked with `write --confirmed`, and what it writes is only the
`code_cycle.profiles` block: every other line of `.code-cycle.yml`, comments
included, stays as it was.

Three rules shape the presets.

**A balanced preset never lets a vendor review its own work.** The providers
that implement and the providers that review are disjoint, fallbacks included.
A fallback that crosses the split is refused unless it is explicitly allowed.

**A balanced preset declares no fallback on either side.** Availability is a
property of the executor, so a fallback on the same executor is unavailable
whenever its primary is; and a fallback on the other executor crosses the
split. An unavailable executor therefore blocks the stage instead.

**Every role must keep an executor that can honour its workspace contract.**
Claude cannot confine writes to a disposable workspace, so `auxiliary_tool`
(verify, run, bootstrap) stays on Codex even in the Anthropic-only preset.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from router import (
    DEFAULT_PROFILES,
    PROFILE_COST,
    ROLE_CANDIDATES,
    Profile,
    RouterError,
    load_profiles,
    parse_target,
)

CONFIG_NAME = ".code-cycle.yml"

#: The profiles on each side of an implement/review split.
IMPLEMENT_PROFILES = ("cheap_coder", "deep_coder")
REVIEW_PROFILES = ("reviewer", "senior_reviewer", "security")

#: The provider each native executor runs. Orca can run either.
EXECUTOR_PROVIDERS = {"codex": "openai", "claude": "anthropic"}
KNOWN_EXECUTORS = ("codex", "claude", "orca")

#: How a read-only role is guaranteed on each executor. Codex enforces it with
#: its sandbox. Claude runs in a disposable clone whose writes are detected and
#: discarded, but a remote write cannot be prevented. Orca needs an explicit
#: review workspace, which only an Orca run provides.
READ_ONLY_MODE = {
    "codex": "enforced",
    "claude": "detected",
    "orca": "requires an Orca review workspace",
}
#: Executors that can confine a `disposable` role (verify, run) to its workspace.
DISPOSABLE_EXECUTORS = frozenset({"codex"})
#: Roles by workspace contract, from `cycle.ROLE_CONTRACTS`.
DISPOSABLE_ROLES = frozenset({"verify", "run"})
READ_ONLY_ROLES = frozenset({"review", "rereview", "security", "bootstrap", "coordinate"})

_LUNA = "codex:openai/gpt-6-luna"
_SOL = "codex:openai/gpt-6-sol"
_SONNET = "claude:anthropic/claude-sonnet-5"
_OPUS = "claude:anthropic/claude-opus-5-5"
_HAIKU = "claude:anthropic/claude-haiku-4-5-20251001"


@dataclass(frozen=True)
class Preset:
    name: str
    title: str
    #: Every profile's primary and fallback. Absent profiles keep the default.
    profiles: dict
    #: (implementing providers, reviewing providers) the preset promises to
    #: keep disjoint, or None when it makes no such promise.
    split: tuple[frozenset[str], frozenset[str]] | None = None
    notes: tuple[str, ...] = ()


def _p(primary: str, fallback: str | None = None) -> dict:
    return {"primary": primary, "fallback": fallback}


_NO_FALLBACK_NOTE = (
    "No fallback on either side: a fallback on the same executor is unavailable "
    "whenever its primary is, and one on the other executor would cross the "
    "split. An unavailable executor blocks the stage."
)

PRESETS: dict[str, Preset] = {
    preset.name: preset for preset in (
        Preset(
            "balanced-openai-implements",
            "Balanced: OpenAI implements, Anthropic reviews",
            {
                "cheap_coder": _p(f"{_LUNA} high"),
                "deep_coder": _p(f"{_LUNA} max"),
                "reviewer": _p(f"{_SONNET} high"),
                "senior_reviewer": _p(f"{_OPUS} high"),
                "security": _p(f"{_OPUS} high"),
            },
            split=(frozenset({"openai"}), frozenset({"anthropic"})),
            notes=(
                _NO_FALLBACK_NOTE,
                "Claude reviews with read_only_mode = detected: its local edits "
                "are discarded and a change to the branch fails the stage, but a "
                "remote write cannot be prevented or undone.",
            ),
        ),
        Preset(
            "balanced-anthropic-implements",
            "Balanced: Anthropic implements, OpenAI reviews",
            {
                "cheap_coder": _p(f"{_SONNET} high"),
                "deep_coder": _p(f"{_OPUS} high"),
                "reviewer": _p(f"{_SOL} high"),
                "senior_reviewer": _p(f"{_SOL} max"),
                "security": _p(f"{_SOL} high"),
            },
            split=(frozenset({"anthropic"}), frozenset({"openai"})),
            notes=(
                _NO_FALLBACK_NOTE,
                "Codex reviews with read_only_mode = enforced: its sandbox "
                "prevents workspace writes.",
            ),
        ),
        Preset(
            "defaults",
            "Toolkit defaults",
            {name: _p(spec["primary"], spec.get("fallback"))
             for name, spec in DEFAULT_PROFILES.items()},
            notes=("Writing this preset removes the profiles block.",),
        ),
        Preset(
            "single-openai",
            "Single vendor: OpenAI",
            {
                "cheap_tool": _p(f"{_LUNA} low"),
                "auxiliary_tool": _p(f"{_LUNA} medium"),
                "coordinator": _p(f"{_LUNA} medium"),
                "cheap_coder": _p(f"{_LUNA} high"),
                "deep_coder": _p(f"{_LUNA} max"),
                "reviewer": _p(f"{_SOL} high"),
                "senior_reviewer": _p(f"{_SOL} max"),
                "security": _p(f"{_SOL} high"),
            },
            notes=("The implementing vendor also reviews its own work.",),
        ),
        Preset(
            "single-anthropic",
            "Single vendor: Anthropic",
            {
                "cheap_tool": _p(f"{_HAIKU} low"),
                # Claude cannot honour the disposable contract of verify/run.
                "auxiliary_tool": _p(f"{_LUNA} medium"),
                "coordinator": _p(f"{_SONNET} low"),
                "cheap_coder": _p(f"{_SONNET} high"),
                "deep_coder": _p(f"{_OPUS} high"),
                "reviewer": _p(f"{_SONNET} high"),
                "senior_reviewer": _p(f"{_OPUS} high"),
                "security": _p(f"{_OPUS} high"),
            },
            notes=(
                "The implementing vendor also reviews its own work.",
                "auxiliary_tool (verify, run, bootstrap) stays on Codex: Claude "
                "cannot confine writes to a disposable workspace, so verify and "
                "run would block.",
            ),
        ),
    )
}


class ProfileConfigError(ValueError):
    """A proposal that must not be written."""


# --- reading ---------------------------------------------------------------

def declared_profiles(config: dict | None) -> dict:
    section = (config or {}).get("code_cycle") or {}
    declared = section.get("profiles") if isinstance(section, dict) else None
    return declared if isinstance(declared, dict) else {}


def roles_by_profile() -> dict[str, tuple[str, ...]]:
    roles: dict[str, list[str]] = {name: [] for name in DEFAULT_PROFILES}
    for role, candidates in ROLE_CANDIDATES.items():
        for name in candidates:
            roles.setdefault(name, []).append(role)
    return {name: tuple(value) for name, value in roles.items()}


def full_spec(profiles: dict[str, Profile]) -> dict:
    """Profiles as plain `{name: {primary, fallback}}` strings."""
    return {
        name: _p(str(profile.primary),
                 str(profile.fallback) if profile.fallback else None)
        for name, profile in profiles.items()
    }


def declaration_for(spec: dict) -> dict:
    """The smallest `code_cycle.profiles` block that yields `spec`.

    Only what differs from the defaults is declared. A default fallback the
    spec drops is declared as `null`, which is how `load_profiles` removes it.
    """
    declared = {}
    for name, default in DEFAULT_PROFILES.items():
        wanted = spec.get(name)
        if wanted is None:
            continue
        entry = {}
        if wanted["primary"] != default["primary"]:
            entry["primary"] = wanted["primary"]
        if wanted.get("fallback") != default.get("fallback"):
            entry["fallback"] = wanted.get("fallback")
        if entry:
            declared[name] = entry
    return declared


def codex_models(path: str | Path | None = None) -> dict[str, frozenset[str]] | None:
    """Model slug -> accepted efforts from Codex's local models cache."""
    if path is None:
        home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        path = home / "models_cache.json"
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return None
    out = {}
    for model in models:
        if not isinstance(model, dict) or not isinstance(model.get("slug"), str):
            continue
        efforts = set()
        for level in model.get("supported_reasoning_levels") or ():
            effort = level.get("effort") if isinstance(level, dict) else level
            if isinstance(effort, str):
                efforts.add(effort)
        out[model["slug"]] = frozenset(efforts)
    return out or None


# --- checking --------------------------------------------------------------

def target_checks(spec: str, codex_catalog: dict | None) -> list[tuple[str, str]]:
    """(level, message) pairs for one target.

    The level is error, warning or note, or `unchecked` with the executor name
    as the message when that executor publishes no model list to check against.
    """
    try:
        target = parse_target(spec)
    except RouterError as error:
        return [("error", str(error))]
    checks = []
    if target.executor not in KNOWN_EXECUTORS:
        checks.append(("error", f"unknown executor {target.executor!r} in {spec!r}"))
        return checks
    expected = EXECUTOR_PROVIDERS.get(target.executor)
    if expected and target.provider != expected:
        checks.append(("error", f"{target.executor} runs {expected} models, not "
                                f"{target.provider!r}: {spec!r}"))
    if target.executor == "codex":
        if codex_catalog is None:
            checks.append(("note", f"{spec}: model and effort not checked "
                                   "(no Codex models cache)"))
        elif target.model not in codex_catalog:
            checks.append(("error", f"{spec}: Codex does not list model "
                                    f"{target.model!r}"))
        elif codex_catalog[target.model] and target.effort not in codex_catalog[target.model]:
            accepted = ", ".join(sorted(codex_catalog[target.model]))
            checks.append(("error", f"{spec}: {target.model} accepts efforts "
                                    f"{accepted}, not {target.effort!r}"))
    else:
        checks.append(("unchecked", target.executor))
    return checks


def split_violations(spec: dict, implement: frozenset[str],
                     review: frozenset[str]) -> list[str]:
    violations = []
    for names, allowed, side in ((IMPLEMENT_PROFILES, implement, "implements"),
                                 (REVIEW_PROFILES, review, "reviews")):
        for name in names:
            for key in ("primary", "fallback"):
                value = (spec.get(name) or {}).get(key)
                if not value:
                    continue
                provider = parse_target(value).provider
                if provider not in allowed:
                    violations.append(
                        f"{name}.{key} uses {provider}, but the side that {side} "
                        f"is {', '.join(sorted(allowed))}")
    return violations


def workspace_gaps(spec: dict) -> list[str]:
    """Roles left with no target whose executor can honour their contract."""
    gaps = []
    for role, candidates in ROLE_CANDIDATES.items():
        if role not in DISPOSABLE_ROLES:
            continue
        for name in candidates:
            targets = [v for v in (spec[name]["primary"], spec[name].get("fallback")) if v]
            if not any(parse_target(v).executor in DISPOSABLE_EXECUTORS for v in targets):
                gaps.append(f"{role} uses {name}, and none of its executors can "
                            "confine writes to a disposable workspace: the stage "
                            "would block")
    return gaps


def executor_warnings(spec: dict, probes: dict | None) -> list[str]:
    """Targets whose executor is not installed and authenticated here."""
    if probes is None:
        return []
    from router import Availability
    usable = {Availability.AUTHENTICATED, Availability.READY}
    warnings = []
    for name, value in spec.items():
        for key in ("primary", "fallback"):
            if not value.get(key):
                continue
            executor = parse_target(value[key]).executor
            probe = probes.get(executor)
            state = probe.availability if probe else Availability.UNKNOWN
            if state not in usable:
                warnings.append(f"{name}.{key}: {executor} is {state.value} on this "
                                "machine, so that target cannot run here yet")
    return warnings


# --- the proposal ----------------------------------------------------------

@dataclass
class Proposal:
    spec: dict
    declaration: dict
    preset: Preset | None
    errors: list[str]
    warnings: list[str]
    notes: list[str]


def build_proposal(base: dict, *, preset: str | None = None,
                   overrides: list[str] = (), allow_cross_split: bool = False,
                   codex_catalog: dict | None = None) -> Proposal:
    """Resolve a preset plus `profile.key=target` overrides over `base`."""
    spec = {name: dict(value) for name, value in base.items()}
    chosen = None
    if preset:
        if preset not in PRESETS:
            raise ProfileConfigError(
                f"unknown preset {preset!r}; choose one of: {', '.join(PRESETS)}")
        chosen = PRESETS[preset]
        for name, value in chosen.profiles.items():
            spec[name] = dict(value)
    for override in overrides:
        match = re.fullmatch(r"\s*([a-z_]+)\.(primary|fallback)\s*=\s*(.*?)\s*", override)
        if not match:
            raise ProfileConfigError(
                f"an override is 'profile.primary=TARGET' or "
                f"'profile.fallback=TARGET|none': {override!r}")
        name, key, value = match.groups()
        if name not in DEFAULT_PROFILES:
            raise ProfileConfigError(f"unknown profile name: {name}")
        if value.lower() in ("", "none", "null"):
            if key == "primary":
                raise ProfileConfigError(f"profile {name!r} needs a primary target")
            value = None
        spec[name][key] = value

    errors, warnings, notes = [], [], list(chosen.notes if chosen else ())
    unchecked: dict[str, list[str]] = {}
    for name, value in spec.items():
        for key in ("primary", "fallback"):
            if value.get(key):
                for level, message in target_checks(value[key], codex_catalog):
                    if level == "unchecked":
                        unchecked.setdefault(message, []).append(f"{name}.{key}")
                        continue
                    {"error": errors, "warning": warnings, "note": notes}[level].append(
                        f"{name}.{key}: {message}")
    for executor, where in unchecked.items():
        notes.append(f"{executor} publishes no local model list, so the model and "
                     f"effort of {', '.join(where)} were not checked")
    if not errors:
        declaration = declaration_for(spec)
        try:
            load_profiles({"code_cycle": {"profiles": declaration}})
        except RouterError as error:
            errors.append(str(error))
        if chosen and chosen.split:
            violations = split_violations(spec, *chosen.split)
            if violations and not allow_cross_split:
                errors.extend(violations)
            elif violations:
                warnings.extend(
                    v + " (allowed explicitly: that stage's vendor may review "
                    "its own work)" for v in violations)
        warnings.extend(workspace_gaps(spec))
    else:
        declaration = {}
    # A proposal with errors carries no block, so nothing can write it.
    return Proposal(spec, {} if errors else declaration, chosen, errors, warnings, notes)


# --- rendering -------------------------------------------------------------

def _quote(value: str | None) -> str:
    return "null" if value is None else json.dumps(value)


def render_block(declaration: dict, indent: str = "  ") -> str:
    """The `profiles:` block as YAML, indented as a child of `code_cycle`."""
    if not declaration:
        return ""
    lines = [f"{indent}profiles:"]
    for name in DEFAULT_PROFILES:
        if name not in declaration:
            continue
        lines.append(f"{indent * 2}{name}:")
        for key in ("primary", "fallback"):
            if key in declaration[name]:
                lines.append(f"{indent * 3}{key}: {_quote(declaration[name][key])}")
    return "\n".join(lines) + "\n"


def effective_rows(spec: dict, declared: dict | None = None) -> list[dict]:
    declared = declared or {}
    roles = roles_by_profile()
    rows = []
    for name in DEFAULT_PROFILES:
        entry = spec[name]
        targets = [t for t in (entry["primary"], entry.get("fallback")) if t]
        rows.append({
            "profile": name,
            "roles": list(roles.get(name, ())),
            "primary": entry["primary"],
            "primary_source": "config" if "primary" in declared.get(name, {}) else "default",
            "fallback": entry.get("fallback"),
            "fallback_source": "config" if "fallback" in declared.get(name, {}) else "default",
            "cost": PROFILE_COST.get(name),
            # Only meaningful for a profile that serves a read-only role.
            "read_only_mode": {
                parse_target(t).executor: READ_ONLY_MODE.get(parse_target(t).executor, "unknown")
                for t in targets
            } if READ_ONLY_ROLES & set(roles.get(name, ())) else {},
        })
    return rows


def render_table(rows: list[dict]) -> str:
    out = ["| Profile | Roles | Primary | Fallback | Cost | Read-only |",
           "|---|---|---|---|---:|---|"]
    for row in rows:
        primary = f"`{row['primary']}` ({row['primary_source']})"
        fallback = (f"`{row['fallback']}` ({row['fallback_source']})"
                    if row["fallback"] else f"— ({row['fallback_source']})")
        read_only = ", ".join(f"{k}: {v}" for k, v in row["read_only_mode"].items()) or "—"
        roles = ", ".join(row["roles"]) or "—"
        out.append(f"| `{row['profile']}` | {roles} | {primary} | {fallback} | "
                   f"{row['cost']} | {read_only} |")
    return "\n".join(out)


def render_executors(probes: dict | None) -> str:
    if probes is None:
        return "Executors: not probed."
    parts = [f"`{name}`: {probe.availability.value}"
             + (f" ({probe.proof})" if getattr(probe, "proof", "") else "")
             for name, probe in sorted(probes.items())]
    return "Executors: " + "; ".join(parts) + "."


# --- writing ---------------------------------------------------------------

_TOP_KEY = re.compile(r"^[^\s#][^:]*:")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def apply_declaration(text: str, declaration: dict) -> str:
    """Replace, insert or remove `code_cycle.profiles` in `text`.

    Works on lines rather than through a YAML round trip, so every comment and
    key outside the profiles block survives byte for byte.
    """
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    start = next((i for i, line in enumerate(lines)
                  if re.match(r"^code_cycle:\s*(#.*)?$", line)), None)
    if start is None:
        if any(re.match(r"^code_cycle\s*:", line) for line in lines):
            raise ProfileConfigError(
                "code_cycle is not written as a block mapping; edit it by hand")
        if not declaration:
            return "".join(lines)
        prefix = "".join(lines)
        if prefix and not prefix.endswith("\n\n"):
            prefix += "\n"
        return prefix + "code_cycle:\n" + render_block(declaration)

    end = next((i for i in range(start + 1, len(lines))
                if _TOP_KEY.match(lines[i])), len(lines))
    children = [i for i in range(start + 1, end)
                if lines[i].strip() and not lines[i].lstrip().startswith("#")]
    unit = _indent(lines[children[0]]) if children else 2
    indent = " " * unit

    found = next((i for i in children
                  if _indent(lines[i]) == unit
                  and re.match(r"^\s*profiles\s*:", lines[i])), None)
    block = render_block(declaration, indent).splitlines(keepends=True)
    if found is not None:
        if not re.match(r"^\s*profiles:\s*(#.*)?$", lines[found]):
            raise ProfileConfigError(
                "code_cycle.profiles is not written as a block mapping; edit it by hand")
        stop = found + 1
        while stop < end and (not lines[stop].strip() or _indent(lines[stop]) > unit):
            stop += 1
        while stop > found + 1 and not lines[stop - 1].strip():
            stop -= 1
        return "".join(lines[:found] + block + lines[stop:])
    if not declaration:
        return "".join(lines)
    last = children[-1] if children else start
    while last + 1 < end and lines[last + 1].strip() and _indent(lines[last + 1]) > unit:
        last += 1
    return "".join(lines[:last + 1] + block + lines[last + 1:])


def write_declaration(path: Path, declaration: dict) -> str:
    """Write the block and prove nothing else in the file changed."""
    import yaml  # the configuration cannot be read without it anyway

    original = path.read_text(encoding="utf-8") if path.is_file() else ""
    before = yaml.safe_load(original) or {}
    updated = apply_declaration(original, declaration)
    after = yaml.safe_load(updated) or {}

    def without_profiles(value: dict) -> dict:
        value = json.loads(json.dumps(value))
        section = value.get("code_cycle")
        if isinstance(section, dict):
            section.pop("profiles", None)
            if not section and "code_cycle" not in before:
                value.pop("code_cycle")
        return value

    if without_profiles(before) != without_profiles(after):
        raise ProfileConfigError("the edit would change keys outside code_cycle.profiles")
    if declared_profiles(after) != declaration:
        raise ProfileConfigError("the written profiles block does not read back as proposed")
    load_profiles(after)
    _replace_atomically(path, updated)
    return updated


def _replace_atomically(path: Path, text: str) -> None:
    """Write `text` to `path` through a fresh temporary file.

    The temporary name is unique and created exclusively, so nothing planted
    beside the configuration — a symlink at a predictable name — can redirect
    the write to another file.
    """
    import tempfile

    directory = path.parent if str(path.parent) else Path(".")
    if path.is_file():
        mode = path.stat().st_mode & 0o777
    else:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.chmod(name, mode)
        os.replace(name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)
        raise


# --- command line ----------------------------------------------------------

def _load(cwd: str, config: str | None) -> tuple[Path, dict]:
    path = Path(config) if config else Path(cwd) / CONFIG_NAME
    if not path.is_file():
        return path, {}
    from run_cycle import CycleDriverError, load_config
    try:
        return path, load_config(path)
    except CycleDriverError as error:
        raise ProfileConfigError(str(error)) from error


def _probe(enabled: bool):
    if not enabled:
        return None
    from executors import Registry
    return Registry().probe_all()


def show_report(config: dict, probes=None) -> str:
    declared = declared_profiles(config)
    spec = full_spec(load_profiles(config))
    source = "declared in .code-cycle.yml" if declared else "all toolkit defaults"
    return "\n\n".join((
        f"## Effective routing profiles ({source})",
        render_table(effective_rows(spec, declared)),
        render_executors(probes),
    )) + "\n"


def proposal_report(proposal: Proposal, path: Path, probes=None) -> str:
    title = proposal.preset.title if proposal.preset else "Custom"
    parts = [f"## Proposed routing profiles: {title}"]
    block = render_block(proposal.declaration)
    parts.append(f"Block for `{path}`:\n\n```yaml\ncode_cycle:\n{block}```"
                 if block else f"No profiles block: `{path}` would use the defaults.")
    parts.append(render_table(effective_rows(proposal.spec, proposal.declaration)))
    parts.append(render_executors(probes))
    for heading, items in (("Errors", proposal.errors),
                           ("Warnings", proposal.warnings),
                           ("Notes", proposal.notes)):
        if items:
            parts.append(f"{heading}:\n" + "\n".join(f"- {item}" for item in items))
    return "\n\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="profile_config",
        description="Show, propose and write code_cycle.profiles.")
    parser.add_argument("command", choices=("show", "propose", "write", "presets"))
    parser.add_argument("--cwd", default=".", help="repository root")
    parser.add_argument("--config", default=None, help=f"path to {CONFIG_NAME}")
    parser.add_argument("--preset", default=None, choices=sorted(PRESETS))
    parser.add_argument("--set", dest="overrides", action="append", default=[],
                        metavar="PROFILE.KEY=TARGET",
                        help="override one target; 'none' removes a fallback")
    parser.add_argument("--allow-cross-split", action="store_true",
                        help="accept a target that crosses a balanced preset's split")
    parser.add_argument("--confirmed", action="store_true",
                        help="required by write: the user approved this exact proposal")
    parser.add_argument("--no-probe", action="store_true",
                        help="do not check which executors are installed")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    args = parser.parse_args(argv)

    try:
        if args.command == "presets":
            payload = {name: {"title": p.title, "profiles": p.profiles,
                              "notes": list(p.notes)} for name, p in PRESETS.items()}
            print(json.dumps(payload, indent=2) if args.format == "json" else "\n".join(
                f"- `{name}`: {p.title}" for name, p in PRESETS.items()))
            return 0
        path, config = _load(args.cwd, args.config)
        probes = _probe(not args.no_probe)
        if args.command == "show":
            if args.format == "json":
                spec = full_spec(load_profiles(config))
                print(json.dumps(effective_rows(spec, declared_profiles(config)), indent=2))
            else:
                print(show_report(config, probes), end="")
            return 0
        base = full_spec(load_profiles(config))
        proposal = build_proposal(
            base, preset=args.preset, overrides=args.overrides,
            allow_cross_split=args.allow_cross_split, codex_catalog=codex_models())
        proposal.warnings.extend(executor_warnings(proposal.spec, probes))
        if args.command == "propose" or proposal.errors:
            if args.format == "json":
                print(json.dumps({
                    "declaration": proposal.declaration,
                    "effective": effective_rows(proposal.spec, proposal.declaration),
                    "errors": proposal.errors, "warnings": proposal.warnings,
                    "notes": proposal.notes}, indent=2))
            else:
                print(proposal_report(proposal, path, probes), end="")
            if proposal.errors:
                return 1
            return 0
        if not args.confirmed:
            print("write needs --confirmed: show the proposal and get approval first",
                  file=sys.stderr)
            return 2
        write_declaration(path, proposal.declaration)
        print(f"Wrote code_cycle.profiles to {path}.")
        return 0
    except (ProfileConfigError, RouterError) as error:
        print(f"profile_config: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
