# 05 · What is real vs simulated (v0.1)

| Component | Status | Notes |
|---|---|---|
| Workflow engine (runs, dedupe, routing, status) | **Real** | Python code, tested |
| Human approval gate | **Real** | Pending → approved/rejected → executed/failed; a decision can only be made once |
| Retry with exponential backoff | **Real** | Tested with injected transient failures |
| Dead-letter table | **Real** | Exhausted or permanent failures are stored for replay |
| Audit log + JSON logs | **Real** | Append-only table; secrets redacted |
| Database | **Real (SQLite)** | Local file. Production needs Postgres |
| "CRM" | **Real database, synthetic data** | `SqliteCRM` stands in for HubSpot/Pipedrive etc. Same interface a real connector will implement |
| HTTP API | **Real** | FastAPI; tested in-process. **No auth: local use only** |
| AI classification: `simulated` provider (default) | **Simulated** | Keyword rules, deterministic. **It is not AI**, and the accuracy numbers are meaningless |
| AI classification: `anthropic` provider | **Real code, not yet run live** | Calls Claude with schema-enforced output. Tested against a fake client (request shape, refusals, error mapping). Needs an approved API key to run for real |
| Outbound email | **Simulated** | Written to the `sim_email_outbox` table. Nothing is sent |
| Refunds / payments | **Simulated** | Written to the `sim_refunds` table, with a hard $5,000 cap. No money moves |
| Input messages | **Synthetic** | `data/synthetic/inbound.jsonl`, with fictional people and `.example` domains |
| Docker image | **Real** | Built locally (see 07) |
| CI pipeline | **Written, not run** | Needs a GitHub repository |
| n8n | **Config only** | Optional compose profile; not started or tested |
| Deployment | **None** | Nothing is hosted anywhere |

"Tests pass" means the logic is correct against simulated services. It does **not** mean
production-ready. See the checklist in 04.
