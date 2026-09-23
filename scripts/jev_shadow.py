"""Ask Jev which coder profile it would pick, and change nothing with the answer.

The rules in `router.rule_selector` choose every profile the cycle dispatches.
This module exists so a second opinion can be measured against them before
anyone considers trusting it: in `shadow` mode, an `implement` or `resolve`
stage that the rules have already routed is described to Jev, and whatever Jev
answers is written to telemetry beside the rules' choice. It is never handed to
`route()`, never resolves a target and never reaches a dispatch. It is an
observer, not a `ProfileSelector`, and that is structural rather than promised:
nothing here returns anything the router reads.

Three constraints shape it.

**Only closed pre-routing facts leave the machine.** The request is built from
the fields `telemetry.PRE_ROUTING_SIGNALS` classifies — the #35 contract — with
each value a count, a flag or a closed token. No prose, path, diff, repository
name, work-item id or later outcome is sent, and the two options are fixed.
The instructions that describe them are this module's own constants, not text
from the work item.

**Failure is a category, not a message.** Timeout, rate limit, HTTP error,
unreadable response and a missing key each map to one token. A remote body or
exception text is never kept, so nothing the service says can reach the store.

**Disabled means absent.** With the mode unset or `disabled`, no `JevShadow`
is built, no key is read and no socket is opened. In `shadow` without
`JEV_API_KEY`, the stage records `unavailable` and the cycle carries on.

The key is read from the environment at the moment of the call, placed in one
request header, and held nowhere else.
"""

from __future__ import annotations

import json
import math
import os
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import Enum

from telemetry import JEV_MODELS, PRE_ROUTING_SIGNALS

#: The only host the adapter talks to. Not configurable: a configurable
#: endpoint is a place to send pre-routing data that nobody reviewed.
JEV_URL = "https://www.jevai.org/api/v1/decisions"
API_KEY_ENV = "JEV_API_KEY"
DEFAULT_MODEL = "typesafe-ai/jev"
DEFAULT_TIMEOUT_SECONDS = 3.0
MAX_TIMEOUT_SECONDS = 10.0
#: A decision is a few hundred bytes; anything much larger is not one.
MAX_RESPONSE_BYTES = 64 * 1024

#: The stages Jev is asked about, and the options it may choose between.
SHADOW_ROLES = frozenset({"implement", "resolve"})
OPTIONS = ("cheap_coder", "deep_coder")
QUESTION = "profile"
INSTRUCTIONS = (
    "Choose the implementation profile for this stage from the pre-routing "
    "signals alone."
)
CRITERIA = {
    "cheap_coder": "Less expensive coder; suited to routine, well-bounded work.",
    "deep_coder": "More expensive, deeper coder; suited to difficult or risky work.",
}

#: Everything the request may carry, and nothing else: the #35 signals.
PAYLOAD_FIELDS = frozenset().union(*PRE_ROUTING_SIGNALS.values())
#: The signals that are tokens rather than counts or flags.
PAYLOAD_TOKENS = {"verifiability": frozenset({"auto", "partial", "human"}),
                  "role": SHADOW_ROLES}

#: What `jev_status` may say. `suggested` is the only one with a suggestion.
STATUSES = frozenset({
    "suggested", "unavailable", "timeout", "rate_limited", "http_error",
    "invalid_response",
})

#: The keys `code_cycle.routing.jev` may carry.
CONFIG_KEYS = frozenset({"mode", "model", "timeout_seconds"})


class JevMode(str, Enum):
    DISABLED = "disabled"
    SHADOW = "shadow"


class JevConfigError(ValueError):
    """`code_cycle.routing.jev` declares something this adapter will not run."""


@dataclass(frozen=True)
class JevConfig:
    mode: JevMode = JevMode.DISABLED
    model: str = DEFAULT_MODEL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @property
    def enabled(self) -> bool:
        return self.mode is JevMode.SHADOW


def load_jev_config(config: dict | None = None) -> JevConfig:
    """Read `code_cycle.routing.jev`, refusing anything it does not know.

    Absent means disabled. A misspelt key is refused rather than ignored: a
    `mod: shadow` that silently stays disabled measures nothing and says so
    nowhere.
    """
    section = (config or {}).get("code_cycle") or {}
    routing = section.get("routing") if isinstance(section, dict) else None
    jev = routing.get("jev") if isinstance(routing, dict) else None
    if jev is None:
        return JevConfig()
    if not isinstance(jev, dict):
        raise JevConfigError(
            f"code_cycle.routing.jev must be a mapping, not {type(jev).__name__}")
    unknown = sorted(str(key) for key in set(jev) - CONFIG_KEYS)
    if unknown:
        raise JevConfigError(
            "unknown key under code_cycle.routing.jev: " + ", ".join(unknown)
            + "; known keys are " + ", ".join(sorted(CONFIG_KEYS)))

    mode = jev.get("mode", JevMode.DISABLED.value)
    try:
        mode = JevMode(mode)
    except (TypeError, ValueError):
        raise JevConfigError(
            f"unknown code_cycle.routing.jev.mode value: {mode!r} "
            "(expected 'disabled' or 'shadow')") from None

    model = jev.get("model", DEFAULT_MODEL)
    if model not in JEV_MODELS:
        raise JevConfigError(
            f"code_cycle.routing.jev.model must be one of "
            f"{', '.join(sorted(JEV_MODELS))}, not {model!r}")

    timeout = jev.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not 0 < timeout <= MAX_TIMEOUT_SECONDS):
        raise JevConfigError(
            "code_cycle.routing.jev.timeout_seconds must be a number above 0 "
            f"and at most {MAX_TIMEOUT_SECONDS:g}, not {timeout!r}")
    return JevConfig(mode, model, float(timeout))


def build_state(role: str, signals: Mapping) -> dict:
    """The request's `state`: allowlisted pre-routing signals, scalars only.

    A field outside the contract, or a value that is not a count, a flag or a
    known token, is left out rather than sent. Dropping is the safe direction
    here: the request is optional, and the telemetry row already refuses loudly.
    """
    state = {"role": role} if role in SHADOW_ROLES else {}
    for key, value in signals.items():
        if key not in PAYLOAD_FIELDS or key == "role":
            continue
        if key in PAYLOAD_TOKENS:
            if isinstance(value, str) and value in PAYLOAD_TOKENS[key]:
                state[key] = value
        elif isinstance(value, bool) or (isinstance(value, int) and value >= 0):
            state[key] = value
    return state


def build_request(model: str, role: str, signals: Mapping) -> dict:
    return {
        "model": model,
        "state": build_state(role, signals),
        "questions": {
            QUESTION: {
                "type": "choice",
                "instructions": INSTRUCTIONS,
                "criteria": dict(CRITERIA),
            },
        },
    }


@dataclass(frozen=True)
class Suggestion:
    """What Jev said, reduced to typed fields; unknown stays `None`."""

    status: str
    profile: str | None = None
    confidence: float | None = None
    probabilities: Mapping[str, float] | None = None
    model_resolved: str | None = None
    model_resolution: str = "unreported"
    duration_ms: int | None = None


#: `transport(url, body, headers, timeout) -> (http_status, body)`. Raises
#: `TimeoutError` on a timeout and `OSError` when the service cannot be reached.
Transport = Callable[[str, bytes, dict, float], tuple[int, bytes]]


def urllib_transport(url: str, body: bytes, headers: dict,
                     timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        return error.code, b""
    except urllib.error.URLError as error:
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            raise TimeoutError() from None
        raise OSError() from None


def _probability(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and 0.0 <= value <= 1.0 else None


def parse_response(status: int, body: bytes, model: str) -> Suggestion:
    """Turn a response into a `Suggestion`, or into the category it failed by."""
    if status == 429:
        return Suggestion("rate_limited")
    if status >= 500:
        return Suggestion("unavailable")
    if status != 200:
        return Suggestion("http_error")
    if len(body) > MAX_RESPONSE_BYTES:
        return Suggestion("invalid_response")
    try:
        envelope = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return Suggestion("invalid_response")
    if not isinstance(envelope, dict) or envelope.get("code") != 0:
        return Suggestion("invalid_response")
    data = envelope.get("data")
    answers = data.get("answers") if isinstance(data, dict) else None
    answer = answers.get(QUESTION) if isinstance(answers, dict) else None
    if not isinstance(answer, dict) or answer.get("choice") not in OPTIONS:
        return Suggestion("invalid_response")

    confidence = answer.get("confidence")
    if confidence is not None:
        confidence = _probability(confidence)
        if confidence is None:
            return Suggestion("invalid_response")

    probabilities = answer.get("probabilities")
    if probabilities is not None:
        if not isinstance(probabilities, dict) or not set(probabilities) <= set(OPTIONS):
            return Suggestion("invalid_response")
        checked = {key: _probability(value) for key, value in probabilities.items()}
        if any(value is None for value in checked.values()):
            return Suggestion("invalid_response")
        probabilities = checked

    # Only a model the service names is recorded, and only a known one by name.
    reported = data.get("model")
    if reported is None:
        resolved, resolution = None, "unreported"
    elif reported == model:
        resolved, resolution = reported, "matched"
    elif reported in JEV_MODELS:
        resolved, resolution = reported, "mismatch_known"
    else:
        resolved, resolution = None, "mismatch_unrecognized"

    return Suggestion(
        "suggested", answer["choice"], confidence, probabilities or None,
        resolved, resolution,
    )


class JevShadow:
    """One bounded question per eligible stage; never raises into the cycle."""

    def __init__(self, config: JevConfig, *, transport: Transport | None = None,
                 environ: Mapping[str, str] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not config.enabled:
            raise JevConfigError("a JevShadow is built only in shadow mode")
        self.config = config
        self._transport = transport or urllib_transport
        self._environ = os.environ if environ is None else environ
        self._clock = clock

    def applies_to(self, role: str) -> bool:
        return role in SHADOW_ROLES

    def suggest(self, role: str, signals: Mapping) -> Suggestion:
        key = self._environ.get(API_KEY_ENV)
        if not key:
            return Suggestion("unavailable")
        body = json.dumps(build_request(self.config.model, role, signals)).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {key}"}
        started = self._clock()
        try:
            status, raw = self._transport(JEV_URL, body, headers,
                                          self.config.timeout_seconds)
            suggestion = parse_response(status, raw, self.config.model)
        except TimeoutError:
            suggestion = Suggestion("timeout")
        except OSError:
            suggestion = Suggestion("unavailable")
        except Exception:
            # A shadow must not be able to stop the stage it watches. What the
            # exception said is not kept: it could be anything the service sent.
            suggestion = Suggestion("invalid_response")
        elapsed = max(0, int(round((self._clock() - started) * 1000)))
        return replace(suggestion, duration_ms=elapsed)


def telemetry_fields(config: JevConfig, rule_profile: str,
                     suggestion: Suggestion) -> dict:
    """The `shadow` row's fields. Unknown values are omitted, never defaulted."""
    fields = {
        "jev_status": suggestion.status,
        "jev_rule_profile": rule_profile,
        "jev_model_requested": config.model,
        "jev_duration_ms": suggestion.duration_ms,
    }
    if suggestion.status == "suggested":
        fields.update(
            jev_suggested_profile=suggestion.profile,
            jev_agreement=suggestion.profile == rule_profile,
            jev_confidence=suggestion.confidence,
            jev_model_resolved=suggestion.model_resolved,
            jev_model_resolution=suggestion.model_resolution,
        )
        for option, value in (suggestion.probabilities or {}).items():
            fields[f"jev_probability_{option}"] = value
    return {key: value for key, value in fields.items() if value is not None}
