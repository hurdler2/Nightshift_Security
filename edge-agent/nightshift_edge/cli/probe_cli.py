"""Command line front-end for the Dahua hardware probe (spec §50).

    python scripts/dahua_probe.py --host 192.168.1.108 --username nightshift --ask-password

The password is read interactively or from an environment variable — never from a
command-line flag, so it stays out of shell history and the process table (spec §51).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from nightshift_edge.devices.dahua.cgi_client import DahuaCredentials
from nightshift_edge.devices.dahua.probe import (
    DahuaProbe,
    ProbeOptions,
    example_commands,
    summarize,
)
from nightshift_edge.security.redaction import redact_text

PASSWORD_ENV = "DAHUA_PASSWORD"  # noqa: S105 - env var name, not a secret


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dahua_probe",
        description="Probe a Dahua XVR/NVR and write a JSON capability report.",
        epilog=f"Password: --ask-password, or set {PASSWORD_ENV}.",
    )
    parser.add_argument("--host", required=True, help="XVR IP or hostname (LAN address)")
    parser.add_argument("--username", required=True, help="XVR service account (not admin)")
    parser.add_argument("--ask-password", action="store_true", help="prompt for the password")
    parser.add_argument("--http-port", type=int, default=80)
    parser.add_argument("--https", action="store_true", help="use https for the CGI API")
    parser.add_argument("--insecure", action="store_true", help="skip TLS verification")
    parser.add_argument("--rtsp-port", type=int, default=554)
    parser.add_argument("--channels", type=int, default=8, help="max channels to test")
    parser.add_argument(
        "--listen", type=float, default=60.0, help="event stream listen window in seconds"
    )
    parser.add_argument(
        "--codes",
        default="[All]",
        help="attach codes filter, e.g. '[SmartMotionHuman,SmartMotionVehicle]'",
    )
    parser.add_argument("--heartbeat", type=int, default=5)
    parser.add_argument("--main-stream", action="store_true", help="also probe RTSP main streams")
    parser.add_argument("--no-playback", action="store_true", help="skip the playback test")
    parser.add_argument(
        "--playback-offset",
        type=int,
        default=5,
        help="minutes back from now for the playback window",
    )
    parser.add_argument(
        "--trigger-channel",
        type=int,
        help="logical channel you will trigger SMD on (pins the index mapping)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="report path (default: probe-reports/<host>-<timestamp>.json)",
    )
    parser.add_argument("--print-commands", action="store_true", help="print manual curl tests")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def resolve_password(args: argparse.Namespace) -> str:
    if args.ask_password:
        return getpass.getpass(f"Password for {args.username}@{args.host}: ")
    password = os.environ.get(PASSWORD_ENV)
    if password:
        return password
    if sys.stdin.isatty():
        return getpass.getpass(f"Password for {args.username}@{args.host}: ")
    raise SystemExit(f"no password supplied: use --ask-password or export {PASSWORD_ENV}")


def default_output_path(host: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_host = host.replace(":", "_").replace("/", "_")
    return Path("probe-reports") / f"{safe_host}-{stamp}.json"


async def run(args: argparse.Namespace) -> int:
    options = ProbeOptions(
        host=args.host,
        credentials=DahuaCredentials(args.username, resolve_password(args)),
        http_port=args.http_port,
        scheme="https" if args.https else "http",
        rtsp_port=args.rtsp_port,
        channels=args.channels,
        listen_seconds=args.listen,
        event_codes=args.codes,
        heartbeat_seconds=args.heartbeat,
        test_main_stream=args.main_stream,
        test_playback=not args.no_playback,
        playback_offset_minutes=args.playback_offset,
        trigger_channel=args.trigger_channel,
        verify_tls=not args.insecure,
    )

    def progress(message: str) -> None:
        print(redact_text(message), flush=True)

    report = await DahuaProbe(options, progress=progress).run()
    print(summarize(report))

    output = args.output or default_output_path(args.host)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"report written to {output}")

    if args.print_commands:
        print("\nManual equivalents (spec §51):")
        for command in example_commands(args.host, args.username):
            print(f"  {command}")

    return 0 if not report.errors else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
