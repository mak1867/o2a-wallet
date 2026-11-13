"""The command line, for the user only. Never exposed over MCP. All logic is in the service."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping

from . import settings as st
from .config import ConfigError, load_config
from .service import Result, WalletError, build_wallet

NEXT_SESSION = "Changes take effect in the agent's next session."
_SET_KEYS = ("saving", "notices", "claiming", "issuers", "email")


def _print_table(offers: list[Result]) -> None:
    if not offers:
        print("No offers.")
        return
    columns = ("offer_id", "status", "expires", "merchant_name", "terms_text")
    rows = [[str(offer[c]) for c in columns] for offer in offers]
    widths = [max(len(r[i]) for r in [list(columns), *rows]) for i in range(len(columns))]
    for row in [list(columns), *rows]:
        print(
            "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="o2a-wallet", description="Manage the O2A wallet.")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="a table of offers (default: active and claimed)")
    listing.add_argument("--all", action="store_true", help="include every status")
    commands.add_parser("show", help="full details of one offer").add_argument("offer_id")
    commands.add_parser("remove", help="delete one offer").add_argument("offer_id")
    reset = commands.add_parser("reset", help="empty the offers table")
    reset.add_argument("--yes", action="store_true", help="required: confirms the reset")
    reset.add_argument("--keys", action="store_true", help="also empty the key cache")
    commands.add_parser(
        "add", help="save an offer from a page or file you found yourself"
    ).add_argument("target", metavar="url-or-file")
    commands.add_parser("stats", help="counts of offers by status, and recent activity")

    init = commands.add_parser("init", help="create the settings file (~/.o2a/config.toml)")
    init.add_argument("--saving", choices=st.SAVING_MODES, default="auto")
    init.add_argument("--notices", choices=st.NOTICE_MODES, default="every")
    init.add_argument("--claiming", choices=st.CLAIMING_MODES, default="with_purchase")
    init.add_argument(
        "--issuer", action="append", metavar="HOST",
        help=f"a trusted issuer; repeat for several (default: {st.DEFAULT_ISSUER})",
    )  # fmt: skip

    config = commands.add_parser("config", help="show or change the wallet's settings")
    actions = config.add_subparsers(dest="action", required=True)
    actions.add_parser("show", help="the effective settings and where each came from")
    setting = actions.add_parser("set", help="change one setting in the settings file")
    setting.add_argument("key", choices=sorted(_SET_KEYS))
    setting.add_argument("value")
    actions.add_parser("unset", help="remove an optional setting").add_argument(
        "key", choices=["email"]
    )
    return parser


def _init(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    path = st.config_path(env)
    if path.exists():
        print(
            f"o2a-wallet: {path} already exists; change it with `o2a-wallet config set`",
            file=sys.stderr,
        )
        return 1
    issuers = st.check_issuers("--issuer", args.issuer or [st.DEFAULT_ISSUER])
    st.write_file(
        path,
        st.render_file(
            saving=args.saving, notices=args.notices, claiming=args.claiming,
            issuers=issuers, email=None,
        ),
    )  # fmt: skip
    print(f"Created {path}.")
    _print_settings(st.load_settings(env), path)
    return 0


def _print_settings(settings: st.Settings, path: object) -> None:
    rows = [
        ("saving", settings.saving),
        ("notices", settings.notices),
        ("claiming", settings.claiming),
        ("issuers", ", ".join(settings.trusted_issuers) or "(none)"),
        ("email", "set" if settings.buyer_email else "not set"),
    ]
    print(f"Settings file: {path}")
    width = max(len(name) for name, _ in rows)
    for name, value in rows:
        print(f"  {name.ljust(width)}  {value}  [{settings.sources.get(name, 'default')}]")


def _config(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    path = st.config_path(env)
    if args.action == "show":
        _print_settings(st.load_settings(env), path if path.exists() else f"{path} (absent)")
        return 0
    if not path.exists():
        print(f"o2a-wallet: no settings file at {path}; run `o2a-wallet init` first",
              file=sys.stderr)  # fmt: skip
        return 1
    current = st.read_file(path)
    values: dict[str, object] = {
        "saving": current.get("saving", "auto"),
        "notices": current.get("notices", "every"),
        "claiming": current.get("claiming", "with_purchase"),
        "issuers": current.get("issuers", ()),
        "email": current.get("email"),
    }
    name = args.key
    if args.action == "unset":
        values[name] = None
    elif name == "saving":
        values[name] = st.check_choice("saving", args.value, st.SAVING_MODES)
    elif name == "notices":
        values[name] = st.check_choice("notices", args.value, st.NOTICE_MODES)
    elif name == "claiming":
        values[name] = st.check_choice("claiming", args.value, st.CLAIMING_MODES)
    elif name == "issuers":
        issuers = st.check_issuers("issuers", st.split_issuers(args.value))
        if not issuers:
            raise st.SettingsError("issuers: give at least one issuer hostname")
        values[name] = issuers
    else:
        values[name] = st.check_email("email", args.value)
    st.write_file(
        path,
        st.render_file(
            saving=str(values["saving"]), notices=str(values["notices"]),
            claiming=str(values["claiming"]),
            issuers=list(values["issuers"]),  # type: ignore[call-overload]
            email=values["email"],  # type: ignore[arg-type]
        ),
    )  # fmt: skip
    if args.action == "unset":
        print(f"Removed {args.key} from {path}. {NEXT_SESSION}")
    else:
        shown = "(set)" if name == "email" else args.value
        print(f"Set {args.key} to {shown} in {path}. {NEXT_SESSION}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command in {"init", "config"}:
        try:
            return (_init if args.command == "init" else _config)(args, os.environ)
        except st.SettingsError as exc:
            print(f"o2a-wallet: {exc}", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"o2a-wallet: cannot write the settings file: {exc.strerror}", file=sys.stderr)
            return 2
    try:
        # Only `add` verifies an offer, so only `add` needs to know who to trust.
        config = load_config(os.environ, require_issuers=args.command == "add")
        wallet = build_wallet(config)
    except ConfigError as exc:
        print(f"o2a-wallet: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"o2a-wallet: cannot open the wallet: {exc.strerror}", file=sys.stderr)
        return 2
    try:
        if args.command == "list":
            _print_table(wallet.list_offers(include_all=args.all))
        elif args.command == "show":
            print(json.dumps(wallet.show(args.offer_id), indent=2, ensure_ascii=False))
        elif args.command == "remove":
            wallet.remove(args.offer_id)
            print(f"Removed {args.offer_id}.")
        elif args.command == "reset":
            if not args.yes:
                print("o2a-wallet: reset needs --yes", file=sys.stderr)
                return 2
            print(f"Removed {wallet.reset(keys=args.keys)} offers.")
        elif args.command == "add":
            result = wallet.add(args.target)
            print(f"{result['outcome']}: {result['message']}")
            return 0 if result["outcome"] == "valid" else 1
        elif args.command == "stats":
            print(json.dumps(wallet.stats(), indent=2))
    except WalletError as exc:
        print(f"o2a-wallet: {exc.message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
