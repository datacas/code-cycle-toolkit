"""Sensitive sandbox grants: what a stage asks for, what it really opens, who agreed.

A stage that hits a sandbox restriction reports an `environment_blocker`. When
that blocker names a resource registered here, the runtime does not widen the
sandbox on the agent's word. It asks the person, or uses a decision the person
already made, and it says what the permission really opens on this executor and
platform, which may be more than the resource asked for: Codex on Linux cannot
allow the Docker socket alone, only every local Unix socket.

Three answers exist. *This cycle* lives in this process only, applies to the
remaining dispatches of the cycle, and is never written down, so it cannot reach
another session. It is offered only where the executor applies the grant per
dispatch. *Persistent* is stored per user, outside every checkout, keyed by the
repository and by a fingerprint of the real scope: when the scope changes, the
stored answer no longer applies and the person is asked again. *Deny* is also
stored for the exact scope until revoked; checks needing it stay unverified.

Without a person to ask, nothing is widened: the cycle stops and carries the
question, and a later run applies the answer through `--grant`.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import sys
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

#: Answers a person can give, as `--grant ID=<answer>` spells them.
ALLOW_CYCLE = "cycle"
ALLOW_PERSISTENT = "persistent"
ALLOW_GLOBAL = "forever"
DENY = "deny"
ANSWERS = (ALLOW_CYCLE, ALLOW_PERSISTENT, ALLOW_GLOBAL, DENY)

#: What `ConsentBroker.request` can return besides an answer.
PENDING = "pending"
UNSUPPORTED = "unsupported"

STORE_SCHEMA = 2

#: The source of a `--grant` answer kept for this run only, never stored.
RUN_ONLY_SOURCE = "--grant (this run)"


class GrantError(ValueError):
    """A grant request, answer, or stored decision the runtime refuses."""


@dataclass(frozen=True)
class GrantScope:
    """What one grant opens for one executor on one platform."""

    executor: str
    platform: str
    #: The real reach of the permission, which may exceed the resource.
    effective: str
    risks: str
    #: Whether the executor applies the grant to one dispatch and nothing else,
    #: which is what makes a temporary answer safe to offer.
    isolated_per_dispatch: bool


@dataclass(frozen=True)
class SensitiveGrant:
    id: str
    resource: str
    reason: str
    scopes: tuple[GrantScope, ...]
    #: Words in a blocker's command or error that name this resource, for a
    #: blocker that does not carry `resource` itself.
    terms: tuple[str, ...] = ()
    operation: dict[str, Any] | None = None

    def scope_for(self, executor: str, platform: str) -> GrantScope | None:
        return next((scope for scope in self.scopes
                     if scope.executor == executor and scope.platform == platform), None)

    def matches(self, blocker: dict) -> bool:
        if blocker.get("resource") == self.id:
            return True
        if blocker.get("kind") != "sandbox_local_access":
            return False
        text = f"{blocker.get('command', '')} {blocker.get('error', '')}".lower()
        return any(term in text for term in self.terms)

    def fingerprint(self, scope: GrantScope) -> str:
        """Identify what a stored answer agreed to, so a wider scope asks again."""
        material = json.dumps([self.id, scope.executor, scope.platform, scope.effective],
                              ensure_ascii=True)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


COMMAND_OPTIONS = ["this cycle", "this repository", "forever", "deny"]
#: Roles that review untrusted changes: a global grant never reaches them.
NO_GLOBAL_ROLES = frozenset({"review", "rereview"})


def consent_role(role: str | None) -> str | None:
    """Review and rereview share one decision; `stage_permissions` keeps the same rule."""
    return "review" if role in NO_GLOBAL_ROLES else role


def command_options(role: str, *, unique_path: str | None = None) -> list[str]:
    """The scopes offered for an exact operation.

    An operation naming a path unique to one dispatch or session can never
    match again, so only this cycle is offered; a pattern grant stays
    available as an edited answer (#439 AC-14).
    """
    if unique_path:
        return ["this cycle", "deny"]
    return [option for option in COMMAND_OPTIONS
            if option != "forever" or role not in NO_GLOBAL_ROLES]


def unique_path(op: dict[str, Any] | None) -> str | None:
    """The dispatch- or session-unique path an exact operation names, if any."""
    from stage_permissions import dispatch_unique_path
    return dispatch_unique_path(op)


def identifier(op: dict[str, Any]) -> str:
    return "command_" + hashlib.sha256(json.dumps(
        op, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


PATTERN_COMMAND_LIMIT = 4096
PATTERN_LIMIT = 1024


def pattern_matches(pattern: str, command: str) -> bool:
    """Run Python's regex engine in a time-bounded process, never in the hook.

    Structural validation rejects dangerous forms at creation; the process
    budget also covers ambiguous alternatives and adjacent repetitions.
    No shell is involved and no command being matched is executed.
    """
    if len(command) > PATTERN_COMMAND_LIMIT or len(pattern) > PATTERN_LIMIT:
        return False
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c",
             "import json,re,sys; p,c=json.load(sys.stdin); print(int(re.fullmatch(p,c) is not None))"],
            input=json.dumps([pattern, command]), text=True, capture_output=True, timeout=0.5)
        return result.returncode == 0 and result.stdout.strip() == "1"
    except (OSError, subprocess.TimeoutExpired):
        return False


def validate_pattern(pattern: str, positive: list[str], negative: list[str]) -> None:
    """Validate before asking or saving, with an actionable rejection reason."""
    if not isinstance(pattern, str) or not pattern.strip():
        raise GrantError("pattern is empty")
    if len(pattern) > PATTERN_LIMIT:
        raise GrantError(f"pattern exceeds {PATTERN_LIMIT} characters")
    try:
        compiled = re.compile(pattern)
        # Python's own regex AST avoids guessing repetition from escaped text.
        # Python 3.11+ keeps the parser in the re package; the sre_* shims are
        # deprecated and absent from newer interpreters.
        try:
            _parser = importlib.import_module("re._parser")
            _constants = importlib.import_module("re._constants")
        except ImportError:
            _parser = importlib.import_module("sre_parse")
            _constants = importlib.import_module("sre_constants")
        tree = _parser.parse(pattern, 0)
        repeats = {_constants.MAX_REPEAT, _constants.MIN_REPEAT}
        if (possessive := getattr(_constants, "POSSESSIVE_REPEAT", None)) is not None:
            repeats.add(possessive)

        def inspect(nodes, repeated=False):
            for kind, value in nodes:
                if kind in repeats:
                    minimum, maximum, children = value
                    if repeated:
                        raise GrantError("nested repetition can cause catastrophic backtracking")
                    if maximum != _constants.MAXREPEAT and maximum > PATTERN_COMMAND_LIMIT:
                        raise GrantError("pattern repetition exceeds command length limit")
                    inspect(children, True)
                elif kind == _constants.SUBPATTERN:
                    inspect(value[-1], repeated)
                elif kind == _constants.BRANCH:
                    if repeated:
                        raise GrantError("repeated alternatives can cause catastrophic backtracking")
                    for branch in value[1]:
                        inspect(branch, repeated)
                elif kind in {_constants.GROUPREF, _constants.GROUPREF_EXISTS,
                              _constants.ASSERT, _constants.ASSERT_NOT}:
                    raise GrantError("backreferences and lookarounds are not bounded pattern grants")
        inspect(tree)
    except (re.error, OverflowError) as error:
        raise GrantError(f"invalid pattern: {error}") from error
    if compiled.fullmatch("") is not None:
        raise GrantError("pattern matches the empty command")
    if (not isinstance(positive, list) or not isinstance(negative, list)
            or not positive or not negative):
        raise GrantError("positive and negative example commands must be non-empty lists")
    if any(not isinstance(item, str) or not item
                                           for item in [*positive, *negative]):
        raise GrantError("positive and negative example commands are required")
    for example in positive:
        if not pattern_matches(pattern, example):
            raise GrantError(f"positive example does not match within the budget: {example}")
    for example in negative:
        if len(example) > PATTERN_COMMAND_LIMIT or pattern_matches(pattern, example):
            raise GrantError(f"negative example matches or exceeds the length limit: {example}")


def make_pattern_grant(pattern: str, role: str, executor: str, platform: str, *,
                       proposer: str, justification: str, positive: list[str],
                       negative: list[str], compound: bool = False) -> SensitiveGrant:
    if not isinstance(compound, bool):
        raise GrantError("compound must be a boolean")
    validate_pattern(pattern, positive, negative)
    if not proposer or not justification:
        raise GrantError("a pattern needs its proposer and justification")
    op = {"version": 2, "role": consent_role(role), "tool": "Bash",
          "pattern": {"regex": pattern}, "proposer": proposer,
          "justification": justification, "positive": positive, "negative": negative}
    if compound:
        op["compound"] = True
    return make_grant(op, executor, platform)


def pattern_covers_operation(pattern: dict[str, Any], op: dict[str, Any]) -> bool:
    """A simple-command matcher never implicitly authorizes shell source."""
    from stage_permissions import normalized_command
    shell = op.get("pattern", {}).get("words", [])[:1] == ["<SHELL>"]
    return (not shell or pattern.get("compound") is True) and pattern_matches(
        pattern.get("pattern", {}).get("regex", ""), normalized_command(op))



def _command_program(op: dict[str, Any]) -> str | None:
    """The program a Bash operation runs, after its environment prefixes."""
    for word in op.get("pattern", {}).get("words", []):
        if isinstance(word, dict) and "env" in word:
            continue
        if isinstance(word, str) and re.match(r"[A-Za-z_][A-Za-z0-9_]*=", word):
            continue
        return os.path.basename(word).removesuffix(".exe") if isinstance(word, str) else None
    return None


def compose_warnings(op: dict[str, Any] | None) -> list[str]:
    compose = (op or {}).get("compose")
    warnings = compose.get("warnings") if isinstance(compose, dict) else None
    return [str(item) for item in warnings] if isinstance(warnings, list) else []


def make_grant(op: dict[str, Any], executor: str, platform: str) -> SensitiveGrant:
    pattern = json.dumps(op, sort_keys=True)
    program = _command_program(op) if op.get("tool") == "Bash" else "edit"
    risks = ("Destructive deletion can permanently remove data." if program in {"rm", "rmdir", "unlink"}
             else "Modifies the exact edit target." if program == "edit"
             else "Docker access is in practice root on the host." if program == "docker"
             else "May read, change or execute data with the user's privileges.")
    warnings = compose_warnings(op)
    if warnings:
        risks += " Compose warnings: " + "; ".join(warnings) + "."
    if program == "docker":
        risks += (" The hook recomputes the effective Compose configuration before each run; "
                  "a changed configuration asks again.") if op.get("compose") else ""
    elif program != "edit":
        # Indirect Docker (#398): the hook sees this command, never what it starts.
        risks += (" With docker_host granted, this command and anything it starts reach "
                  "Docker without a per-operation check.")
    risks += (" An interpreter's internal operations cannot be inspected by the hook; "
              "only external isolation enforces restrictions inside it.")
    if op.get("version") == 2:
        risks += (" A regex grants every matching normalized command, including interpreters. "
                  "Git, publication, credential and redirect guards still apply to each command; "
                  "a match grants nothing for a protected command.")
        if op.get("compound"):
            risks += " Compound pattern: authorizes every segment of matching shell source."
    return SensitiveGrant(
        identifier(op), "the command pattern" if op.get("version") == 2 else "the exact stage operation", "outside the stage table",
        (GrantScope(executor, platform, pattern, risks, True),),
        operation=op)


#: The placeholder shapes a stored Bash word may take (#398).
_WORD_SHAPES = ({"tmpdir"}, {"checkout"}, {"env", "tmpdir"}, {"env", "checkout"},
                {"option", "tmpdir"}, {"option", "checkout"}, {"tool_results"},
                {"env", "tool_results"}, {"option", "tool_results"})


def _valid_word(word: Any) -> bool:
    return isinstance(word, str) or (
        isinstance(word, dict) and set(word) in _WORD_SHAPES
        and all(isinstance(value, str) for value in word.values()))


def from_entry(entry: dict[str, Any]) -> SensitiveGrant | None:
    op = entry.get("operation")
    if isinstance(op, dict) and op.get("version") == 2:
        try:
            if op.get("tool") != "Bash" or not isinstance(op.get("role"), str):
                return None
            return make_pattern_grant(op["pattern"]["regex"], op["role"],
                                      str(entry.get("executor")), str(entry.get("platform")),
                                      proposer=op["proposer"], justification=op["justification"],
                                      positive=op["positive"], negative=op["negative"],
                                      compound=op.get("compound", False))
        except (GrantError, KeyError, TypeError, AttributeError):
            return None
    if (not isinstance(op, dict) or op.get("version") != 1
            or not isinstance(op.get("role"), str) or not isinstance(op.get("pattern"), dict)
            or op.get("tool") not in {"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit"}
            or not set(op) <= {"version", "role", "tool", "pattern", "compose", "check"}
            or ("check" in op and not isinstance(op["check"], str))
            or ("compose" in op and not (isinstance(op["compose"], dict)
                                         and isinstance(op["compose"].get("config_sha256"), str)))):
        return None
    pattern = op["pattern"]
    if op["tool"] == "Bash":
        words = pattern.get("words")
        if (not isinstance(words, list) or not words
                or any(not _valid_word(word) for word in words)
                or _command_program(op) is None
                or not set(pattern) <= {"words", "redirects", "cwd"}
                or ("cwd" in pattern and not isinstance(pattern["cwd"], (str, type(None))))
                or not isinstance(pattern.get("redirects"), list)):
            return None
    elif not isinstance(pattern.get("target"), str):
        return None
    return make_grant(op, str(entry.get("executor")), str(entry.get("platform")))



def refusal_question(grant: SensitiveGrant, scope: GrantScope, role: str,
                     question_id: str, blocker: dict[str, Any] | None) -> dict[str, Any]:
    command = str((blocker or {}).get("command", ""))
    reason = str((blocker or {}).get("error", grant.reason))
    op = grant.operation or {}
    warnings = compose_warnings(op)
    # A disclosed host access comes before the generic risk, so it is never cut.
    warned = f"WARNING: {'; '.join(warnings)[:160]}. " if warnings else ""
    if (blocker or {}).get("unresolved_directory"):
        warned += ("WARNING: a directory change cannot be resolved; relative operands are "
                   "checked only for credential-store names. ")
    check = op.get("check")
    skip = (f"Alternative: skip; check {str(check)[:60]} stays unverified and the cycle continues. "
            if check else "Alternative: skip; affected checks stay not_verified. ")
    configured = str((blocker or {}).get("configured_command") or "")
    detail = (f" Configured command: {configured}." if configured else "")
    regex = op.get("pattern", {}).get("regex")
    compound = ("Compound pattern: all segments of matching shell source are authorized. "
                if op.get("compound") else "")
    regex_note = f" Regex: {regex[:80]}." if regex else ""
    unique = unique_path(op)
    # Said before the generic text, so the 600-character cut never drops it.
    scope_note = (f" Offered for this cycle or as a pattern grant only: {unique[-60:]} is unique "
                  "to one dispatch or session, so a repository or forever grant could never "
                  "match again." if unique else "")
    return {"id": question_id,
            "prompt": (f"{role}: {compound}{command[:140]}.{detail}{scope_note} {warned}"
                       "Pattern grants never bypass Git, credential or redirect guards. "
                       "Interpreter internals require external isolation. "
                       f"{regex_note} {skip}"
                       f"Outside table: {reason[:40]}. Risk: {scope.risks[:60]} "
                       "Pro: finish this operation. Con: extra access persists for the selected scope. "
                       "Protected commands receive no pattern authorization.")[:600],
            "options": command_options(str((grant.operation or {}).get("role", role)),
                                       unique_path=unique),
            "recommended": None,
            "blocks": [f"grant:{grant.id}"]}



DOCKER_HOST = SensitiveGrant(
    id="docker_host",
    resource="the host Docker daemon",
    reason="a stage's checks need Docker, such as containers or docker compose",
    scopes=(
        GrantScope(
            executor="codex", platform="linux",
            effective=("every local Unix socket the user can reach, not only "
                       "Docker's: also ssh-agent, D-Bus and any other local "
                       "service socket. Codex on Linux cannot allow one socket alone"),
            risks=("Docker acts as root on the host, and ssh-agent can sign "
                   "with your keys. Network domains and read-only review "
                   "checkouts stay as they are"),
            isolated_per_dispatch=True,
        ),
        GrantScope(
            executor="claude", platform="linux",
            effective=("every reachable Unix socket, including Docker, ssh-agent, "
                       "D-Bus and other local service sockets, for the whole dispatch; "
                       "Claude's command hook permits docker compose up, down, ps, logs "
                       "and build, and asks a separate exact-command consent for "
                       "compose run and every other Docker command"),
            risks=("Access to the Docker daemon is in practice root on the host, and "
                   "ssh-agent can sign with your keys. A consented command that runs "
                   "Docker indirectly reaches it without a per-operation check"),
            isolated_per_dispatch=True,
        ),
    ),
    terms=("docker",),
)

GRANTS: dict[str, SensitiveGrant] = {grant.id: grant for grant in (DOCKER_HOST,)}


def current_platform() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt":
        return "windows"
    return sys.platform


def parse_grant_options(values: list[str] | tuple[str, ...]) -> dict[str, str]:
    """Read repeated `--grant ID=cycle|persistent|deny` options."""
    answers: dict[str, str] = {}
    for value in values:
        grant_id, separator, answer = value.partition("=")
        if (not separator or (grant_id not in GRANTS and not re.fullmatch(r"command_[0-9a-f]{64}", grant_id))
                or answer not in ANSWERS or (answer == ALLOW_GLOBAL and grant_id in GRANTS)):
            raise GrantError(
                f"--grant must be ID={'|'.join(ANSWERS)} with ID one of "
                f"{', '.join(sorted(GRANTS))}, not {value!r}")
        if answers.get(grant_id, answer) != answer:
            raise GrantError(f"--grant {grant_id} was given two different answers")
        answers[grant_id] = answer
    return answers


def default_store_path() -> Path:
    override = os.environ.get("CODE_CYCLE_CONSENT_FILE")
    if override:
        return Path(override)
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "code-cycle" / "consents.json"


class ConsentStore:
    """Persistent answers, per user and outside every checkout."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else default_store_path()

    def _read(self) -> dict:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema": STORE_SCHEMA, "repos": {}}
        except (OSError, ValueError) as error:
            raise GrantError(f"{self.path}: unreadable consent store: {error}") from error
        if (not isinstance(payload, dict) or payload.get("schema") not in (1, STORE_SCHEMA)
                or not isinstance(payload.get("repos"), dict)):
            raise GrantError(f"{self.path}: not a consent store this runtime can read")
        if payload["schema"] == 1:
            payload["repos"] = {
                repo: {grant_id: {entry["fingerprint"]: entry}
                       for grant_id, entry in entries.items()
                       if isinstance(entry, dict) and isinstance(entry.get("fingerprint"), str)}
                for repo, entries in payload["repos"].items() if isinstance(entries, dict)
            }
            payload["schema"] = STORE_SCHEMA
        if any(not isinstance(entries, dict) or any(not isinstance(scopes, dict)
                   for scopes in entries.values()) for entries in payload["repos"].values()):
            raise GrantError(f"{self.path}: malformed repository decisions")
        if not isinstance(payload.get("revocations", []), list):
            raise GrantError(f"{self.path}: malformed revocation history")
        return payload

    def load(self, repo_id: str, grant: SensitiveGrant, scope: GrantScope) -> str | None:
        """`allow`, `deny`, or None (UNKNOWN), for this exact scope only."""
        payload = self._read()
        entries = (payload["repos"].get(repo_id) or {}).get(grant.id, {})
        entry = entries.get(grant.fingerprint(scope)) if isinstance(entries, dict) else None
        operation = getattr(grant, "operation", None)
        if (entry is None and operation and not repo_id.startswith("cycle:")
                and operation.get("role") not in NO_GLOBAL_ROLES):
            entry = (payload["repos"].get("*") or {}).get(grant.id, {}).get(grant.fingerprint(scope))
        if (not isinstance(entry, dict)
                or entry.get("fingerprint") != grant.fingerprint(scope)
                or entry.get("decision") not in ("allow", "deny")):
            return None
        return entry["decision"]

    def allowed_scopes(self, repo_id: str) -> dict[str, str | None]:
        """Each scope the store allows for the repository now, with when it was decided.

        An entry counts only while its fingerprint still matches the real
        scope of its grant, the rule `load` applies. Raises `GrantError` when
        the file cannot be read or validated, so a caller infers nothing from it.
        """
        return {f"{entry['grant']}: {entry['effective_scope']}": entry.get("decided_at")
                for entry in self.list(repo_id) if entry["status"] == "GRANTED"
                and entry["repo"] in {"*", repo_id}}

    def list(self, repo_id: str) -> list[dict[str, Any]]:
        """Current scope-valid decisions. History never authorizes access."""
        decisions = []
        payload = self._read()
        sources = list(dict.fromkeys(("*", repo_id)))
        for key in payload["repos"]:
            try:
                identity = json.loads(key.removeprefix("cycle:")) if key.startswith("cycle:") else None
            except ValueError:
                continue
            if isinstance(identity, list) and len(identity) == 3 and identity[0] == repo_id:
                sources.append(key)
        for source, grant_id, entries in (
                (source, grant_id, entries) for source in dict.fromkeys(sources)
                for grant_id, entries in (payload["repos"].get(source) or {}).items()):
            if not isinstance(entries, dict):
                continue
            for fingerprint, entry in entries.items():
                if not isinstance(entry, dict) or entry.get("decision") not in ("allow", "deny"):
                    continue
                grant = GRANTS.get(grant_id) or from_entry(entry)
                if grant is None or grant.id != grant_id:
                    continue
                scope = grant.scope_for(str(entry.get("executor")), str(entry.get("platform")))
                if (scope is None or fingerprint != grant.fingerprint(scope)
                        or entry.get("fingerprint") != fingerprint):
                    continue
                decisions.append({**entry, "grant": grant_id, "source": "consents.json", "repo": source,
                                  "status": "GRANTED" if entry["decision"] == "allow" else "DENIED",
                                  "effective_scope": scope.effective})
        return decisions

    def cycle_for(self, repo_id: str, task_id: str) -> str | None:
        """Recover the latest command-consent lineage when resuming this work item."""
        candidates = []
        for entry in self.list(repo_id):
            source = entry["repo"]
            if not source.startswith("cycle:"):
                continue
            try:
                identity = json.loads(source.removeprefix("cycle:"))
            except ValueError:
                continue
            if isinstance(identity, list) and len(identity) == 3 and identity[:2] == [repo_id, task_id]:
                candidates.append((entry["decided_at"], identity[2]))
        return max(candidates)[1] if candidates else None

    def revoke(self, repo_id: str, grant: SensitiveGrant, scope: GrantScope) -> bool:
        """Return a stored decision to UNKNOWN and retain a separate audit record."""
        payload = self._read()
        entries = (payload["repos"].get(repo_id) or {}).get(grant.id, {})
        entry = entries.pop(grant.fingerprint(scope), None)
        if entry is None:
            return False
        payload.setdefault("revocations", []).append({
            "repo": repo_id, "grant": grant.id, **entry,
            "revoked_at": datetime.now(timezone.utc).isoformat(),
        })
        self._write(payload)
        return True

    def save(self, repo_id: str, grant: SensitiveGrant, scope: GrantScope,
             decision: str) -> None:
        if decision not in ("allow", "deny"):
            raise GrantError(f"invalid stored decision {decision!r}")
        payload = self._read()
        payload["repos"].setdefault(repo_id, {}).setdefault(grant.id, {})[grant.fingerprint(scope)] = {
            "decision": decision,
            "fingerprint": grant.fingerprint(scope),
            "executor": scope.executor,
            "platform": scope.platform,
            "decided_at": datetime.now(timezone.utc).isoformat(),
            **({"operation": grant.operation} if getattr(grant, "operation", None) else {}),
            "user_approval": {"decision": decision, "source": "consent answer"},
            "executed_commands": [],
        }
        self._write(payload)

    def record_execution(self, repo_id: str, grant: SensitiveGrant, scope: GrantScope,
                         command: str) -> None:
        """Append observed execution evidence; never creates authorization."""
        payload = self._read()
        entries = (payload["repos"].get(repo_id) or {}).get(grant.id, {})
        entry = entries.get(grant.fingerprint(scope))
        if not isinstance(entry, dict) or entry.get("decision") != "allow":
            raise GrantError("cannot record execution without an existing approved grant")
        entry.setdefault("executed_commands", []).append({
            "command": command, "at": datetime.now(timezone.utc).isoformat()})
        self._write(payload)

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".consents-", dir=self.path.parent)
        try:
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


def question(grant: SensitiveGrant, scope: GrantScope, *, role: str,
             question_id: str = "Q-001", blocker: dict[str, Any] | None = None) -> dict:
    """The consent question, in the shape `stop_questions` defines."""
    if getattr(grant, "operation", None):
        return refusal_question(grant, scope, role, question_id, blocker)
    options = []
    if scope.isolated_per_dispatch:
        options.append(f"Allow for this cycle ({ALLOW_CYCLE})")
    options += [f"Allow persistently for this repository ({ALLOW_PERSISTENT})",
                f"Deny ({DENY})", f"Use exact-HEAD CI; leave local checks unverified ({DENY})"]
    # The answer is not per stage: say who else receives it.
    operation = str((blocker or {}).get("command") or grant.reason)[:60]
    # Full scope/risks and unabridged operation accompany the request record.
    effective = scope.effective if len(scope.effective) <= 120 else scope.effective[:117] + "..."
    risks = scope.risks if len(scope.risks) <= 120 else scope.risks[:117] + "..."
    prompt = (f"The {role} stage needs {grant.resource} for {operation}. "
              f"Alternative: exact-HEAD CI; local checks stay unverified. "
              f"Opens {effective}. Risk: {risks}. {reach(scope)}")
    return {
        "id": question_id,
        "prompt": prompt[:597] + "..." if len(prompt) > 600 else prompt,
        "options": options,
        "recommended": None,
        "blocks": [f"grant:{grant.id}"],
    }


def reach(scope: GrantScope) -> str:
    """Which dispatches an allow reaches, beyond the stage that asked."""
    return (f"An allow reaches every later {scope.executor} stage of the cycle "
            "(of the repository, if persistent), including reviews of untrusted "
            "pull requests.")


def answer_from_option(option: str) -> str | None:
    """Map a chosen option back to its answer token."""
    if option == "this cycle":
        return ALLOW_CYCLE
    if option == "this repository":
        return ALLOW_PERSISTENT
    for answer in ANSWERS:
        if option.rstrip().endswith(f"({answer})") or option.strip() == answer:
            return answer
    return None


@dataclass
class PermissionRequest:
    grant: SensitiveGrant
    scope: GrantScope
    role: str
    question: dict
    blocker: dict[str, Any] | None = None

    def record(self) -> dict:
        op = self.grant.operation or {}
        return {
            "grant": self.grant.id, "resource": self.grant.resource,
            "executor": self.scope.executor, "platform": self.scope.platform,
            "effective_scope": self.scope.effective, "risks": self.scope.risks,
            "temporary_offered": self.scope.isolated_per_dispatch,
            "role": self.role, "answer_with": f"--grant {self.grant.id}=<answer>",
            "operation": (self.blocker or {}).get("command", self.grant.reason),
            "observed_error": (self.blocker or {}).get("error"),
            "alternative": "exact-HEAD CI; local checks remain not_verified",
            **({"pattern": self.grant.operation,
                "pros": "finish the requested operation",
                "cons": "extra user-level access for the selected scope",
                "global_never_covers": sorted(NO_GLOBAL_ROLES),
                **({"dispatch_unique_path": unique_path(op), "offered_scope": "cycle or pattern grant"}
                   if unique_path(op) else {}),
                **({"warnings": compose_warnings(op)} if compose_warnings(op) else {}),
                **({"check": op["check"],
                    "alternative": "skip; the check is recorded unverified and the cycle continues"}
                   if op.get("check") else {})}
               if op else {}),
        }


@dataclass
class ConsentBroker:
    """Decide, for one cycle, which sensitive grants its dispatches receive."""

    repo_id: str
    platform: str = field(default_factory=current_platform)
    store: ConsentStore | None = None
    #: Answers given for this run, from `--grant`.
    answers: dict[str, str] = field(default_factory=dict)
    #: Grants the project configuration authorizes in advance.
    preauthorized: frozenset[str] = frozenset()
    #: Asks a person and returns an answer token, or None when nobody can answer.
    prompt: Callable[[dict], str | dict[str, Any] | None] | None = None
    granted_ids: set[str] = field(default_factory=set)
    denied_ids: set[str] = field(default_factory=set)
    pending: list[PermissionRequest] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Best-effort audit observer; never used to authorize a grant.
    observer: Callable[..., None] | None = None
    #: Scope fingerprints of the questions a person was shown for this work
    #: item. Only those scopes keep a `--grant` answer beyond the run.
    asked: frozenset[str] = frozenset()
    _cycle_answers: dict[str, str] = field(default_factory=dict)
    _consumed_answers: set[str] = field(default_factory=set)
    _observed: dict[str, str | None] = field(default_factory=dict)
    operations: dict[str, SensitiveGrant] = field(default_factory=dict)
    cycle_id: str | None = None
    task_id: str | None = None

    @property
    def cycle_scope(self) -> str | None:
        if self.cycle_id and self.task_id:
            return "cycle:" + json.dumps([self.repo_id, self.task_id, self.cycle_id])
        return f"cycle:{self.cycle_id}:{self.repo_id}" if self.cycle_id else None

    @property
    def registry(self) -> dict[str, SensitiveGrant]:
        if self.store is not None:
            try:
                entries = self.store.list(self.repo_id) + (
                    self.store.list(self.cycle_scope) if self.cycle_scope else [])
            except GrantError as error:
                self.notes.append(f"{error}; no stored authorization applied")
                entries = []
            for entry in entries:
                grant = from_entry(entry)
                if grant is not None:
                    previous = self.operations.get(grant.id)
                    if previous is not None:
                        from dataclasses import replace
                        grant = replace(grant, scopes=tuple(dict.fromkeys((*previous.scopes, *grant.scopes))))
                    self.operations[grant.id] = grant
        return {**GRANTS, **self.operations}

    def command_entries(self, executor: str, role: str) -> tuple[dict[str, Any], ...]:
        entries = []
        for grant in self.registry.values():
            op = getattr(grant, "operation", None)
            scope = grant.scope_for(executor, self.platform)
            if op and consent_role(op["role"]) == consent_role(role) and scope is not None:
                answer, _ = self._resolve(grant, scope)
                if answer is not None:
                    entries.append({"operation": op, "decision": "deny" if answer == DENY else "allow"})
        return tuple(entries)

    def record_executions(self, commands: list[str], executor: str, policy: Any,
                          cwd: str | None, *, observed: list[dict[str, Any]] | None = None) -> None:
        from stage_permissions import operation, protected_reason
        if self.store is None:
            return
        pattern_grants = [grant for grant in self.registry.values()
                          if (grant.operation or {}).get("version") == 2]
        if not pattern_grants:
            return
        retained = {record["command"]: record["operation"] for record in observed or []
                    if isinstance(record, dict) and isinstance(record.get("command"), str)
                    and isinstance(record.get("operation"), dict)}
        for command in commands:
            if command not in retained and protected_reason("Bash", {"command": command}, policy, cwd):
                continue
            try:
                op = retained.get(command) or operation("Bash", {"command": command}, policy, cwd)
                for grant in pattern_grants:
                    stored = grant.operation or {}
                    scope = grant.scope_for(executor, self.platform)
                    if (stored.get("version") != 2 or scope is None
                            or consent_role(stored.get("role")) != consent_role(policy.role)
                            or not pattern_covers_operation(stored, op)):
                        continue
                    for repo in (self.cycle_scope, self.repo_id, "*"):
                        if repo and self.store.load(repo, grant, scope) == "allow":
                            try:
                                self.store.record_execution(repo, grant, scope, command)
                                break
                            except GrantError:
                                continue
            except (ValueError, OSError, TypeError) as error:
                self.notes.append(f"execution audit unavailable: {type(error).__name__}")

    def _observe(self, kind, grant, scope, role, answer=None, source=None) -> None:
        if self.observer is not None:
            try:
                self.observer(kind, grant, scope, role, answer, source)
            except Exception as error:
                self.notes.append(f"consent audit failed ({type(error).__name__})")

    def grant_for(self, blocker: dict) -> SensitiveGrant | None:
        return next((grant for grant in self.registry.values() if grant.matches(blocker)), None)

    def granted(self, executor: str) -> tuple[str, ...]:
        """Revalidate effective decisions immediately before each dispatch."""
        granted, denied = set(), set()
        for grant in self.registry.values():
            scope = grant.scope_for(executor, self.platform)
            if scope is None:
                continue
            answer, source = self._resolve(grant, scope)
            if answer in (ALLOW_CYCLE, ALLOW_PERSISTENT, ALLOW_GLOBAL):
                granted.add(grant.id)
            elif answer == DENY:
                denied.add(grant.id)
            # Audit a change of the effective decision, not every revalidation.
            key = grant.fingerprint(scope)
            if self._observed.get(key) != answer:
                self._observed[key] = answer
                if answer is not None:
                    self._observe("decision_answered", grant, scope, "dispatch", answer, source)
        self.granted_ids, self.denied_ids = granted, denied
        return tuple(sorted(granted))

    def _resolve(self, grant: SensitiveGrant, scope: GrantScope) -> tuple[str | None, str | None]:
        key = grant.fingerprint(scope)
        if grant.id in self.answers and key not in self._consumed_answers:
            answer = self.answers[grant.id]
            # The answer persists only for the scope its question showed; every
            # other registered scope receives it for this run alone.
            persist = key in self.asked
            self.apply_answer(grant, scope, answer, persist=persist)
            self._consumed_answers.add(key)
            if persist or answer == ALLOW_CYCLE:
                return answer, "--grant"
            self.notes.append(f"{grant.id}={answer} kept for this run only on "
                              f"{scope.executor}/{scope.platform}: no earlier question "
                              "showed this scope")
            return answer, RUN_ONLY_SOURCE
        # Configuration remains a separate explicit source of authorization.
        if grant.id in self.preauthorized:
            return ALLOW_PERSISTENT, "project configuration"
        if key in self._cycle_answers:
            if (getattr(grant, "operation", None) and self.cycle_scope and self.store
                    and self._cycle_answers[key] == ALLOW_CYCLE
                    and self.store.load(self.cycle_scope, grant, scope) is None):
                self._cycle_answers.pop(key)
            else:
                return self._cycle_answers[key], "cycle"
        if getattr(grant, "operation", None) and self.cycle_scope and self.store:
            stored = self.store.load(self.cycle_scope, grant, scope)
            if stored is not None:
                return (ALLOW_CYCLE if stored == "allow" else DENY), "cycle"
        if self.store is not None:
            try:
                stored = self.store.load(self.repo_id, grant, scope)
            except GrantError as error:
                self.notes.append(f"{error}; no stored authorization applied")
                return None, None
            if stored is not None:
                return (ALLOW_PERSISTENT if stored == "allow" else DENY), "consents.json"
        return None, None

    def listing(self, executor: str) -> list[dict[str, Any]]:
        """Stored decisions plus the effective authorization and its source."""
        stored = self.store.list(self.repo_id) if self.store is not None else []
        for grant in self.registry.values():
            scope = grant.scope_for(executor, self.platform)
            if scope is None:
                continue
            answer, source = self._resolve(grant, scope)
            stored.append({"grant": grant.id, "executor": executor, "platform": self.platform,
                           "effective_scope": scope.effective, "source": source,
                           "effective": True, "status": "UNKNOWN" if answer is None else
                           "DENIED" if answer == DENY else "GRANTED"})
        return stored

    def apply_answer(self, grant: SensitiveGrant, scope: GrantScope, answer: str,
                     *, persist: bool = True) -> str:
        unique = unique_path(grant.operation) if answer in {ALLOW_PERSISTENT, ALLOW_GLOBAL} else None
        if unique:
            # Kept for this cycle, where it can still match; never refused (#439 AC-14).
            self.notes.append(f"{grant.id}: {answer} kept for this cycle only; {unique} is "
                              "unique to one dispatch or session and could never match again")
            answer = ALLOW_CYCLE
        if answer == ALLOW_GLOBAL:
            if grant.operation and grant.operation.get("role") in NO_GLOBAL_ROLES:
                raise GrantError(
                    f"{grant.id}: a global grant never covers {grant.operation['role']}; "
                    "answer this cycle or this repository")
            if grant.operation is None or self.store is None:
                raise GrantError("global answers require an exact command and a consent store")
            if not persist:
                return self.apply_answer(grant, scope, ALLOW_CYCLE, persist=False)
            self.store.save("*", grant, scope, "allow")
            return answer
        if answer == ALLOW_CYCLE and not scope.isolated_per_dispatch:
            raise GrantError(
                f"{grant.id} cannot be granted for one cycle on {scope.executor}/"
                f"{scope.platform}: the grant would not stay isolated to it")
        if answer == DENY:
            self._cycle_answers.pop(grant.fingerprint(scope), None)
            if getattr(grant, "operation", None) and self.cycle_scope and self.store:
                self.store.revoke(self.cycle_scope, grant, scope)
            if persist and self.store is not None:
                self.store.save(self.repo_id, grant, scope, "deny")
            else:
                self._cycle_answers[grant.fingerprint(scope)] = DENY
            self.denied_ids.add(grant.id)
            self.granted_ids.discard(grant.id)
            return answer
        self.granted_ids.add(grant.id)
        self.denied_ids.discard(grant.id)
        if answer == ALLOW_CYCLE or (answer == ALLOW_PERSISTENT and not persist):
            self._cycle_answers[grant.fingerprint(scope)] = answer
            if (answer == ALLOW_CYCLE and getattr(grant, "operation", None)
                    and self.cycle_scope and self.store):
                self.store.save(self.cycle_scope, grant, scope, "allow")
        if answer == ALLOW_PERSISTENT and persist:
            if self.store is None:
                raise GrantError(f"{grant.id}: no consent store to keep a persistent answer in")
            self.store.save(self.repo_id, grant, scope, "allow")
        return answer

    def request(self, grant: SensitiveGrant, executor: str, *, role: str,
                blocker: dict[str, Any] | None = None) -> str:
        """Answer a stage's request, ask for it, or leave it pending."""
        scope = grant.scope_for(executor, self.platform)
        if scope is None:
            self.notes.append(f"{grant.id} is not available for {executor} on "
                              f"{self.platform}; the sandbox was not widened")
            return UNSUPPORTED
        answer, source = self._resolve(grant, scope)
        if answer is not None:
            self._observed[grant.fingerprint(scope)] = answer
            self._observe("decision_answered", grant, scope, role, answer, source)
            return answer
        validation_error = ""
        while answer is None and self.prompt is not None:
            self._observe("decision_asked", grant, scope, role)
            source = "interactive"
            asked = question(grant, scope, role=role, blocker=blocker)
            if validation_error:
                asked = {**asked, "validation_error": validation_error,
                         "prompt": f"Invalid answer: {validation_error}. " + asked["prompt"]}
            if grant.operation:
                asked = {**asked, "permission_record": PermissionRequest(grant, scope, role, asked, blocker).record()}
            response = self.prompt(asked)
            if response is None:
                break
            try:
                candidate, candidate_scope = grant, scope
                answer = response
                # Edited matchers are new grants; reject before storing any
                # authorization, and retain the original request for retries.
                if isinstance(response, dict):
                    answer = response.get("answer")
                    if "pattern" in response:
                        candidate = make_pattern_grant(
                            response["pattern"], role, executor, self.platform,
                            proposer="user edit", justification=response.get("justification", "edited consent pattern"),
                            positive=response.get("positive", []), negative=response.get("negative", []),
                            compound=response.get("compound", False))
                        refused = (blocker or {}).get("refused_operation") or grant.operation
                        if (not isinstance(refused, dict) or refused.get("version") != 1
                                or not pattern_covers_operation(candidate.operation or {}, refused)):
                            raise GrantError("edited pattern does not cover the refused command")
                        candidate_scope = candidate.scopes[0]
                if not isinstance(answer, str) or answer not in ANSWERS:
                    raise GrantError(f"answer must be one of {', '.join(ANSWERS)}")
                answer = self.apply_answer(candidate, candidate_scope, answer)
                grant, scope = candidate, candidate_scope
                if grant.operation:
                    self.operations[grant.id] = grant
            except GrantError as error:
                validation_error = str(error)
                self.notes.append(f"consent answer rejected: {validation_error}")
                answer = None
                continue
            break
        if answer is None:
            self._observe("decision_asked", grant, scope, role)
            self.pending.append(PermissionRequest(
                grant, scope, role,
                question(grant, scope, role=role,
                         question_id=f"Q-{len(self.pending) + 1:03d}", blocker=blocker), blocker))
            return PENDING
        if answer not in ANSWERS:
            raise GrantError(f"{grant.id}: {answer!r} is not one of {', '.join(ANSWERS)}")
        if source != "interactive":
            answer = self.apply_answer(grant, scope, answer)
        self._observed[grant.fingerprint(scope)] = answer
        self._observe("decision_answered", grant, scope, role, answer, source)
        if answer == DENY:
            self.notes.append(f"{grant.id} denied: only the operations that need it "
                              "are skipped")
        else:
            self.notes.append(f"{grant.id} granted to {executor} for the rest of this "
                              f"cycle ({answer}); it opens {scope.effective}")
        return answer


def denial_context(grant: SensitiveGrant) -> str:
    """What a stage reruns with when a person denied the grant it needed."""
    if grant.operation is not None:
        check = grant.operation.get("check")
        if check:
            # A declined configured check never stops the cycle (#398).
            return (f"The user denied {grant.id} for role {grant.operation['role']}: "
                    f"{json.dumps(grant.operation, sort_keys=True)}. It is configured check "
                    f"{check}: do not run it, record it unverified with cause "
                    f"`permission {grant.id}: deny`, and complete every other step with the "
                    "status the rest of the work earns. This denial is not essential; the "
                    "cycle continues and its final state names the pending check.")
        return (f"The user denied {grant.id} for role {grant.operation['role']}: "
                f"{json.dumps(grant.operation, sort_keys=True)}. Skip this operation; "
                "affected checks stay not_verified. Only an essential denied operation "
                "needs HUMAN_INTERVENTION naming this decision.")
    return (
        f"The user denied {grant.resource} for this stage. Do not try to reach it "
        "again or work around the sandbox. Complete every check and step that "
        "does not need it, report each one that does as not verified because "
        "the permission was denied, and do not count it as passed."
        " Alternative: use exact-HEAD CI as complementary evidence; local checks "
        "remain not_verified. If the denied operation is essential to advance, "
        "report BLOCKED with the denial reason."
    )


def granted_context(grant: SensitiveGrant, scope: GrantScope) -> str:
    if grant.operation is not None:
        warnings = compose_warnings(grant.operation)
        return (f"The user granted {grant.id} only for role {grant.operation['role']} "
                f"and exact pattern {scope.effective}. This does not widen the sandbox "
                "or authorize another stage role. Interpreter internals depend on external isolation."
                + (" The hook recomputes the effective Compose configuration before the run; "
                   "if it changed, the run is refused and asks again. Record the check with "
                   f"its config_sha256 {grant.operation['compose']['config_sha256']}"
                   + (f" and these warnings: {'; '.join(warnings)}" if warnings else "") + "."
                   if grant.operation.get("compose") else ""))
    return (
        f"The user granted {grant.resource} to this and every later "
        f"{scope.executor} stage of the cycle. The sandbox now opens "
        f"{scope.effective}. Use it only for the checks that need it."
    )


def terminal_prompt(stream_in=None, stream_out=None) -> Callable[[dict], str | dict[str, Any] | None]:
    """Ask on a terminal, one question at a time, and return the answer token."""
    stream_in = stream_in or sys.stdin
    stream_out = stream_out or sys.stderr

    def ask(asked: dict) -> str | dict[str, Any] | None:
        print(f"\nPermission request {asked['id']}: {asked['prompt']}", file=stream_out)
        if "permission_record" in asked:
            print(json.dumps(asked["permission_record"], ensure_ascii=True), file=stream_out)
        for number, option in enumerate(asked["options"], start=1):
            print(f"  {number}. {option}", file=stream_out)
        while True:
            print("Choose a number: ", end="", file=stream_out, flush=True)
            line = stream_in.readline()
            if not line:
                return None
            choice = line.strip()
            if choice.startswith("{"):
                try:
                    edited = json.loads(choice)
                    if isinstance(edited, dict) and edited.get("answer") in ANSWERS:
                        return edited
                except ValueError:
                    pass
            if choice.isdigit() and 1 <= int(choice) <= len(asked["options"]):
                return answer_from_option(asked["options"][int(choice) - 1])

    return ask
