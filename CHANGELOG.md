# Changelog

## 0.3.0

First public release.

The O2A wallet is a local MCP server that keeps signed sponsored offers ([O2A format](https://github.com/mak1867/o2a/blob/main/docs/o2a-format-v0.md)) for you and exchanges them for promo codes when you buy:

- **Verifies before saving.** Every offer's Ed25519 signature is checked against its issuer's published keys, and only issuers you trust are accepted.
- **Five tools:** `save_offer` (from an offer or a ref), `search_offers`, `claim_offer`, `mark_used`, and the read-only `wallet_settings`.
- **Your settings, in `~/.o2a/config.toml`:** saving (`auto`, `relevant`, `ask`, `off`), save notices (`every`, `when_relevant`, `off`) and claiming (`with_purchase`, `never`). The server instructions follow them; saving `off` and claiming `never` are enforced by the wallet, and claims need `purchase_approved: true`.
- **A command line** to set up (`init`), change settings (`config`), and inspect and manage offers (`list`, `show`, `add`, `remove`, `reset`, `stats`).
- **Local and private:** offers, codes and a small activity log stay in `~/.o2a/`. Claim tokens never reach the model or the log.
