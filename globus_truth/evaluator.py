"""Strict, deterministic evaluation of Globus agent run receipts."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

SCHEMA_VERSION = "1.0"
VERDICTS = (
    "healthy",
    "verified_no_work",
    "degraded_contradictory",
    "failed",
    "stale",
)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
_TOP_LEVEL_FIELDS = {
    "schema_version",
    "receipt_id",
    "agent_id",
    "run_id",
    "declared_status",
    "started_at",
    "finished_at",
    "heartbeat_at",
    "input",
    "output",
    "summary",
    "evidence",
    "checks",
    "no_work",
    "error",
    "metadata",
}
_EVIDENCE_KINDS = {
    "artifact",
    "database_write",
    "api_ack",
    "checksum",
    "metric",
    "human_ack",
}
_REFUSAL_PATTERNS = (
    re.compile(r"\bas an ai\b", re.I),
    re.compile(r"\bi (?:cannot|can't|am unable to) (?:comply|complete|perform|access)\b", re.I),
    re.compile(r"\bplease (?:provide|share|upload) (?:the )?(?:input|data|source|material)\b", re.I),
    re.compile(r"\bno (?:input|source|material|data) (?:was|were|has been) (?:provided|included)\b", re.I),
    re.compile(r"^\s*(?:error|exception|traceback)\s*[:\n]", re.I),
    re.compile(r"\b(?:request failed|timed out|rate limit(?:ed)?|quota exceeded)\b", re.I),
)


@dataclass(frozen=True)
class Evaluation:
    """A persisted, explainable verdict."""

    verdict: str
    evaluated_at: str
    reason_codes: tuple[str, ...]
    checks: tuple[dict[str, Any], ...]

    @property
    def valid(self) -> bool:
        return self.verdict in {"healthy", "verified_no_work"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "valid": self.valid,
            "evaluated_at": self.evaluated_at,
            "reason_codes": list(self.reason_codes),
            "checks": [dict(check) for check in self.checks],
        }


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 40
        or not _RFC3339_RE.fullmatch(value)
    ):
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def _plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _text(value: Any, *, minimum: int = 1, maximum: int = 500) -> bool:
    return isinstance(value, str) and minimum <= len(value.strip()) <= maximum


def _check(
    checks: list[dict[str, Any]],
    name: str,
    passed: bool,
    detail: str,
    failures: list[str],
    code: str,
) -> None:
    checks.append({"name": name, "passed": bool(passed), "detail": detail})
    if not passed:
        failures.append(code)


def _contains_refusal(receipt: Mapping[str, Any]) -> bool:
    values: list[str] = []
    for key in ("summary",):
        if isinstance(receipt.get(key), str):
            values.append(receipt[key])
    evidence = receipt.get("evidence")
    if isinstance(evidence, list):
        for item in evidence:
            if isinstance(item, Mapping):
                for key in ("detail", "ref"):
                    if isinstance(item.get(key), str):
                        values.append(item[key])
    combined = "\n".join(values)
    return any(pattern.search(combined) for pattern in _REFUSAL_PATTERNS)


def evaluate_receipt(
    receipt: Mapping[str, Any] | Any,
    *,
    now: datetime | None = None,
    stale_after: timedelta = timedelta(hours=24),
    future_tolerance: timedelta = timedelta(minutes=5),
) -> Evaluation:
    """Evaluate one receipt without side effects.

    Structurally invalid receipts are ``failed``. Structurally valid receipts whose
    claims conflict with their measurements are ``degraded_contradictory``.
    Failure and contradiction take precedence over staleness.
    """

    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    evaluated_at = _iso(clock)
    checks: list[dict[str, Any]] = []
    schema_failures: list[str] = []

    if not isinstance(receipt, Mapping):
        _check(checks, "receipt_object", False, "Receipt must be a JSON object.", schema_failures, "receipt_not_object")
        return Evaluation("failed", evaluated_at, tuple(schema_failures), tuple(checks))

    unknown = sorted(set(receipt) - _TOP_LEVEL_FIELDS)
    _check(
        checks,
        "known_fields",
        not unknown,
        "No unknown top-level fields." if not unknown else f"Unknown fields: {', '.join(unknown)}",
        schema_failures,
        "unknown_fields",
    )
    _check(
        checks,
        "schema_version",
        receipt.get("schema_version") == SCHEMA_VERSION,
        f"Expected schema_version {SCHEMA_VERSION}.",
        schema_failures,
        "unsupported_schema_version",
    )
    for field in ("receipt_id", "agent_id", "run_id"):
        value = receipt.get(field)
        valid = isinstance(value, str) and bool(_ID_RE.fullmatch(value))
        _check(checks, field, valid, f"{field} must be a safe 1-128 character identifier.", schema_failures, f"invalid_{field}")

    status = receipt.get("declared_status")
    _check(
        checks,
        "declared_status",
        status in {"success", "no_work", "failed"},
        "declared_status must be success, no_work, or failed.",
        schema_failures,
        "invalid_declared_status",
    )

    timestamps: dict[str, datetime | None] = {}
    for field in ("started_at", "finished_at", "heartbeat_at"):
        timestamps[field] = _parse_timestamp(receipt.get(field))
        _check(
            checks,
            field,
            timestamps[field] is not None,
            f"{field} must be an RFC 3339 timestamp with a timezone.",
            schema_failures,
            f"invalid_{field}",
        )

    input_data = receipt.get("input")
    input_ok = isinstance(input_data, Mapping) and set(input_data) == {"items_seen", "items_eligible"}
    _check(
        checks,
        "input_shape",
        input_ok,
        "input must contain exactly items_seen and items_eligible.",
        schema_failures,
        "invalid_input_shape",
    )
    output_data = receipt.get("output")
    output_ok = isinstance(output_data, Mapping) and set(output_data) == {"items_processed", "items_changed"}
    _check(
        checks,
        "output_shape",
        output_ok,
        "output must contain exactly items_processed and items_changed.",
        schema_failures,
        "invalid_output_shape",
    )

    counts: dict[str, int] = {}
    if input_ok and output_ok:
        raw_counts = {
            "items_seen": input_data["items_seen"],
            "items_eligible": input_data["items_eligible"],
            "items_processed": output_data["items_processed"],
            "items_changed": output_data["items_changed"],
        }
        counts_ok = all(_plain_int(value) and 0 <= value <= 1_000_000_000 for value in raw_counts.values())
        if counts_ok:
            counts = {key: int(value) for key, value in raw_counts.items()}
        _check(
            checks,
            "count_types",
            counts_ok,
            "All counts must be non-negative integers no greater than 1,000,000,000.",
            schema_failures,
            "invalid_counts",
        )

    _check(
        checks,
        "summary",
        _text(receipt.get("summary"), minimum=1, maximum=2000),
        "summary must contain 1-2,000 characters.",
        schema_failures,
        "invalid_summary",
    )

    evidence = receipt.get("evidence")
    evidence_shape = isinstance(evidence, list) and len(evidence) <= 100
    if evidence_shape:
        for item in evidence:
            if not isinstance(item, Mapping) or not {"kind", "ref", "observed_at"} <= set(item):
                evidence_shape = False
                break
            if set(item) - {"kind", "ref", "observed_at", "detail", "sha256"}:
                evidence_shape = False
                break
            if item.get("kind") not in _EVIDENCE_KINDS or not _text(item.get("ref"), maximum=500):
                evidence_shape = False
                break
            if _parse_timestamp(item.get("observed_at")) is None:
                evidence_shape = False
                break
            if "detail" in item and not _text(item.get("detail"), maximum=1000):
                evidence_shape = False
                break
            if "sha256" in item and (
                not isinstance(item.get("sha256"), str)
                or not _SHA256_RE.fullmatch(item["sha256"])
            ):
                evidence_shape = False
                break
    _check(
        checks,
        "evidence_shape",
        evidence_shape,
        "evidence must be a list of at most 100 well-formed evidence objects.",
        schema_failures,
        "invalid_evidence",
    )

    declared_checks = receipt.get("checks")
    declared_checks_shape = isinstance(declared_checks, list) and len(declared_checks) <= 100
    if declared_checks_shape:
        for item in declared_checks:
            if (
                not isinstance(item, Mapping)
                or set(item) != {"name", "passed", "detail"}
                or not _text(item.get("name"), maximum=100)
                or not isinstance(item.get("passed"), bool)
                or not _text(item.get("detail"), maximum=1000)
            ):
                declared_checks_shape = False
                break
    _check(
        checks,
        "checks_shape",
        declared_checks_shape,
        "checks must be a list of name/passed/detail objects.",
        schema_failures,
        "invalid_checks",
    )

    metadata = receipt.get("metadata", {})
    metadata_ok = isinstance(metadata, Mapping) and len(metadata) <= 20 and all(
        _text(key, maximum=64)
        and (
            isinstance(value, (str, int, bool))
            or (isinstance(value, float) and math.isfinite(value))
        )
        for key, value in metadata.items()
    )
    _check(
        checks,
        "metadata_shape",
        metadata_ok,
        "metadata may contain at most 20 scalar values.",
        schema_failures,
        "invalid_metadata",
    )

    if schema_failures:
        return Evaluation("failed", evaluated_at, tuple(dict.fromkeys(schema_failures)), tuple(checks))

    contradictions: list[str] = []
    start = timestamps["started_at"]
    finish = timestamps["finished_at"]
    heartbeat = timestamps["heartbeat_at"]
    assert start is not None and finish is not None and heartbeat is not None
    time_order_ok = start <= finish and start <= heartbeat <= finish + future_tolerance
    _check(
        checks,
        "timestamp_order",
        time_order_ok,
        "started_at must not follow finished_at; heartbeat must belong to this run.",
        contradictions,
        "timestamp_invariant",
    )
    not_future = finish <= clock + future_tolerance and heartbeat <= clock + future_tolerance
    _check(
        checks,
        "not_future_dated",
        not_future,
        "Completion and heartbeat timestamps cannot be materially in the future.",
        contradictions,
        "future_timestamp",
    )

    count_order_ok = (
        counts["items_eligible"] <= counts["items_seen"]
        and counts["items_processed"] <= counts["items_eligible"]
        and counts["items_changed"] <= counts["items_processed"]
    )
    _check(
        checks,
        "count_invariants",
        count_order_ok,
        "changed <= processed <= eligible <= seen.",
        contradictions,
        "count_invariant",
    )

    if evidence:
        observed_times = [_parse_timestamp(item["observed_at"]) for item in evidence]
        evidence_time_ok = all(
            observed is not None
            and start - future_tolerance <= observed <= finish + future_tolerance
            for observed in observed_times
        )
    else:
        evidence_time_ok = True
    _check(
        checks,
        "evidence_timestamps",
        evidence_time_ok,
        "Evidence timestamps must fall within the run window (five-minute tolerance).",
        contradictions,
        "evidence_timestamp_invariant",
    )

    if status == "failed":
        error = receipt.get("error")
        error_ok = (
            isinstance(error, Mapping)
            and set(error) == {"code", "message"}
            and _text(error.get("code"), maximum=100)
            and _text(error.get("message"), maximum=2000)
        )
        _check(
            checks,
            "failure_detail",
            bool(error_ok),
            "Failed runs require an explicit error code and message.",
            contradictions,
            "missing_failure_detail",
        )
        reasons = tuple(dict.fromkeys(contradictions or ["agent_declared_failure"]))
        return Evaluation("failed", evaluated_at, reasons, tuple(checks))

    refusal_free = not _contains_refusal(receipt)
    _check(
        checks,
        "not_error_prose",
        refusal_free,
        "Summary and evidence must not be fluent refusal/error prose.",
        contradictions,
        "error_prose_as_output",
    )

    failed_declared_checks = [
        item["name"] for item in declared_checks if not item["passed"]
    ]
    _check(
        checks,
        "agent_checks_passed",
        not failed_declared_checks,
        "All agent-declared checks passed."
        if not failed_declared_checks
        else f"Failed checks: {', '.join(failed_declared_checks)}",
        contradictions,
        "agent_check_failed",
    )

    if status == "success":
        success_evidence = bool(evidence)
        _check(
            checks,
            "success_has_evidence",
            success_evidence,
            "Declared success requires at least one evidence record.",
            contradictions,
            "success_without_evidence",
        )
        measured_input = counts["items_seen"] > 0 or counts["items_processed"] > 0
        _check(
            checks,
            "success_measured_work",
            measured_input,
            "Declared success requires measured input or processed work.",
            contradictions,
            "success_without_measured_work",
        )
        if receipt.get("no_work") is not None:
            contradictions.append("success_with_no_work_detail")
        if receipt.get("error") is not None:
            contradictions.append("success_with_error")
    else:
        no_work = receipt.get("no_work")
        no_work_ok = (
            isinstance(no_work, Mapping)
            and set(no_work) == {"reason_code", "reason"}
            and _text(no_work.get("reason_code"), maximum=100)
            and _text(no_work.get("reason"), maximum=1000)
        )
        _check(
            checks,
            "no_work_detail",
            bool(no_work_ok),
            "No-work runs require a reason_code and reason.",
            contradictions,
            "missing_no_work_reason",
        )
        zero_work = (
            counts["items_eligible"] == 0
            and counts["items_processed"] == 0
            and counts["items_changed"] == 0
        )
        _check(
            checks,
            "no_work_counts",
            zero_work,
            "No-work requires eligible, processed, and changed counts to be zero.",
            contradictions,
            "no_work_count_contradiction",
        )
        if receipt.get("error") is not None:
            contradictions.append("no_work_with_error")

    if contradictions:
        return Evaluation(
            "degraded_contradictory",
            evaluated_at,
            tuple(dict.fromkeys(contradictions)),
            tuple(checks),
        )

    age = clock - max(finish, heartbeat)
    fresh = -future_tolerance <= age <= stale_after
    checks.append(
        {
            "name": "freshness",
            "passed": fresh,
            "detail": f"Latest run signal is {max(age.total_seconds(), 0):.0f}s old; limit is {stale_after.total_seconds():.0f}s.",
        }
    )
    if not fresh:
        return Evaluation("stale", evaluated_at, ("heartbeat_stale",), tuple(checks))
    verdict = "healthy" if status == "success" else "verified_no_work"
    return Evaluation(verdict, evaluated_at, ("all_invariants_satisfied",), tuple(checks))
