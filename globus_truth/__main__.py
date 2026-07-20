"""Command-line entry point for ``python -m globus_truth``."""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import __version__
from .action_gate import ActionGateAuditError, POLICIES
from .approval_center import (
    ApprovalAuditError,
    ApprovalCenterError,
    OUTCOMES as APPROVAL_OUTCOMES,
    RISKS as APPROVAL_RISKS,
)
from .evaluator import evaluate_receipt
from .service import TruthService
from .storage import (
    ActionDecisionConflict,
    ActionProposalConflict,
    ApprovalExecutionConflict,
    HumanApprovalConflict,
    ReceiptConflict,
    TruthRepository,
)
from .web import TruthHTTPServer

DEFAULT_DATABASE = "globus-truth.db"


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _read_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin, parse_constant=_reject_json_constant)
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle, parse_constant=_reject_json_constant)


def _service(args: argparse.Namespace) -> TruthService:
    return TruthService(
        TruthRepository(args.db),
        stale_after=timedelta(hours=args.stale_hours),
    )


def _serve(args: argparse.Namespace, *, load_demo: bool) -> int:
    service = _service(args)
    if load_demo:
        result = service.load_demo()
        print(
            "Loaded demo receipts: "
            + ", ".join(result["verdicts"]),
            flush=True,
        )
    server = TruthHTTPServer((args.host, args.port), service)
    host, port = server.server_address[:2]
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{display_host}:{port}/"
    print(f"Globus Mission Control {__version__} listening at {url}", flush=True)
    print(f"SQLite database: {Path(args.db).resolve()}", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.", flush=True)
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m globus_truth",
        description="Verified outcomes and fail-closed actions for the Globus agent fleet.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def database_options(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--db", default=DEFAULT_DATABASE, help="SQLite database path")
        subparser.add_argument(
            "--stale-hours",
            type=float,
            default=24.0,
            help="heartbeat age that produces a stale verdict (default: 24)",
        )

    for name, help_text in (
        ("serve", "serve the dashboard and API"),
        ("demo", "load five safe scenarios, then serve the dashboard"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        database_options(sub)
        sub.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
        sub.add_argument("--port", type=int, default=8765, help="listen port (default: 8765)")
        sub.add_argument("--open", action="store_true", help="open the dashboard in a browser")

    evaluate = subparsers.add_parser("evaluate", help="evaluate a JSON receipt without storing it")
    evaluate.add_argument("file", help="JSON file, or - for stdin")
    evaluate.add_argument("--stale-hours", type=float, default=24.0)

    ingest = subparsers.add_parser("ingest", help="evaluate and store a JSON receipt")
    database_options(ingest)
    ingest.add_argument("file", help="JSON file, or - for stdin")

    listing = subparsers.add_parser("list", help="list stored receipts as JSON")
    database_options(listing)
    listing.add_argument("--limit", type=int, default=100)

    load = subparsers.add_parser("load-demo", help="append five safe sample receipts")
    database_options(load)

    gate = subparsers.add_parser(
        "gate",
        help="audit a fail-closed action decision from a persisted receipt",
    )
    database_options(gate)
    gate.add_argument("storage_id", help="persisted receipt/storage identifier")
    gate.add_argument(
        "--action-id",
        required=True,
        help="stable identifier for the controlled downstream action",
    )
    gate.add_argument(
        "--policy",
        choices=sorted(POLICIES),
        default="healthy_only",
        help="authorization policy (default: healthy_only)",
    )

    outcome = subparsers.add_parser(
        "outcome-challenge",
        help="prove a healthy allow and contradictory block against local state",
    )
    database_options(outcome)
    outcome.add_argument(
        "--artifact-root",
        help="optional directory for isolated challenge artifacts",
    )

    proposal = subparsers.add_parser(
        "approval-propose",
        help="stage one payload-free exact action for human review",
    )
    database_options(proposal)
    proposal.add_argument("storage_id", help="persisted Truth receipt identifier")
    proposal.add_argument("--proposal-id", required=True)
    proposal.add_argument("--action-id", required=True)
    proposal.add_argument("--action-kind", required=True)
    proposal.add_argument("--payload-sha256", required=True)
    proposal.add_argument("--requested-by", required=True)
    proposal.add_argument("--expires-at", required=True)
    proposal.add_argument(
        "--policy",
        choices=sorted(POLICIES),
        default="healthy_only",
    )
    proposal.add_argument(
        "--risk",
        choices=sorted(APPROVAL_RISKS),
        default="high",
    )

    approval_decide = subparsers.add_parser(
        "approval-decide",
        help="immutably approve or reject one exact action proposal",
    )
    database_options(approval_decide)
    approval_decide.add_argument("proposal_id")
    approval_decide.add_argument(
        "--outcome",
        choices=sorted(APPROVAL_OUTCOMES),
        required=True,
    )
    approval_decide.add_argument("--decided-by", required=True)
    approval_decide.add_argument("--reason-code", required=True)

    approval_list = subparsers.add_parser(
        "approval-list",
        help="list privacy-safe action proposals and derived states",
    )
    database_options(approval_list)
    approval_list.add_argument("--limit", type=int, default=100)
    approval_list.add_argument("--offset", type=int, default=0)

    approval_challenge = subparsers.add_parser(
        "approval-challenge",
        help="stage or resolve the credential-free exact-action proof",
    )
    database_options(approval_challenge)
    approval_challenge.add_argument(
        "--artifact-root",
        help="optional directory for isolated challenge artifacts",
    )
    approval_challenge.add_argument(
        "--proposal-id",
        help="staged challenge proposal to resolve",
    )
    approval_challenge.add_argument(
        "--decision",
        choices=sorted(APPROVAL_OUTCOMES),
        help="human decision for --proposal-id",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if not raw_argv:
        raw_argv = ["demo"]
    args = parser.parse_args(raw_argv)
    if hasattr(args, "stale_hours") and args.stale_hours <= 0:
        parser.error("--stale-hours must be greater than zero")
    if args.command == "approval-challenge" and bool(args.proposal_id) != bool(
        args.decision
    ):
        parser.error("--proposal-id and --decision must be supplied together")
    if args.command in {"serve", "demo"}:
        if not 0 <= args.port <= 65535:
            parser.error("--port must be between 0 and 65535")
        if args.host not in {"127.0.0.1", "localhost", "::1"}:
            parser.error(
                "--host must be 127.0.0.1, localhost, or ::1; "
                "the Truth dashboard and approval API are local-only"
            )
        return _serve(args, load_demo=args.command == "demo")
    try:
        receipt = _read_json(args.file) if hasattr(args, "file") else None
        if args.command == "evaluate":
            result = evaluate_receipt(
                receipt,
                stale_after=timedelta(hours=args.stale_hours),
            ).to_dict()
        elif args.command == "ingest":
            result = _service(args).ingest(receipt)
        elif args.command == "list":
            result = {"runs": _service(args).repository.list_runs(limit=args.limit)}
        elif args.command == "load-demo":
            result = _service(args).load_demo()
        elif args.command == "gate":
            result = _service(args).authorize_action(
                args.storage_id,
                args.action_id,
                policy_id=args.policy,
            )
        elif args.command == "outcome-challenge":
            result = _service(args).run_outcome_gate_challenge(
                artifact_root=args.artifact_root,
            )
        elif args.command == "approval-propose":
            result = _service(args).submit_action_proposal(
                proposal_id=args.proposal_id,
                storage_id=args.storage_id,
                action_id=args.action_id,
                policy_id=args.policy,
                action_kind=args.action_kind,
                payload_sha256=args.payload_sha256,
                requested_by=args.requested_by,
                risk=args.risk,
                expires_at=args.expires_at,
            )
        elif args.command == "approval-decide":
            result = _service(args).decide_action_proposal(
                args.proposal_id,
                outcome=args.outcome,
                decided_by=args.decided_by,
                reason_code=args.reason_code,
            )
        elif args.command == "approval-list":
            result = {
                "proposals": _service(args).list_approval_proposals(
                    limit=args.limit,
                    offset=args.offset,
                )
            }
        elif args.command == "approval-challenge":
            service = _service(args)
            if args.proposal_id:
                result = service.resolve_approval_challenge(
                    args.proposal_id,
                    disposition=args.decision,
                    artifact_root=args.artifact_root,
                )
            else:
                result = service.stage_approval_challenge(
                    artifact_root=args.artifact_root,
                )
        else:  # pragma: no cover - argparse prevents this
            parser.error("unknown command")
            return 2
    except (
        ActionDecisionConflict,
        ActionGateAuditError,
        ActionProposalConflict,
        ApprovalAuditError,
        ApprovalCenterError,
        ApprovalExecutionConflict,
        HumanApprovalConflict,
        OSError,
        json.JSONDecodeError,
        ValueError,
        ReceiptConflict,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "gate":
        return 0 if result["authorized"] else 1
    if args.command == "outcome-challenge":
        return 0 if result.get("expectations_met") is True else 1
    if args.command == "approval-challenge" and args.proposal_id:
        return 0 if result.get("expectations_met") is True else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
