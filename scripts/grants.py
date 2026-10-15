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
stored answer no longer applies and the person is asked again. *Deny* removes
only the operations that need the grant, for this run only; the stage
reruns without it, and a later run asks again.

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
from typing import Callable

#: Answers a person can give, as `--grant ID=<answer>` spells them.
ALLOW_CYCLE = "cycle"
ALLOW_PERSISTENT = "persistent"
DENY = "deny"
ANSWERS = (ALLOW_CYCLE, ALLOW_PERSISTENT, DENY)

#: What `ConsentBroker.request` can return besides an answer.
PENDING = "pending"
UNSUPPORTED = "unsupported"

STORE_SCHEMA = 1


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
            risks=("The Docker daemon acts as root on the host: a container can "
                   "mount and change any host path, outside the checkout. Other "
                   "sockets expose their own services, for example ssh-agent can "
                   "sign with your keys. Network domains and the read-only "
                   "checkout of review stages stay as they are"),
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
        if (not isinstance(payload, dict) or payload.get("schema") != STORE_SCHEMA
                or not isinstance(payload.get("repos"), dict)):
            raise GrantError(f"{self.path}: not a consent store this runtime can read")
        return payload

    def load(self, repo_id: str, grant: SensitiveGrant, scope: GrantScope) -> str | None:
        """`allow` when a person agreed to this exact scope, else None."""
        entry = (self._read()["repos"].get(repo_id) or {}).get(grant.id)
        if (not isinstance(entry, dict)
                or entry.get("fingerprint") != grant.fingerprint(scope)
                or entry.get("decision") != "allow"):
            return None
        return entry["decision"]

    def save(self, repo_id: str, grant: SensitiveGrant, scope: GrantScope,
             decision: str) -> None:
        if decision != "allow":
            raise GrantError(f"only an allow is stored, not {decision!r}")
        payload = self._read()
        payload["repos"].setdefault(repo_id, {})[grant.id] = {
            "decision": decision,
            "fingerprint": grant.fingerprint(scope),
            "executor": scope.executor,
            "platform": scope.platform,
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }
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
             question_id: str = "Q-001") -> dict:
    """The consent question, in the shape `stop_questions` defines."""
    options = []
    if scope.isolated_per_dispatch:
        options.append(f"Allow for this cycle ({ALLOW_CYCLE})")
    options += [f"Allow persistently for this repository ({ALLOW_PERSISTENT})",
                f"Deny ({DENY})"]
    prompt = (f"The {role} stage asked for {grant.resource}: {grant.reason}. "
              f"Granting it opens {scope.effective}. {scope.risks}.")
    return {
        "id": question_id,
        "prompt": prompt[:597] + "..." if len(prompt) > 600 else prompt,
        "options": options,
        "recommended": None,
        "blocks": [f"grant:{grant.id}"],
    }


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

    def record(self) -> dict:
        return {
            "grant": self.grant.id, "resource": self.grant.resource,
            "executor": self.scope.executor, "platform": self.scope.platform,
            "effective_scope": self.scope.effective, "risks": self.scope.risks,
            "temporary_offered": self.scope.isolated_per_dispatch,
            "role": self.role, "answer_with": f"--grant {self.grant.id}=<answer>",
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

    def grant_for(self, blocker: dict) -> SensitiveGrant | None:
        return next((grant for grant in GRANTS.values() if grant.matches(blocker)), None)

    def granted(self, executor: str) -> tuple[str, ...]:
        """The grants every remaining dispatch of this executor receives."""
        return tuple(sorted(
            grant_id for grant_id in self.granted_ids
            if GRANTS[grant_id].scope_for(executor, self.platform) is not None))

    def apply_answer(self, grant: SensitiveGrant, scope: GrantScope, answer: str,
                     *, persist: bool = True) -> str:
        if answer == ALLOW_CYCLE and not scope.isolated_per_dispatch:
            raise GrantError(
                f"{grant.id} cannot be granted for one cycle on {scope.executor}/"
                f"{scope.platform}: the grant would not stay isolated to it")
        if answer == DENY:
            # A denial holds for this run only; the next run asks again.
            self.denied_ids.add(grant.id)
            self.granted_ids.discard(grant.id)
            return answer
        self.granted_ids.add(grant.id)
        if answer == ALLOW_PERSISTENT and persist:
            if self.store is None:
                raise GrantError(f"{grant.id}: no consent store to keep a persistent answer in")
            self.store.save(self.repo_id, grant, scope, "allow")
        return answer

    def request(self, grant: SensitiveGrant, executor: str, *, role: str) -> str:
        """Answer a stage's request, ask for it, or leave it pending."""
        scope = grant.scope_for(executor, self.platform)
        if scope is None:
            self.notes.append(f"{grant.id} is not available for {executor} on "
                              f"{self.platform}; the sandbox was not widened")
            return UNSUPPORTED
        if grant.id in self.denied_ids:
            return DENY
        if grant.id in self.granted_ids:
            return ALLOW_CYCLE
        answer = self.answers.get(grant.id)
        persist = True
        if answer is None and grant.id in self.preauthorized:
            answer, persist = ALLOW_PERSISTENT, False
            self.notes.append(f"{grant.id} granted by the project configuration")
        if answer is None and self.store is not None:
            try:
                stored = self.store.load(self.repo_id, grant, scope)
            except GrantError as error:
                # An unreadable store answers nothing; it never grants.
                stored = None
                self.notes.append(f"{error}; asking instead")
            if stored == "allow":
                answer, persist = ALLOW_PERSISTENT, False
                self.notes.append(f"{grant.id} granted by a stored answer ({self.store.path})")
        if answer is None and self.prompt is not None:
            answer = self.prompt(question(grant, scope, role=role))
        if answer is None:
            self.pending.append(PermissionRequest(
                grant, scope, role,
                question(grant, scope, role=role,
                         question_id=f"Q-{len(self.pending) + 1:03d}")))
            return PENDING
        if answer not in ANSWERS:
            raise GrantError(f"{grant.id}: {answer!r} is not one of {', '.join(ANSWERS)}")
        answer = self.apply_answer(grant, scope, answer, persist=persist)
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
    )


def granted_context(grant: SensitiveGrant, scope: GrantScope) -> str:
    return (
        f"The user granted {grant.resource} for this stage. The sandbox now opens "
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
