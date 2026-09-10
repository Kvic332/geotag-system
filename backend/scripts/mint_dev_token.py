#!/usr/bin/env python
"""Mint an HS256 dev JWT for local testing (SPEC-DECISIONS D6).

`shared.auth.require_auth` verifies, for `ENVIRONMENT=local`, an HS256 token
signed with `DEV_JWT_SECRET` and carrying a `custom:tenant_id` claim. This
script mints exactly that, for curl / the dashboard's TokenBar to use.

Usage:

    python backend/scripts/mint_dev_token.py --tenant-id acme
    python backend/scripts/mint_dev_token.py --tenant-id acme --subject alice --ttl-hours 12

Reads DEV_JWT_SECRET from the environment (or --secret). Never hardcode a
real secret here — this script is fine to commit, a secret value is not.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/ on sys.path

import jwt  # noqa: E402


def build_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True, help="value for the custom:tenant_id claim")
    parser.add_argument("--subject", default=None, help="value for the sub claim (default: random uuid)")
    parser.add_argument("--ttl-hours", type=float, default=24.0, help="token lifetime in hours (default 24)")
    parser.add_argument(
        "--secret",
        default=None,
        help="signing secret (default: $DEV_JWT_SECRET)",
    )
    return parser.parse_args()


def main() -> int:
    args = build_args()

    secret = args.secret or os.environ.get("DEV_JWT_SECRET")
    if not secret:
        print("error: no signing secret. Set DEV_JWT_SECRET or pass --secret.", file=sys.stderr)
        return 1

    now = int(time.time())
    claims = {
        "sub": args.subject or str(uuid.uuid4()),
        "custom:tenant_id": args.tenant_id,
        "iat": now,
        "exp": now + int(args.ttl_hours * 3600),
    }

    token = jwt.encode(claims, secret, algorithm="HS256")
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
