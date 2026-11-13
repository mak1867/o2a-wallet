# Security

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's private vulnerability reporting: the
**Security** tab of this repository, then **Report a vulnerability**. Don't open a public issue.
You'll get a reply within a few days.

## In scope

- **Signature checks:** an offer that is accepted without a valid signature from a trusted issuer,
  or with altered terms.
- **Token handling:** a claim token, promo code or buyer email that reaches the model, the activity
  log, or anyone other than the issuer that should receive it.
- **Settings enforcement:** a way for the wallet's tools to save with saving `off`, claim with
  claiming `never` or without `purchase_approved: true`, or change the user's settings.
- Anything else that lets a malicious offer, page or issuer harm the user through the wallet.

## Out of scope

- What an agent does with shell access: an agent that can run commands can run `o2a-wallet` like
  any other program. Your agent's own permission settings govern that.
- `purchase_approved` being set by the model: it's documented as a guardrail, not proof of consent.
- Issues in the core library belong in [mak1867/o2a](https://github.com/mak1867/o2a).
