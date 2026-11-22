# 04 · Technical roadmap

Principle: every phase ends with something a paying client uses. Platform work follows
demand.

## Phase 0 — Foundation (this delivery) ✅
Repo, engine (runs, dedupe, approvals, retries, dead letters, audit), simulated integrations,
the AI triage demo, tests, CI config, Docker, docs.

## Phase 1 — First paying clients (weeks 1–8 after a market is chosen)
1. **Pick the vertical** with the business plan (e.g. clinics, agencies, real estate,
   logistics). Adapt the demo's categories, prompts and replies to it. *(business decision)*
2. **Real AI on** with an approved API key: build a 50–100 example eval set per workflow, then
   pick the cheapest model that meets the bar.
3. **Postgres** storage + migrations (Alembic). Back up, and test the restore.
4. **First real connectors** for the chosen market. Likely Gmail/Microsoft 365 intake, one CRM
   (HubSpot or Pipedrive), Postmark/SES email, Slack/Teams notifications for approvals.
5. **Approvals where people are:** approve or reject from Slack/Teams or a signed email link.
6. **Auth** on the API (API keys for webhooks, an OIDC login for operators).
7. **Staging deployment** (needs approval) and an uptime and error alerting setup.
8. **n8n self-hosted** for long-tail client glue, under the licence rules in 01.
9. **Weekly client report** from the audit log: volume, time saved, overrides, errors.

## Phase 2 — Productise (after ~3–5 clients use similar workflows)
- Async workers on a Postgres queue; resume after approval.
- Per-tenant credential vault (OAuth flows, encryption, rotation).
- Customer dashboard v1: approvals inbox, run history, usage.
- Usage metering → Stripe Billing.
- Second and third workflow templates (e.g. document/invoice extraction, appointment booking,
  voice/chat intake).
- Evals in CI; prompt and model versioning.

## Phase 3 — SaaS platform
- Self-serve onboarding and templates, RBAC + SSO, Postgres RLS, audit export.
- Durable execution (Temporal) for long-running workflows.
- Optional customer-facing builder (Activepieces MIT or an n8n Enterprise licence).
- Compliance work (SOC 2) driven by customer demand.

## Production-readiness checklist (none of this is done for production yet)
- [ ] Authentication + authorisation on every endpoint
- [ ] Postgres with RLS, migrations, backups, restore tested
- [ ] Real connectors with sandbox-account integration tests
- [ ] Secrets in a manager; key rotation procedure
- [ ] Monitoring, alerting, on-call runbook
- [ ] Load test at 10× expected volume
- [ ] Data processing agreement + privacy policy for client data
- [ ] AI eval with accuracy accepted by the client
- [ ] Incident response and rollback procedure
