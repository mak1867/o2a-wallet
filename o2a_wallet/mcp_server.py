"""The stdio MCP server: a thin wrapper over the service.

`MCPServer` is what the MCP Python SDK 2.x calls `FastMCP`.
"""

# No `from __future__ import annotations` here: the SDK must evaluate the tool signatures, and
# they refer to variables local to build_server.

import os
import sys
from collections.abc import Callable
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.resources import TextResource
from mcp.types import CallToolResult, TextContent
from pydantic import PlainValidator, WithJsonSchema

from . import __version__
from .config import ConfigError, load_config
from .service import MAX_NOTE, Result, Wallet, WalletError, build_wallet
from .texts import MESSAGES, PROTOCOL_NAME, PROTOCOL_URI, TOOL_DESCRIPTIONS, render_protocol

SERVER_NAME = "o2a-wallet"


def _schema(description: str | None, **schema: object) -> dict[str, object]:
    return {**schema, "description": description} if description else dict(schema)


def _reply(text: str, structured: Result, *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=structured,
        is_error=is_error,
    )


def _call(
    wallet: Wallet, operation: Callable[[], Result], render: Callable[[Result], str]
) -> CallToolResult:
    """Errors are always tool results with isError, never protocol errors."""
    try:
        result = operation()
    except WalletError as exc:
        return _reply(exc.message, {"error": exc.kind, "message": exc.message}, is_error=True)
    except Exception as exc:
        # Only the type goes to stderr: an exception's text could quote a secret.
        print(f"o2a-wallet: internal error: {type(exc).__name__}", file=sys.stderr)
        message = MESSAGES["internal_error"]
        return _reply(message, {"error": "internal_error", "message": message}, is_error=True)
    return _reply(render(result), result)


def _render_search(result: Result) -> str:
    offers = result["offers"]
    assert isinstance(offers, list)
    if not offers:
        return "No saved offers match."
    return "\n".join(
        f"{o['offer_id']}: {o['terms_text']} from {o['merchant_name']} ({o['product']}), "
        f"{o['status']}, valid until {o['expires']}"
        for o in offers
    )


def _render_claim(result: Result) -> str:
    return (
        f"Promo code: {result['code']}, valid until {result['code_expires']}. "
        f"{result['terms_text']} at {result['merchant_url']}."
    )


def _render_settings(result: Result) -> str:
    issuers = result["trusted_issuers"]
    assert isinstance(issuers, list)
    return (
        f"Saving: {result['saving']}. Save notices: {result['save_notices']}. "
        f"Claiming: {result['claiming']}. Trusted issuers: {', '.join(issuers)}. "
        f"Buyer email set: {'yes' if result['buyer_email_set'] else 'no'}. "
        f"Wallet version {result['version']}."
    )


def build_server(wallet: Wallet) -> MCPServer[Any]:
    """The server instructions follow the user's settings. MCP clients such as Claude Code put
    them in the model's system prompt."""
    config = wallet.config
    texts = TOOL_DESCRIPTIONS
    instructions = config.instructions()
    server: MCPServer[Any] = MCPServer(
        SERVER_NAME, version=__version__, instructions=instructions, log_level="WARNING"
    )
    server.add_resource(
        TextResource(
            uri=PROTOCOL_URI,
            name=PROTOCOL_NAME,
            mime_type="text/markdown",
            text=render_protocol(config.trusted_issuers),
        )
    )

    # Declared as `str` on purpose. For any other annotation the SDK runs json.loads on string
    # arguments, which would hide duplicate members from the strict parser. PlainValidator passes
    # an object through untouched, and the service handles both kinds.
    # Both are optional in the schema; the service requires exactly one. Annotated as plain
    # `str` with a None default (not `str | None`), which keeps the SDK from json.loads-ing them.
    def save_offer(
        block: Annotated[
            str,  # noqa: RUF013 - plain str on purpose, see above
            PlainValidator(lambda value: value),
            WithJsonSchema(
                _schema(texts.save_offer_block, anyOf=[{"type": "object"}, {"type": "string"}])
            ),
        ] = None,  # type: ignore[assignment]
        ref: Annotated[
            str,  # noqa: RUF013 - plain str on purpose, see above
            PlainValidator(lambda value: value),
            WithJsonSchema(_schema(texts.save_offer_ref, type="string")),
        ] = None,  # type: ignore[assignment]
    ) -> CallToolResult:
        return _call(wallet, lambda: wallet.save(block, ref=ref), lambda r: str(r["message"]))

    def search_offers(
        query: Annotated[
            str | None,
            WithJsonSchema(_schema(texts.search_offers_query, type="string")),
        ] = None,
    ) -> CallToolResult:
        return _call(wallet, lambda: wallet.search(query), _render_search)

    def claim_offer(
        offer_id: Annotated[
            str, WithJsonSchema(_schema(texts.claim_offer_offer_id, type="string"))
        ],
        # Optional on purpose: leaving it out gets the wallet's own purchase_not_approved message
        # rather than a schema validation error.
        purchase_approved: Annotated[
            bool,
            WithJsonSchema(_schema(texts.claim_offer_purchase_approved, type="boolean")),
        ] = False,
    ) -> CallToolResult:
        return _call(
            wallet,
            lambda: wallet.claim(offer_id, purchase_approved=purchase_approved),
            _render_claim,
        )

    def mark_used(
        offer_id: Annotated[str, WithJsonSchema(_schema(texts.mark_used_offer_id, type="string"))],
        note: Annotated[
            str | None,
            WithJsonSchema(_schema(texts.mark_used_note, type="string", maxLength=MAX_NOTE)),
        ] = None,
    ) -> CallToolResult:
        return _call(
            wallet,
            lambda: wallet.mark_used(offer_id, note),
            lambda r: f"Marked {r['offer_id']} as used.",
        )

    server.add_tool(save_offer, description=texts.save_offer)
    server.add_tool(search_offers, description=texts.search_offers)
    server.add_tool(claim_offer, description=texts.claim_offer)
    server.add_tool(mark_used, description=texts.mark_used)

    def wallet_settings() -> CallToolResult:
        return _call(wallet, wallet.settings, _render_settings)

    server.add_tool(wallet_settings, description=texts.wallet_settings)
    return server


def main() -> None:
    try:
        wallet = build_wallet(load_config(os.environ))
    except ConfigError as exc:
        print(f"o2a-wallet: {exc}", file=sys.stderr)
        sys.exit(2)
    except OSError as exc:
        print(f"o2a-wallet: cannot open the wallet: {exc.strerror}", file=sys.stderr)
        sys.exit(2)
    build_server(wallet).run("stdio")
