# 03 · Deployment plan and cost estimates

All figures are **estimates** from public price lists checked on 2026-11-22, not quotes. Nothing
here has been purchased or provisioned.

## Environments
| Env | Where | Data | Status |
|---|---|---|---|
| local | developer laptop / this container | synthetic only | ✅ working |
| CI | GitHub Actions (free tier for private repos has a minute quota) | synthetic only | ✅ workflow file written, **not yet run** (no GitHub repo yet) |
| staging | 1 small VPS or container host | synthetic + client test accounts | ⏳ needs approval |
| production | same provider, separate instance + managed Postgres | real client data, with written authorisation | ⏳ needs approval and a first client |

## Deployment steps (staging, when approved)
1. Create the GitHub repo and push. CI runs lint, tests, the demo smoke test and a secret scan.
2. Provision one VPS (Docker installed) or a container service. Put Caddy in front for TLS.
3. Store secrets in the provider's secret store or a secrets manager (see 06). No `.env` file on disk.
4. `docker compose up -d api`. Add the n8n profile only if a client workflow needs it.
5. Switch SQLite to Postgres before any real client data (planned v0.2 task).
6. Nightly encrypted DB backups to object storage, with a restore test once a month.
7. Uptime check on `/health`, and error reporting to a Sentry free tier.
8. **Before going public:** add authentication. The current API has none and is bound to
   localhost on purpose.

## Monthly running cost, Phase 1 (services, up to ~10 clients)
| Item | Option | Est. cost / month |
|---|---|---|
| App + n8n host | small VPS (2 vCPU / 4 GB) | $6–25 |
| Postgres | managed free/hobby tier → small paid | $0–25 |
| Backups / object storage | S3-compatible | $1–5 |
| Transactional email | Postmark/SES class, ~10k emails | $0–15 |
| Error tracking + uptime | free tiers | $0 |
| Domain + DNS | | ~$1–2 |
| CI | GitHub Actions free minutes | $0 |
| n8n | self-hosted Community | $0 (licence-compliant only if clients don't edit workflows) |
| **Infrastructure subtotal** | | **≈ $10–75** |
| AI usage | see below | varies by volume |

Alternatives if we prefer not to run a server: n8n Cloud Starter at €20/mo (2.5k executions) or
Pro at €50/mo (10k), billed annually. n8n Business self-hosted (SSO, environments, Git) is
€667/mo, which is not needed now.

## AI cost per message (classification step, estimate)
Assumptions: about 700 input tokens (system prompt + message) and about 500 output tokens
(JSON + adaptive thinking at low effort). These must be **measured** on real traffic.

| Model | $/M in | $/M out | ≈ $ per message | 1,000 msgs | 50,000 msgs |
|---|---|---|---|---|---|
| Claude Opus 5.5 (current default) | 4.00 | 20.00 | ~$0.013 | ~$13 | ~$640 |
| Claude Sonnet 5.5 | 2.00 | 10.00 | ~$0.0064 | ~$6.40 | ~$320 |
| Claude Haiku 5.5 | 0.10 | 0.50 | ~$0.0003 | ~$0.32 | ~$16 |

Prompt caching of the fixed system prompt lowers input cost further at volume. The decision to
make, once we have an eval: **use the cheapest model that meets the client's accuracy bar.**
For plain triage, that's likely Haiku 5.5.

## Phase 2 (SaaS, first ~50 tenants), rough envelope
Managed containers (2–3 instances), managed Postgres with HA, Redis or a queue, an auth provider,
Sentry team plan, and log storage: **≈ $300–1,500/month** before AI usage. That's driven mostly
by HA Postgres and the auth provider's per-user pricing. Re-estimate once Phase 1 usage is real.

## Hidden costs to plan for
- Our own time on maintenance and on-call for client workflows (the biggest real cost).
- Vendor API limits and paid tiers on the **client's** side (CRM API access often requires a
  higher plan).
- Security reviews and questionnaires from business clients; possibly SOC 2 later ($$$).
- Professional indemnity / cyber insurance once we handle client data.
