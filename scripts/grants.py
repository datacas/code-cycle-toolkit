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
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

#: Answers a person can give, as `--grant ID=<answer>` spells them.
ALLOW_CYCLE = "cycle"
ALLOW_PERSISTENT = "persistent"
DENY = "deny"
ANSWERS = (ALLOW_CYCLE, ALLOW_PERSISTENT, DENY)

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
                       "D-Bus and other local service sockets; Claude's command hook "
                       "still permits only docker compose up, down, ps, logs and build; "
                       "docker run, exec and compose run remain denied"),
            risks=("Access to the Docker daemon is in practice root on the host, and "
                   "ssh-agent can sign with your keys. Claude's command hook still "
                   "limits Docker commands to the listed Compose operations"),
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
        if not separator or grant_id not in GRANTS or answer not in ANSWERS:
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
        entries = (self._read()["repos"].get(repo_id) or {}).get(grant.id, {})
        entry = entries.get(grant.fingerprint(scope)) if isinstance(entries, dict) else None
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
                for entry in self.list(repo_id) if entry["status"] == "GRANTED"}

    def list(self, repo_id: str) -> list[dict[str, Any]]:
        """Current scope-valid decisions. History never authorizes access."""
        decisions = []
        for grant_id, entries in (self._read()["repos"].get(repo_id) or {}).items():
            grant = GRANTS.get(grant_id)
            if grant is None or not isinstance(entries, dict):
                continue
            for fingerprint, entry in entries.items():
                if not isinstance(entry, dict) or entry.get("decision") not in ("allow", "deny"):
                    continue
                scope = grant.scope_for(str(entry.get("executor")), str(entry.get("platform")))
                if (scope is None or fingerprint != grant.fingerprint(scope)
                        or entry.get("fingerprint") != fingerprint):
                    continue
                decisions.append({**entry, "grant": grant_id, "source": "consents.json",
                                  "status": "GRANTED" if entry["decision"] == "allow" else "DENIED",
                                  "effective_scope": scope.effective})
        return decisions

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
        }
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
        return {
            "grant": self.grant.id, "resource": self.grant.resource,
            "executor": self.scope.executor, "platform": self.scope.platform,
            "effective_scope": self.scope.effective, "risks": self.scope.risks,
            "temporary_offered": self.scope.isolated_per_dispatch,
            "role": self.role, "answer_with": f"--grant {self.grant.id}=<answer>",
            "operation": (self.blocker or {}).get("command", self.grant.reason),
            "observed_error": (self.blocker or {}).get("error"),
            "alternative": "exact-HEAD CI; local checks remain not_verified",
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
    prompt: Callable[[dict], str | None] | None = None
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

    def _observe(self, kind, grant, scope, role, answer=None, source=None) -> None:
        if self.observer is not None:
            try:
                self.observer(kind, grant, scope, role, answer, source)
            except Exception as error:
                self.notes.append(f"consent audit failed ({type(error).__name__})")

    def grant_for(self, blocker: dict) -> SensitiveGrant | None:
        return next((grant for grant in GRANTS.values() if grant.matches(blocker)), None)

    def granted(self, executor: str) -> tuple[str, ...]:
        """Revalidate effective decisions immediately before each dispatch."""
        granted, denied = set(), set()
        for grant in GRANTS.values():
            scope = grant.scope_for(executor, self.platform)
            if scope is None:
                continue
            answer, source = self._resolve(grant, scope)
            if answer in (ALLOW_CYCLE, ALLOW_PERSISTENT):
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
            return self._cycle_answers[key], "cycle"
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
        for grant in GRANTS.values():
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
        if answer == ALLOW_CYCLE and not scope.isolated_per_dispatch:
            raise GrantError(
                f"{grant.id} cannot be granted for one cycle on {scope.executor}/"
                f"{scope.platform}: the grant would not stay isolated to it")
        if answer == DENY:
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
        if answer is None and self.prompt is not None:
            self._observe("decision_asked", grant, scope, role)
            source = "interactive"
            answer = self.prompt(question(grant, scope, role=role, blocker=blocker))
        if answer is None:
            self._observe("decision_asked", grant, scope, role)
            self.pending.append(PermissionRequest(
                grant, scope, role,
                question(grant, scope, role=role,
                         question_id=f"Q-{len(self.pending) + 1:03d}", blocker=blocker), blocker))
            return PENDING
        if answer not in ANSWERS:
            raise GrantError(f"{grant.id}: {answer!r} is not one of {', '.join(ANSWERS)}")
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
    return (
        f"The user granted {grant.resource} to this and every later "
        f"{scope.executor} stage of the cycle. The sandbox now opens "
        f"{scope.effective}. Use it only for the checks that need it."
    )


def terminal_prompt(stream_in=None, stream_out=None) -> Callable[[dict], str | None]:
    """Ask on a terminal, one question at a time, and return the answer token."""
    stream_in = stream_in or sys.stdin
    stream_out = stream_out or sys.stderr

    def ask(asked: dict) -> str | None:
        print(f"\nPermission request {asked['id']}: {asked['prompt']}", file=stream_out)
        for number, option in enumerate(asked["options"], start=1):
            print(f"  {number}. {option}", file=stream_out)
        while True:
            print("Choose a number: ", end="", file=stream_out, flush=True)
            line = stream_in.readline()
            if not line:
                return None
            choice = line.strip()
            if choice.isdigit() and 1 <= int(choice) <= len(asked["options"]):
                return answer_from_option(asked["options"][int(choice) - 1])

    return ask
