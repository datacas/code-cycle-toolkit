"""Normalize structured executor usage without retaining executor output."""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation


_CATEGORIES = {
    "input_total", "input", "cached_input", "cache_read_input",
    "cache_write_input", "cache_creation_input", "output_total", "output",
    "reasoning_output",
}
_MAX_TOKEN_COUNT = 9_223_372_036_854_775_807
_MAX_COST_TEXT_LENGTH = 128
_MAX_COST_EXPONENT = 128
_MAX_COST_AMOUNT = Decimal("1000000000000000000")
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}\Z")


def _integer(value) -> int | None:
    if (isinstance(value, bool) or not isinstance(value, int)
            or value < 0 or value > _MAX_TOKEN_COUNT):
        return None
    return value


def _decimal_text(value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, str) and len(value) > _MAX_COST_TEXT_LENGTH:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if (
        not parsed.is_finite()
        or parsed < 0
        or parsed > _MAX_COST_AMOUNT
        or abs(parsed.as_tuple().exponent) > _MAX_COST_EXPONENT
    ):
        return None
    return format(parsed, "f")


def _observation_id(attempt_id: str, source: str, event: str, ordinal: int,
                    model: str | None, category: str) -> str:
    material = "\0".join((attempt_id, source, event, str(ordinal), model or "", category))
    return "usage-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _measure_id(attempt_id: str, source: str, event: str, ordinal: int,
                model: str | None, component: str) -> str:
    material = "\0".join((attempt_id, source, event, str(ordinal), model or "", component))
    return "cost-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def normalize_executor_output(executor: str, stdout: str, attempt_id: str):
    """Return token observations and unpriced cost reports from JSONL output.

    The CLI output is parsed in memory and never returned or persisted. Missing
    categories are omitted. A reported CLI dollar value remains an unpriced
    report with an ``unknown`` basis until a versioned pricing source can
    establish what it represents.
    """
    if executor not in {"codex", "claude"} or not isinstance(stdout, str):
        return (), ()
    usage_rows: list[dict] = []
    cost_rows: list[dict] = []
    ordinals = {"turn.completed": 0, "result.modelUsage": 0}

    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue

        event_type = event.get("type")
        if executor == "codex" and event_type == "turn.completed":
            usage = event.get("usage")
            if not isinstance(usage, dict):
                continue
            source, event_name = "codex", "turn.completed"
            ordinal = ordinals[event_name]
            ordinals[event_name] += 1
            model = None
            fields = (
                ("input_tokens", "input_total"),
                ("cached_input_tokens", "cached_input"),
                ("cache_write_input_tokens", "cache_write_input"),
                ("output_tokens", "output_total"),
                ("reasoning_output_tokens", "reasoning_output"),
            )
            for key, category in fields:
                amount = _integer(usage.get(key))
                if amount is not None:
                    usage_rows.append({
                        "observation_id": _observation_id(
                            attempt_id, source, event_name, ordinal, model, category,
                        ),
                        "source": source, "source_event": event_name,
                        "event_ordinal": ordinal, "source_model": model,
                        "category": category, "amount": amount,
                    })

        if executor == "claude" and event_type == "result":
            model_usage = event.get("modelUsage")
            if not isinstance(model_usage, dict) or not model_usage:
                continue
            source, event_name = "claude", "result.modelUsage"
            ordinal = ordinals[event_name]
            ordinals[event_name] += 1
            for raw_model, values in model_usage.items():
                model = raw_model if isinstance(raw_model, str) and _MODEL_RE.fullmatch(raw_model) else None
                if not isinstance(values, dict):
                    continue
                fields = (
                    ("inputTokens", "input"),
                    ("cacheReadInputTokens", "cache_read_input"),
                    ("cacheCreationInputTokens", "cache_creation_input"),
                    ("outputTokens", "output"),
                )
                for key, category in fields:
                    amount = _integer(values.get(key))
                    if amount is not None:
                        usage_rows.append({
                            "observation_id": _observation_id(
                                attempt_id, source, event_name, ordinal, model, category,
                            ),
                            "source": source, "source_event": event_name,
                            "event_ordinal": ordinal, "source_model": model,
                            "category": category, "amount": amount,
                        })
                reported_cost = _decimal_text(values.get("costUSD"))
                if reported_cost is not None:
                    component = "reported_cli_cost"
                    cost_rows.append({
                        "measure_id": _measure_id(
                            attempt_id, source, event_name, ordinal, model, component,
                        ),
                        "basis": "unknown", "component": component,
                        "source": source, "unit": "USD",
                        "amount": None, "reported_amount": reported_cost,
                        "pricing_snapshot_id": None, "pricing_date": None,
                    })

    return tuple(usage_rows), tuple(cost_rows)


def usage_summary(observations) -> str | None:
    """Format only categories the executor explicitly reported."""
    values: dict[str, int] = {}
    for observation in observations or ():
        if not isinstance(observation, dict):
            continue
        category, amount = observation.get("category"), observation.get("amount")
        if category in _CATEGORIES and isinstance(amount, int) and not isinstance(amount, bool):
            values[category] = values.get(category, 0) + amount
    if not values:
        return None
    parts = []
    input_total = values.get("input_total", values.get("input"))
    if input_total is not None:
        parts.append(f"in {_compact(input_total)}")
    for category, label in (
        ("cached_input", "cached"),
        ("cache_read_input", "cache read"),
        ("cache_write_input", "cache write"),
        ("cache_creation_input", "cache create"),
    ):
        if category in values:
            parts.append(f"{label} {_compact(values[category])}")
    output_total = values.get("output_total", values.get("output"))
    if output_total is not None:
        parts.append(f"out {_compact(output_total)}")
    if "reasoning_output" in values:
        parts.append(f"reasoning {_compact(values['reasoning_output'])}")
    return " / ".join(parts)


def _compact(amount: int) -> str:
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.1f}M"
    if amount >= 10_000:
        return f"{amount / 1_000:.0f}k"
    if amount >= 1_000:
        if amount % 1_000 == 0:
            return f"{amount // 1_000}k"
        return f"{amount / 1_000:.1f}k"
    return str(amount)
