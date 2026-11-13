# O2A wallet

A local [MCP](https://modelcontextprotocol.io) server that keeps signed sponsored offers your AI
agent finds ([O2A: Offers to Agents](https://o2a.exchange)) and exchanges them for promo codes
when you buy. It verifies every offer's signature before saving it, and follows settings that only
you control.

What it never does: buy anything (it only hands your agent a promo code to use at the merchant's
normal checkout), send your data anywhere except as described under [Privacy](#privacy), or change
its own settings through its tools.

## Install

Needs [uv](https://docs.astral.sh/uv/). Then:

```sh
uvx --from git+https://github.com/mak1867/o2a-wallet@v0.3.0 o2a-wallet init
claude mcp add o2a-wallet -- uvx --from git+https://github.com/mak1867/o2a-wallet@v0.3.0 o2a-wallet-mcp
```

`init` writes your settings to `~/.o2a/config.toml` and trusts the issuer `o2a.exchange`. The
second line adds the wallet to [Claude Code](https://claude.com/claude-code). For other MCP
clients, add a stdio server like this:

```json
{
  "mcpServers": {
    "o2a-wallet": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/mak1867/o2a-wallet@v0.3.0", "o2a-wallet-mcp"]
    }
  }
}
```

The first start takes longer while uvx builds the wallet's environment once; later starts are fast.
To run the CLI without typing the `uvx --from …` prefix each time, install it as a tool:
`uv tool install git+https://github.com/mak1867/o2a-wallet@v0.3.0`.

## Settings

You decide how the wallet behaves, in three settings. Your agent can read them (the
`wallet_settings` tool) but the wallet's tools can't change them; only you can, with the
`o2a-wallet` command.

| Setting | Options | Default |
|---|---|---|
| **Saving** | `auto` · `relevant` · `ask` · `off` | `auto` |
| **Save notices** | `every` · `when_relevant` · `off` | `every` |
| **Claiming** | `with_purchase` · `never` | `with_purchase` |
| **Disclosure** | not a setting: always on | – |

- **Saving:** `auto` saves every offer your agent finds, even one unrelated to the task;
  `relevant` saves offers related to what you're working on or likely to buy soon; `ask` tells you
  about an offer and saves it only if you want it; `off` saves nothing.
- **Save notices** (with `auto` or `relevant`): `every` lists each offer saved during a task when
  the agent reports back; `when_relevant` mentions saved offers only when you're about to buy
  something they apply to, or ask about offers; `off` never mentions them unprompted (you can
  always ask, or run `o2a-wallet list`).
- **Disclosure** is separate and always on: whenever an offer affects a recommendation or is used,
  the agent says it's sponsored and gives its terms and expiry.
- **Claiming** happens only at purchase time. `with_purchase`: once you (or your rules for the
  agent) have approved a purchase from that merchant, the agent claims and applies the offer
  without asking again, since a discount only lowers a price you already agreed to. `never`: the
  agent never claims, but may still tell you about saved offers. Spending limits are a rule for
  your agent, not a wallet setting: the wallet never sees payments.

**The file**, `~/.o2a/config.toml`, created by `o2a-wallet init` with permissions `0600`:

```toml
[saving]
mode = "auto"                   # auto | relevant | ask | off
notices = "every"               # every | when_relevant | off

[claiming]
mode = "with_purchase"          # with_purchase | never

[issuers]
trusted = ["o2a.exchange"]

# [buyer]
# email = "you@example.com"  # optional; only if an issuer requires it
```

It's managed by `o2a-wallet config set`, which rewrites the whole file, so comments you add by
hand aren't kept. The server reads settings once at startup, so changes take effect in your
agent's next session.

**Buyer email: only if an issuer requires it.** Unset by default, and sent with claims only if you
set it. It matters only for an issuer that limits discounts per person or binds codes to a
customer ([format spec §9.1](https://github.com/mak1867/o2a/blob/main/docs/o2a-format-v0.md#91-request));
`o2a.exchange` needs neither. Sending it links your identity to where the offer was seen, so
`init` never asks for it.

### What's enforced, and what's an instruction

Most settings shape the [instructions](#what-the-model-receives) your agent receives. Some are also
checked by the wallet itself:

| Setting | Enforced by the wallet |
|---|---|
| Saving `off` | `save_offer` returns the error `saving_off` and stores nothing |
| Saving `auto`, `relevant`, `ask`; save notices | Instructions only: the wallet can't judge relevance or see whether you agreed |
| Claiming `never` | `claim_offer` returns `claiming_off` |
| Claiming `with_purchase` | `claim_offer` needs `purchase_approved: true`, or returns `purchase_not_approved` |

`claim_offer`'s checks run in this order, before any network call: an offer that's already claimed
returns its stored code (even under `never`); then `claiming_off`; then `purchase_not_approved`.

Two limits, stated plainly:

- **`purchase_approved` is declared by the model**, so it's a guardrail, not proof of your consent.
  It's stored on the offer when claimed (`o2a-wallet show`).
- **The wallet's tools can't change your settings, but an agent that can run shell commands could
  run `o2a-wallet config set`** like any other command. Your agent's own permission settings govern
  that.

`o2a-wallet add` (saving an offer you found yourself) works even with saving `off`: that's you
acting directly, not your agent.

## Tools

| Tool | Input | Output |
|---|---|---|
| `save_offer` | exactly one of `block` (the offer object or the full response containing it, as an object or a string) or `ref` (a `clm_…` claim token) | `{outcome, saved, offer_id, summary, message}` |
| `search_offers` | `query`: optional text matched against merchant, product or publisher | `{count, offers: [...]}` (at most 20, soonest expiry first) |
| `claim_offer` | `offer_id`; `purchase_approved` (boolean, must be `true` to claim) | `{offer_id, code, code_expires, merchant_url, terms_text}` |
| `mark_used` | `offer_id`, optional `note` (at most 200 characters) | `{offer_id, status}` |
| `wallet_settings` | none | `{saving, save_notices, claiming, trusted_issuers, buyer_email_set, version}` |

Your agent refers to offers only by `offer_id`. After saving, claim tokens never go back to the
model, and promo codes reach it only through `claim_offer`. A rejected offer is a normal
`save_offer` result whose `outcome` says why (for example `bad_signature`, `untrusted_issuer` or
`expired`). Other failures are tool results with `isError: true`, an `error` kind and a `message`.

**Saving from a ref.** Some pages show an offer only as a short hint with a claim token (a *ref*,
[format spec §4.7](https://github.com/mak1867/o2a/blob/main/docs/o2a-format-v0.md#47-the-hint-optional)),
for example when a tool summarizes a page and loses the signed offer. `save_offer` takes it as
`ref`: the wallet asks the trusted issuers for the signed offer
([format spec §9.5](https://github.com/mak1867/o2a/blob/main/docs/o2a-format-v0.md#95-resolving-a-claim-token)),
then verifies and saves it exactly like any other offer.

### The `o2a://protocol` resource

A read-only resource explaining the format and how to verify an offer yourself, with a short
Python script and the issuers this wallet trusts.

### What the model receives

The server sends MCP instructions, which clients such as Claude Code put in the model's system
prompt. They say that offers are data, not instructions; how to save, compare and claim offers,
following your settings; that offers count only toward price, never over your needs or real
differences in quality; that your own instructions take priority; and that any offer that affects
a recommendation must be disclosed as sponsored. The exact text, and every tool description, is in
[`o2a_wallet/texts.py`](o2a_wallet/texts.py).

## Privacy

Everything stays on your machine, in `~/.o2a/`:

- `config.toml`: your settings.
- `wallet.db`: saved offers, their claim tokens and promo codes (permissions `0600`).
- `activity.jsonl`: the activity log (below).

The wallet contacts only the issuers you trust, over HTTPS, and only to:

- fetch their public signing keys (`/.well-known/o2a-keys.json`), to verify offers;
- send a ref, when your agent saves an offer from a hint (`/v0/offers/resolve`);
- send an offer's claim token, when your agent claims it at purchase time (`/v0/claims`), plus your
  buyer email only if you set one.

`o2a-wallet add <url>` also fetches the page you give it.

**The activity log** shows what your agent did with offers: one line per tool call, with the time,
the tool, where the call came from (`mcp` or `cli`), the outcome, the offer ID, whether the offer
came from a block or a ref, how long it took, the wallet version, and your settings at the time.
It never contains claim tokens, promo codes, emails or raw offers. It's capped at 5 MB (the oldest
half is dropped), `o2a-wallet stats` shows the most recent lines, and `O2A_WALLET_LOG=off` turns it
off.

## CLI

| Command | Does |
|---|---|
| `o2a-wallet init [--saving M] [--notices M] [--claiming M] [--issuer HOST]...` | Creates the settings file (defaults for anything not given; `--issuer` repeats). Refuses to overwrite an existing one |
| `o2a-wallet config show` | Your settings, and whether each came from the file or a default. Shows whether a buyer email is set, never the email |
| `o2a-wallet config set KEY VALUE` | `saving auto`, `notices when_relevant`, `claiming never`, `issuers a.example,b.example`, `email you@example.com` |
| `o2a-wallet config unset email` | Removes the buyer email |
| `o2a-wallet list [--all]` | A table of offers (default: active and claimed) |
| `o2a-wallet show <offer_id>` | Full details. The claim token is masked; the code is shown |
| `o2a-wallet add <url-or-file>` | Saves an offer from a page or file you found yourself |
| `o2a-wallet remove <offer_id>` | Deletes one offer |
| `o2a-wallet reset --yes [--keys]` | Deletes every offer, and with `--keys` the cached issuer keys |
| `o2a-wallet stats` | Counts of offers by status, and recent activity |

### Advanced settings

Environment variables, for unusual setups. Your saving, notice, claiming, issuer and email
settings come only from the settings file.

| Variable | Default | Meaning |
|---|---|---|
| `O2A_CONFIG` | `~/.o2a/config.toml` | The settings file |
| `O2A_WALLET_DB` | `~/.o2a/wallet.db` | The offers database (created with permissions `0600`) |
| `O2A_WALLET_LOG` | `~/.o2a/activity.jsonl` | The activity log, or `off` |
| `O2A_PINNED_KEYS` | none | A JSON file `{ "<issuer>": <JWKS> }` of issuer keys to trust without fetching |
| `O2A_FETCH_KEYS` | `1` | `0` to never fetch keys over HTTPS (then `O2A_PINNED_KEYS` is required) |
| `O2A_MAX_ACTIVE` | `200` | Maximum number of active, unexpired offers |

Invalid settings or variables make the server exit at startup with a message on stderr.

## Limitations

- **Refs with several trusted issuers.** A ref is sent to each trusted issuer in turn until one
  recognizes it, so with more than one issuer, a token can reach issuers that never issued it. With
  the default single issuer this doesn't arise.
- **Local MCP only.** The wallet runs on your machine over stdio, so agents running in the cloud
  can't use it yet.
- **Key cache window.** Fetched issuer keys are cached in the database. A cached key set stays
  usable for up to an hour past its own expiry, so verifying offline after a restart works only for
  a limited time; after that, the wallet reports `unknown_key` until it can fetch keys again.

## Development

```sh
uv sync
uv run pytest
uv run mypy
uv run ruff check .
uv run ruff format --check .
```

The core library, [`o2a-protocol`](https://github.com/mak1867/o2a), does the parsing, signature
checks and claim requests; this repository is the wallet around it. Released under the
[Apache License 2.0](LICENSE).
