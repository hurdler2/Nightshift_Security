"""Edge agent entry point.

PHASE 1 exposes the operator tooling that exists today: hardware probe and local
credential management. The supervised run loop (event stream -> outbox -> cloud)
arrives with PHASE 2/4.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from nightshift_edge import __version__
from nightshift_edge.cli.probe_cli import main as probe_main
from nightshift_edge.config import load_settings
from nightshift_edge.devices.dahua.cgi_client import DahuaCredentials
from nightshift_edge.secrets.secret_store import SecretStore, SecretStoreError, generate_key

log = logging.getLogger(__name__)


def _cmd_secrets(args: argparse.Namespace) -> int:
    settings = load_settings()
    if args.secrets_command == "generate-key":
        print(generate_key())
        print("Export as EDGE_SECRET_KEY (systemd unit / vault), never commit it.", file=sys.stderr)
        return 0

    try:
        store = SecretStore(settings.secrets_path, settings.secret_key)
    except SecretStoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.secrets_command == "set":
        password = getpass.getpass(f"XVR password for {args.username}@{args.device_id}: ")
        store.put(args.device_id, DahuaCredentials(args.username, password))
        print(f"stored (encrypted) credentials for {args.device_id}")
        return 0
    if args.secrets_command == "list":
        for device_id in store.device_ids():
            print(device_id)
        return 0
    if args.secrets_command == "delete":
        print("deleted" if store.delete(args.device_id) else "not found")
        return 0
    return 2


def _cmd_run(_: argparse.Namespace) -> int:
    print(
        "The supervised edge run loop is not implemented yet.\n"
        "PHASE 1 delivers the Dahua probe; enrollment, heartbeat and event forwarding\n"
        "are PHASE 2 and PHASE 4. Use `nightshift-edge probe --help` for now.",
        file=sys.stderr,
    )
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nightshift-edge", description="Nightshift Security Edge Agent"
    )
    parser.add_argument("--version", action="version", version=f"nightshift-edge {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("probe", add_help=False, help="probe a Dahua device (see probe --help)")
    sub.add_parser("run", help="run the supervised agent (PHASE 2+)")

    secrets = sub.add_parser("secrets", help="manage locally stored XVR credentials")
    secrets_sub = secrets.add_subparsers(dest="secrets_command", required=True)
    secrets_sub.add_parser("generate-key", help="print a new EDGE_SECRET_KEY")
    set_cmd = secrets_sub.add_parser("set", help="store credentials for a device")
    set_cmd.add_argument("device_id")
    set_cmd.add_argument("username")
    secrets_sub.add_parser("list", help="list device ids with stored credentials")
    delete_cmd = secrets_sub.add_parser("delete", help="remove stored credentials")
    delete_cmd.add_argument("device_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "probe":
        return probe_main(argv[1:])

    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    if args.command == "secrets":
        return _cmd_secrets(args)
    if args.command == "run":
        return _cmd_run(args)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
