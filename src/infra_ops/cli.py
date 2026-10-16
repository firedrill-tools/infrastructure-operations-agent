"""Actual fixed synthetic deployment CLI; owned stdin never falls back to human scope."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .store import DeploymentStore
from .source import keys, parse_json, service_name, stable_version
from .state import hex_digest, identifier


def parser():
    p = argparse.ArgumentParser(description="Local synthetic deployment CLI")
    p.add_argument("--db", default="workspace/deployments.db")
    p.add_argument("--owned-input", action="store_true", help=argparse.SUPPRESS)
    commands = p.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("seed")
    seed.add_argument(
        "--fixture", default=str(Path(__file__).parent / "data/starter.json")
    )
    status = commands.add_parser("status")
    status.add_argument("service")
    status.add_argument(
        "--cached",
        action="store_true",
        help="Return a declared historical cached snapshot",
    )
    deploy = commands.add_parser("deploy")
    deploy.add_argument("service")
    deploy.add_argument("version")
    deploy.add_argument("--revision", type=int, required=True)
    for name in ("health", "rollback"):
        command = commands.add_parser(name)
        command.add_argument("service")
        command.add_argument("--revision", type=int, required=True)
    history = commands.add_parser("history")
    history.add_argument("service")
    inspect = commands.add_parser(
        "inspect", help="Private model-free source/current intent preflight"
    )
    inspect.add_argument("--service")
    inspect.add_argument("--version")
    inspect.add_argument("--rollback-on-failure", action="store_true")
    return p


def owned_request():
    raw = sys.stdin.buffer.read(4097)
    if len(raw) > 4096:
        raise ValueError("owned CLI input exceeds retained limit")
    value = keys(
        parse_json(raw.decode("utf-8", errors="strict")), {"credential", "proof"}
    )
    hex_digest(value["credential"])
    if value["proof"] is not None:
        keys(value["proof"], {"id", "token"})
        identifier(value["proof"]["id"])
        hex_digest(value["proof"]["token"])
    return value


def run(args):
    if args.owned_input and args.command not in {
        "status",
        "history",
        "deploy",
        "health",
        "rollback",
    }:
        raise ValueError("owned CLI exposes fixed business commands only")
    # Missing/malformed stdin is an error before DB open, never human fallback.
    owned = owned_request() if args.owned_input else None
    if args.command == "seed":
        store = DeploymentStore.seed(args.db, args.fixture)
        return {
            "database": str(store.path),
            "seeded": True,
            "instance_id": store.instance_id,
        }
    store = DeploymentStore(args.db)
    credential = owned["credential"] if owned else None
    proof = owned["proof"] if owned else None
    if args.command == "status":
        return (
            store.observe(args.service, cached=args.cached, credential=credential)
            if owned
            else store.status(args.service, args.cached)
        )
    if args.command == "history":
        return (
            store.observe(args.service, kind="history", credential=credential)
            if owned
            else store.history(args.service)
        )
    if args.command == "deploy":
        return store.deploy(
            args.service,
            args.version,
            args.revision,
            credential=credential,
            proof=proof,
        )
    if args.command == "health":
        return store.health_check(
            args.service, args.revision, credential=credential, proof=proof
        )
    if args.command == "rollback":
        return store.rollback(
            args.service, args.revision, credential=credential, proof=proof
        )
    if args.command == "inspect":
        source = store.snapshot()
        if (
            args.service is not None
            or args.version is not None
            or args.rollback_on_failure
        ):
            if not args.service or not args.version:
                raise ValueError(
                    "intent preflight requires both --service and --version"
                )
            name, version = service_name(args.service), stable_version(args.version)
            current = store._service(source, name)
            return {
                "service": name,
                "version": version,
                "expected_revision": current["revision"],
                "rollback_on_failure": args.rollback_on_failure,
            }
        return source
    raise ValueError("unknown synthetic CLI command")


def main():
    args = parser().parse_args()
    try:
        print(
            json.dumps(run(args), sort_keys=True, ensure_ascii=False, allow_nan=False),
            flush=True,
        )
    except (ValueError, OSError, sqlite3.Error) as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), flush=True)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
