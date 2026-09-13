#!/usr/bin/env python3
"""Probe a real Dahua XVR and write a capability report (spec §50).

Usage:

    python scripts/dahua_probe.py --host 192.168.1.108 --username nightshift --ask-password

Run it from the repo root; the edge-agent package is added to sys.path automatically
so no install is required for a field engineer with only a checkout.
"""

from __future__ import annotations

import sys
from pathlib import Path

EDGE_ROOT = Path(__file__).resolve().parent.parent / "edge-agent"
if str(EDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(EDGE_ROOT))

from nightshift_edge.cli.probe_cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
