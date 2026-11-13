"""The MCP server, run as a subprocess by the stdio tests, with its HTTP going to a fake issuer.

Equivalent to ``o2a_wallet.mcp_server.main`` except for the HTTP client, so claims never reach a
real issuer.
"""

from __future__ import annotations

import os
import sys

import httpx

from o2a_wallet.config import ConfigError, load_config
from o2a_wallet.mcp_server import build_server
from o2a_wallet.service import build_wallet

from .helpers import fake_issuer


def main() -> None:
    try:
        config = load_config(os.environ)
    except ConfigError as exc:
        print(f"o2a-wallet: {exc}", file=sys.stderr)
        sys.exit(2)
    client = httpx.Client(transport=httpx.MockTransport(fake_issuer))
    wallet = build_wallet(config, client=client, sleep=lambda _: None)
    build_server(wallet).run("stdio")


if __name__ == "__main__":
    main()
