# 08 · Blockers and next actions

## Blockers (need yaso)
| # | Blocker | Why it matters | What's needed |
|---|---|---|---|
| 1 | **No GitHub repository** | Code exists only in a local git repo. CI can't run and nothing is backed up | Create an empty private repo (suggested name `flow-automations`) under your account or org, connect GitHub to Claude, and say "push" |
| 2 | **No AI API key approved** | The demo runs on keyword rules, not AI | Approve spend (about $0.0003–$0.013 per message; see 03) and set `ANTHROPIC_API_KEY` in a secret store. Never paste it in chat |
| 3 | **Target market not chosen** | Categories, prompts, connectors and the first real integration depend on it | Business decision (with the ChatGPT plan) |
| 4 | **Hosting not approved** | No staging environment | Approve roughly $10–75/month for Phase 1 infra when we have a pilot client |

## Next actions (engineering, no approval needed)
1. Postgres support + migrations, keeping SQLite for tests.
2. Async worker queue so intake returns immediately.
3. API-key authentication on the HTTP API.
4. An eval harness (labelled synthetic set + accuracy report) ready for when AI is switched on.
5. An approval notification channel (Slack/email link), simulated first.
6. A second demo workflow (document/invoice extraction) built from the same components.

## Known limitations of v0.1
- CRM writes and the approval request for one run aren't in a single DB transaction.
- The engine is synchronous and in-process: a crash mid-run leaves a `running` row (it's
  visible, but there's no automatic resume yet).
- The simulated classifier's confidence numbers don't mean anything.
- The API has no auth. That's why compose binds it to 127.0.0.1.
