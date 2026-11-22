# Flow Automations

The engine, integrations and demo workflows for Flow Automations, an AI automation services
company that's growing into a SaaS platform.

**Status: v0.2. AI Secretary browser demo, dashboard and admin panel run locally on synthetic
data with simulated phone, SMS and calendar. Not production-ready.** See [docs/05-real-vs-simulated.md](docs/05-real-vs-simulated.md).

## What's here
```
src/flow/
  core/          engine, approvals, retries, audit log, DB schema, domain models
  ai/            classifier interface: simulated (rules) and Claude (structured output)
  integrations/  CRM, email and payments interfaces + local/simulated implementations
  workflows/     inbound_triage: the v0.1 triage workflow
  secretary/     AI Secretary: tools, dialogue, availability, calls, metrics, seed data
  web/           login, dashboard, admin, browser call demo, voice-agent tool endpoints
  api/           FastAPI HTTP API (local only, no auth yet)
  cli.py         `flow demo`, `flow approvals`, `flow approve|reject`
config/tenants/  per-client policy files (no secrets)
data/synthetic/  fictional test messages
tests/           pytest suite (unit + workflow + HTTP)
e2e/             browser end-to-end test (Playwright)
docs/            stack, architecture, costs, roadmap, secrets, test results, blockers
```

## AI Secretary demo (v0.2)
```bash
FLOW_DEMO_PASSWORD=choose-a-local-password flow serve      # http://127.0.0.1:8000
```
- Call the synthetic plumbing company in the browser: http://127.0.0.1:8000/demo/northside
  (type, or speak in Chrome)
- Owner dashboard: sign in as `owner@northside.example`
- Platform admin: sign in as `admin@flow.local`
- End-to-end browser test: `BASE_URL=http://127.0.0.1:8000 FLOW_DEMO_PASSWORD=... node e2e/first_journey.js`

See [docs/09-ai-secretary-demo.md](docs/09-ai-secretary-demo.md).

## Quick start
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
make test          # lint is `make lint`
make demo          # processes data/synthetic/inbound.jsonl
flow approvals     # see what's waiting for a human
flow approve <id> --by yourname
make api           # http://127.0.0.1:8000/docs
```
Using real AI (costs money, needs an approved key): `export ANTHROPIC_API_KEY=...` in your
shell (never in a committed file), then `FLOW_AI_PROVIDER=anthropic flow demo`.

## The demo: AI triage of inbound messages
1. **Receive** an email, web form or chat message (structured fields and/or free text) via
   `POST /intake` or the CLI.
2. **Understand**: the AI classifier returns a validated `Classification` (category,
   confidence, urgency, contact details, amount, refund flag).
3. **Decide**: low confidence or an AI outage goes to human review. Spam is dropped.
4. **Act**: route by category. Upsert a contact, create a deal or ticket, add a note in the CRM.
5. **Approve**: consequential actions (customer emails, refunds) are held as approvals per
   tenant policy, and run only after a person approves them.
6. **Execute** with retries and idempotency keys. Failures are dead-lettered.
7. **Record** every step in the audit log, and produce a `WorkflowResult`.

Adapting it to a market means changing the categories, prompt, route handlers and reply
templates. The engine and components stay the same.

## Docs
1. [Stack recommendation](docs/01-stack-recommendation.md)
2. [Platform architecture](docs/02-platform-architecture.md)
3. [Deployment and costs](docs/03-deployment-and-costs.md)
4. [Roadmap](docs/04-roadmap.md)
5. [Real vs simulated](docs/05-real-vs-simulated.md)
6. [Secrets](docs/06-secrets.md)
7. [Test results](docs/07-test-results.md)
8. [Blockers and next actions](docs/08-blockers-and-next-actions.md)
9. [AI Secretary demo](docs/09-ai-secretary-demo.md)
