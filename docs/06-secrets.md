# 06 · Secret management

## Rules
1. **No secret is ever committed**, pasted into chat, or written into docs, tickets or logs.
2. Code reads secrets only from **environment variables** (e.g. `ANTHROPIC_API_KEY`). Nothing in
   the repo holds a value. `.env.example` lists names only.
3. Every client gets **separate credentials**, scoped to the minimum permissions, ideally a
   sandbox account first.
4. Rotate on staff change, on suspected leak, and at least yearly.

## Local development
- `cp .env.example .env` and fill in values. `.env` is git-ignored.
- Better: export secrets from a password manager CLI (1Password `op run`, Bitwarden, or
  Doppler/Infisical free tiers) so they never touch disk:
  `op run --env-file=.env -- flow demo`.

## Guardrails already in the repo
- `.gitignore` blocks `.env*` (except `.env.example`).
- `scripts/check_no_secrets.sh` is installed as a git pre-commit hook. It blocks `.env` files
  and common key patterns (Anthropic, OpenAI-style, AWS, Slack, GitHub, private keys).
- CI runs gitleaks on every push.
- The audit logger redacts keys named like `api_key`, `token`, `password`, `secret`,
  `authorization`.
- The Anthropic client is created with no literal key. The SDK reads it from the environment.

## Staging / production
- Use the hosting provider's secret store, or a dedicated manager (Infisical self-hosted, Doppler,
  AWS/GCP Secret Manager). Inject secrets as env vars at runtime.
- Client OAuth tokens (Phase 2) go in a per-tenant encrypted vault (envelope encryption with a
  KMS key), never in plain DB columns.

## If a secret leaks
Revoke it at the provider first, then rotate, then purge it from git history, then review the
audit logs for misuse.
