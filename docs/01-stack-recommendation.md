# 01 · Technical stack recommendation

Status: proposal v0.1 (2026-11-22). Prices and licence terms were checked on 2026-11-22
against the sources linked at the bottom. Re-check before any purchase.

## The short answer

**Own the core in code, rent the commodity parts, and use visual tools only where clients
never touch them.**

| Layer | Phase 1 (services, 0–15 clients) | Phase 2 (SaaS) |
|---|---|---|
| Core language | **Python 3.13** (FastAPI, Pydantic) | Python core + **TypeScript/Next.js** customer dashboard |
| Workflow engine | **Our own small engine** (this repo): runs, idempotency, approvals, retries, audit | Same API, backed by a durable executor (**Temporal**, MIT, or a Postgres job queue) |
| Quick glue automations | **Self-hosted n8n Community**, operated by us only | n8n only as an internal tool; customer-facing builder via **Activepieces (MIT)** or an n8n Enterprise licence |
| AI | **Claude API** behind a `Classifier`/provider interface | Same, plus a per-tenant model/cost policy and evals |
| Database | SQLite (dev) → **Postgres** (prod) | Postgres with tenant_id + row-level security |
| Hosting | 1 small VPS or container host, Docker | Managed containers + managed Postgres |
| Secrets | Env vars locally; a secrets manager in prod | Secrets manager + per-tenant encrypted credential vault |
| Observability | JSON logs + audit table + free Sentry tier + uptime ping | OpenTelemetry, metrics, alerting |

Zapier and Make: **use them only when a client already runs on them**. Don't build our product on them.

## Comparison

Scores: 5 = best for us. "Custom" means Python/TypeScript code in this repo.

| Criterion | Custom code | n8n (self-hosted) | n8n Cloud | Make | Zapier | Temporal (+ custom) |
|---|---|---|---|---|---|---|
| Development speed (first workflow) | 3 | **5** | **5** | **5** | **5** | 2 |
| Development speed (10th similar workflow) | **5** (reuse components) | 4 | 4 | 3 | 3 | 4 |
| Hosting cost at small scale | **5** (≈ €5–20/mo VPS) | **5** (same VPS) | 4 (€20–50/mo) | 4 | 3 | 4 self-host / 2 cloud |
| Cost at scale (per execution) | **5** | **5** | 3 | 2 | 1 | 4 |
| API integrations out of the box | 2 (we write them) | **5** (hundreds of nodes) | **5** | **5** | **5** (largest catalog) | 2 |
| Reliability (retries, idempotency, testability) | 4 (we control it, we test it) | 3 | 3 | 3 | 3 | **5** (durable execution) |
| Security / data control | **5** | 4 | 3 | 2 | 2 | **5** |
| Multi-client support | **5** (tenant_id everywhere) | 2 (one instance per client, or shared with care) | 2 | 2 | 2 | 4 |
| SaaS scalability | **5** | 2 (licence) | 1 | 1 | 1 | **5** |
| Commercial licence freedom | **5** (ours) | 2 (see below) | 3 | 3 | 3 | **5** (MIT) |
| Automated testing | **5** (pytest, CI) | 2 | 2 | 1 | 1 | **5** |

### Licensing, the part that decides it

**n8n** uses the Sustainable Use License (fair-code, not OSI open source). From n8n's own licence FAQ:
- ✅ We may build, run and maintain workflows on our own n8n instance and charge clients for
  setup, maintenance and consulting, **as long as clients can't create or edit workflows**.
- ✅ We may use n8n as a hidden backend that only our servers call.
- ❌ We may **not** host n8n for clients so they can build workflows, white-label it, or let our
  SaaS users build/configure workflows through it (including through our own UI, API or an AI
  agent). That needs an n8n Enterprise licence (contact sales; price not public).
- ❌ We may not fork it into our own automation product.

So: n8n is good for **Phase 1 services** and as an internal tool. It is a trap as the **core of a
SaaS**, unless we budget for an Enterprise licence.

**Zapier / Make** are SaaS. There's no licence risk, but per-task pricing climbs fast, data goes
through a third party, and nothing is portable. That's fine for one client's simple Zap. It's bad
as our platform.

**Temporal** is MIT, the strongest durable-execution engine, and has heavy ops overhead. We'll
adopt it when we have long-running, multi-day workflows. Not before.

**Activepieces** Community Edition is MIT, and its enterprise features (SSO, audit logs,
branding) are paid. It's the likeliest candidate if we later want a customer-facing visual
builder without n8n's restriction. *Verify the licence file at the time of adoption.*

Other engines worth knowing (verify licences before use): Windmill (AGPLv3 core plus a
commercial EE, so the copyleft needs care), Kestra (Apache-2.0), Prefect (Apache-2.0),
Inngest, Trigger.dev, Hatchet.

## Why Python for the core

- The AI, document-processing and data ecosystem (PDF/OCR, email parsing, pandas, the
  Anthropic/OpenAI SDKs, evaluation tools) is strongest in Python.
- Pydantic gives us one schema that validates API input, AI structured output and DB records.
- FastAPI produces an OpenAPI spec for free. That spec becomes the integration contract for n8n
  (HTTP node), Zapier/Make webhooks and the future dashboard.
- TypeScript enters in Phase 2 for the customer dashboard (Next.js). We don't run two backend
  languages before we have customers.

## AI provider choice

- The `Classifier` interface hides the provider. The demo ships a deterministic simulated
  classifier and a Claude implementation that uses schema-enforced structured output.
- The model is a config value (`FLOW_AI_MODEL`). The default is Claude Opus 5.5 at low effort.
  For high-volume, simple classification, Claude Haiku 5.5 costs roughly **40× less** per
  message (see 03). Which model a client gets is a business decision per client and should be
  settled with an eval on that client's real (authorised) data.
- Every AI decision is audited, low-confidence results go to a human, and a provider outage
  routes work to humans instead of guessing.

## How we use each tool, in practice

1. **Client asks for something simple** (form → Slack + sheet): build it in our self-hosted n8n
   with a webhook to our API for anything that needs AI, approval or audit.
2. **Client asks for something core to their business** (lead handling, invoices, support
   triage): build it as a workflow module in this repo, with tests, on shared components.
3. **Client already lives in Zapier/Make**: they call our `/intake` webhook from their scenario.
   We don't rebuild their stack.
4. **Repeated patterns across clients** become reusable components here. That library is the
   seed of the SaaS.

## Sources (checked 2026-11-22)
- n8n licence FAQ: https://docs.n8n.io/n8n-community-license/community-license/license-faq
- n8n Sustainable Use License: https://docs.n8n.io/privacy-and-security/sustainable-use-license
- n8n pricing: https://n8n.io/pricing/?selfHosted=true
- Zapier vs Make 2026 cost breakdown: https://dev.to/trackstack/zapier-vs-make-in-2026-the-honest-cost-feature-breakdown-gp3
- Open-source workflow engines 2026: https://automationatlas.io/rankings/best-open-source-workflow-engines-2026/
- Claude model pricing: Anthropic API docs (model table as of 2026-11)
