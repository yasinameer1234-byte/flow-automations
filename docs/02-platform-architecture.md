# 02 · Future platform architecture (design only)

This is a target for Phases 2–3. **Nothing here is built yet except the parts marked ✅.**
We build each piece only when a paying customer needs it.

```
                    ┌─────────────────────── Customer dashboard (Next.js) ─────────────────────┐
                    │ runs · approvals inbox · audit · usage · integrations · settings          │
                    └──────────────▲───────────────────────────────────────────────▲───────────┘
                                   │ HTTPS (session / OIDC)                         │
  Webhooks / email / forms ──► API gateway (FastAPI) ──► Auth & RBAC ──► Tenant context
  Zapier / Make / n8n                │
                                     ▼
                     Workflow service ✅(v0: in-process)  ──►  Durable executor (Postgres queue → Temporal)
                        │  runs ✅  idempotency ✅  approvals ✅  retries ✅  dead letters ✅
                        ├──► AI gateway: provider adapters ✅, model policy, token metering, evals
                        ├──► Integration hub: connectors ✅(interfaces), credential vault, rate limits
                        └──► Audit log ✅ (append-only) ──► export / SIEM
                                     │
                              Postgres (tenant_id + RLS) · object storage · metrics/billing events
```

## 1. Multi-tenant customers
- **Model:** shared database, shared schema, `tenant_id` on every row (✅ already true in the
  SQLite schema). Postgres **row-level security** enforces isolation at the DB layer, so a missed
  `WHERE` clause cannot leak data. A tenant is set per request (`SET app.tenant_id`).
- **Escalation path:** a large or regulated client can get a dedicated database or a dedicated
  deployment, using the same code and a different connection string.
- **Per-tenant config:** policies (✅ `config/tenants/*.toml` today, moving to a DB table),
  enabled workflows, model choice, approval rules, data retention.
- **Tests:** tenant-isolation tests on every new table (✅ `test_tenant_isolation`).

## 2. Authentication and permissions
- **Buy, don't build:** a hosted OIDC provider (WorkOS, Clerk, Auth0) or self-hosted Keycloak.
  We need SSO/SAML for business customers and that's expensive to build.
- **Roles (RBAC):** `owner`, `admin`, `operator` (can approve actions), `viewer`, plus internal
  `flow_staff` with audited, time-boxed support access.
- **Approvals carry identity:** today `decided_by` is a string (✅). Later it's the
  authenticated user id, and approval rights can be scoped per action type and amount (e.g.
  refunds over $500 need `admin`).
- **Machine access:** per-tenant API keys (hashed at rest, scoped, rotatable) for webhooks.

## 3. Workflow execution
- **v0 (✅):** synchronous in-process engine, run records, dedupe keys, policy-gated actions,
  retry with backoff, dead letters.
- **v1:** a Postgres-backed job queue (`SELECT … FOR UPDATE SKIP LOCKED`) with workers. This
  gives async intake, scheduled retries and a resume after approval without an extra service.
- **v2:** Temporal (MIT) for long-running, multi-step, multi-day workflows (onboarding,
  collections) once v1's limits actually hurt.
- **Workflow definition:** code-first Python modules with a declarative route table (✅ see
  `ROUTES` in `inbound_triage`), versioned with the code. Every run records the workflow version.
- **Guarantees:** at-least-once delivery + idempotency keys on every external side effect
  (✅ email and refund simulators honour them).

## 4. AI agent orchestration
- **Default to workflows, not autonomous agents.** Code decides the control flow. AI does
  bounded jobs (classify, extract, draft, summarise) with schema-validated output (✅).
- **Agents where they earn it:** e.g. research or multi-step support resolution. Agent tool
  calls go through the same `propose()` gate (✅ pattern), so consequential tools always need
  approval. Agents get read-only tools by default.
- **AI gateway:** one place for provider adapters, model routing per tenant, prompt versions,
  token and cost metering, PII redaction, and the outage → human-review fallback (✅).
- **Quality:** an eval set per workflow per client (golden examples with expected labels), run in
  CI whenever a prompt or model changes. Track accuracy, human-override rate and cost.
- **Prompt injection:** treat inbound content as data (✅ in the system prompt), never let model
  output choose recipients or amounts without validation, and cap amounts in code (✅ refund cap).

## 5. Integration management
- **Connector interface per capability** (✅ `CRM`, `EmailSender`, payments) with adapters per
  vendor (HubSpot, Pipedrive, Salesforce, Gmail/Graph, Postmark, Stripe, QuickBooks…).
- **Credential vault:** OAuth tokens and API keys are encrypted per tenant (envelope
  encryption, KMS-held key), never logged (✅ audit redaction), and refreshed automatically.
- **Rate limits and quotas** per connector per tenant, plus circuit breakers.
- **Escape hatch:** an n8n instance we operate for long-tail integrations, called over HTTP.
  Customers never edit it, so we stay within its licence.

## 6. Audit logs
- **Append-only** table (✅) with actor (system / AI model / human user), event, run, and
  redacted detail.
- **Later:** hash-chaining rows for tamper evidence, retention policy per tenant, export
  (CSV/JSON) and streaming to a customer's SIEM, and an "explain this run" view in the dashboard.

## 7. Billing and usage tracking
- **Meter events** written by the engine: `run.completed`, `ai.tokens`, `action.executed`, per
  tenant. Aggregate daily.
- **Pricing shape to test with real customers:** platform fee + included runs + overage; AI
  passed through at cost + margin. This is a business decision to make together with the
  ChatGPT recommendations.
- **Billing provider:** Stripe Billing with usage-based prices (no build). Invoices for services
  clients stay manual in Phase 1.

## 8. Customer dashboards
- **Phase 1:** none. We send weekly reports (generated from the audit table) and use the API.
- **Phase 2:** a Next.js app with an approvals inbox (the highest-value screen: clients approve
  AI-drafted actions from their phone), run history with the audit trail, integration
  connections, usage and spend, and team management.
- **Monitoring and analytics:** per-workflow success rate, time saved, AI override rate, cost
  per run. These are the numbers that justify renewals.

## What we are deliberately not building yet
A visual workflow builder, a marketplace, custom agent frameworks, Kubernetes, microservices.
