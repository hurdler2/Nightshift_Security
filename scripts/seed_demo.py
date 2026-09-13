#!/usr/bin/env python3
"""Seed demo tenant/site/device data.

Placeholder: seeding needs the schema from PHASE 3 (tenants, sites, devices,
cameras). Rather than inventing a shape that will not match, this script states what
it is waiting for and exits non-zero.

What it will do once the models exist:

    * demo tenant + owner user (Argon2 password from an env var, never a literal)
    * one site with an IANA timezone (spec §57)
    * one edge gateway with an enrollment code
    * one XVR device plus its channel map, seeded from a real probe report so the
      demo data matches an actual device (`probe-reports/*.json`)
    * five cameras with names, zones, and per-camera AI modes
"""

from __future__ import annotations

import sys

MESSAGE = """\
seed_demo.py is not usable yet.

It depends on the PHASE 3 schema (tenants, sites, edge_gateways, devices, cameras).
Fabricating rows before those models exist would only have to be rewritten.

Available today:
  python scripts/dahua_probe.py --host <XVR_IP> --username <user> --ask-password

The JSON report it writes will be the input for this seeder.
"""


def main() -> int:
    print(MESSAGE, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())


# TODO(V1-BLOCKER): PHASE 3 — implement against the real models, taking a probe
# report path as input so demo channel maps match a verified device.
