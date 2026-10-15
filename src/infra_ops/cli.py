"""Synthetic deployment CLI. No cloud SDK or credentials are used."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .store import DeploymentStore


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Local synthetic deployment CLI")
    p.add_argument("--db", default="workspace/deployments.db")
    commands = p.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("seed")
    seed.add_argument("--fixture", default=str(Path(__file__).parents[2] / "fixtures" / "starter.json"))
    status = commands.add_parser("status")
    status.add_argument("service")
    status.add_argument("--cached", action="store_true", help="Return an intentionally stale cached snapshot")
    deploy = commands.add_parser("deploy")
    deploy.add_argument("service"); deploy.add_argument("version"); deploy.add_argument("--revision", type=int, required=True)
    health = commands.add_parser("health")
    health.add_argument("service"); health.add_argument("--revision", type=int, required=True)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("service"); rollback.add_argument("--revision", type=int, required=True)
    history = commands.add_parser("history")
    history.add_argument("service")
    return p


def run(args: argparse.Namespace):
    if args.command == "seed":
        DeploymentStore.seed(args.db, args.fixture)
        return {"database": str(Path(args.db).resolve()), "seeded": True}
    store = DeploymentStore(args.db)
    if args.command == "status":
        return store.status(args.service, args.cached)
    if args.command == "deploy":
        return store.deploy(args.service, args.version, args.revision)
    if args.command == "health":
        return store.health_check(args.service, args.revision)
    if args.command == "rollback":
        return store.rollback(args.service, args.revision)
    if args.command == "history":
        return store.history(args.service)
    raise ValueError("unknown command")


def main() -> None:
    args = parser().parse_args()
    try:
        print(json.dumps(run(args), sort_keys=True))
    except (ValueError, OSError, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}, sort_keys=True))
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
