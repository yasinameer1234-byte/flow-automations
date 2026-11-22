# 10 · Go-live setup and client onboarding

Status on 2026-11-22. **Nothing here costs money unless it says so.** The rule is: free tiers
for the demo; anything paid waits for a paying client and an explicit yes.

## What is set up
| Item | State |
|---|---|
| GitHub repo `yasinameer1234-byte/flow-automations` (private) | ✅ code pushed, CI runs on every push |
| Hosted-mode guardrails | ✅ with `FLOW_ENV=staging` or `production` the app refuses to start without real secrets; secure cookies; production never seeds demo data |
| Free hosting config | ✅ `render.yaml` (Render free tier, no card) |
| Paid hosting config, for later | ✅ `fly.toml` (Fly.io Toronto) and a manual deploy workflow. **Not used until approved.** |
| Retell phone path | ✅ signed tool endpoints `/voice/{slug}/tools/{tool}` and webhook `/voice/webhooks/retell/{slug}`; phone calls land on the dashboard with transcript, outcome, lead, appointment and minutes |
| Retell agent | ✅ prompt `retell/receptionist_prompt.md` and `scripts/retell_setup.py` (plumbing, HVAC, electrical). Creates the agent; **never buys a number** |
| Anthropic API key | ⏸ not needed yet. The live call LLM (Claude Haiku) runs inside Retell and is billed by Retell |

## The owner's steps (free, about 10 minutes)
1. **Render** — sign in at dashboard.render.com with GitHub. *New → Blueprint →*
   `flow-automations` → *Apply*. Render generates `FLOW_SECRET_KEY`, `FLOW_VOICE_TOOL_KEY` and
   `FLOW_DEMO_PASSWORD` itself. The demo password is visible under the service's
   *Environment* tab. Share only the app's URL (`https://<name>.onrender.com`).
2. **Retell** — sign up at retellai.com (USD $10 free credit). *Settings → API Keys*: use the key
   marked with the webhook badge. Put it in exactly two places, never in chat:
   - Render → the service → *Environment* → `RETELL_API_KEY`
   - Claude project settings → Cloud environment → environment variable `RETELL_API_KEY`
     (lets Claude run `scripts/retell_setup.py`).

Then Claude creates the agent and tests it with Retell web calls, which use the free credit
(roughly 90+ minutes of testing at USD $0.07–0.11/min).

## Where every secret lives
| Secret | Created by | Stored in | Never in |
|---|---|---|---|
| `FLOW_SECRET_KEY`, `FLOW_VOICE_TOOL_KEY`, `FLOW_DEMO_PASSWORD` | Render (generated) | Render environment | git, chat, docs |
| `RETELL_API_KEY` | Owner, in Retell | Render environment + Claude cloud environment variable | git, chat, docs |
| `ANTHROPIC_API_KEY` (later) | Owner, in console.anthropic.com with a spend limit | Hosting environment | git, chat, docs |
| `FLY_API_TOKEN` (later, paid hosting) | Owner, `fly tokens create deploy` | GitHub → repo → Settings → Secrets → Actions | git, chat, docs |

`flow check-config --env staging` shows which are set, never their values.

## Free-tier limits to know
- Render free sleeps after 15 minutes idle and takes about a minute to wake. **A real caller
  would hear silence**, so free Render is for demos only. Open the app URL a minute before a demo.
- Its disk resets on restart. That only reseeds the synthetic businesses; nothing real is lost
  because nothing real is stored.
- Region is Ohio. Real client data must move to Canadian hosting first (Fly.io Toronto, about
  USD $5/month, plus Postgres and backups) — a paid step for the first paying client.

## Onboarding a paying client's phone number
The client keeps their existing number. Options, best first:

| Option | How it works | Per-client cost | Setup time | When |
|---|---|---|---|---|
| **Conditional call forwarding** (default) | Client's carrier forwards *unanswered, busy or after-hours* calls to a Retell number we hold for them. Staff still answer first | Retell number about USD $2/month + minutes (already in package costs) | Same day; the client or their carrier turns on forwarding | Most owner-run trades on a cell or landline |
| **Forward all calls** | Same, but every call goes to the AI first, which transfers to staff when needed | Same | Same day | Clients who want the AI to triage everything |
| **SIP connection from their phone system** | Their VoIP system (e.g. RingCentral, 3CX) routes calls to Retell over SIP; no extra number | Minutes only (plus any fee from their provider) | 1–3 days with their provider | Clients already on a business phone system |
| **Port the number** | Number moves to Twilio and is imported into Retell | About USD $1–2/month + minutes | 1–4 weeks, risk of downtime | Only if the client asks; avoid at first |

Onboarding checklist per client (after payment):
1. Written authorisation to answer their calls and handle their customers' details.
2. Business profile: hours, services and durations, service area, approved answers (fees,
   warranties, payment), emergency rules, on-call person.
3. Create the tenant, load the profile, run `scripts/retell_setup.py --slug <client> --trade <trade>`.
4. Buy the client's Retell forwarding number (approved spend, about USD $2/month).
5. Test with web calls, then a real call from a cell phone, then turn on forwarding.
6. Confirm recording/AI disclosure wording and the booking-confirmation text.
7. Owner logs in and sees the test calls on the dashboard.

## Known gaps before the first paying client
- SMS confirmations are simulated. Retell cannot send SMS from Canadian numbers, so real texts
  need a separate provider (a paid step).
- SQLite → Postgres, backups and monitoring before any real client data.
- Calendar sync (Google Calendar / Jobber) is not built; bookings live in our database.
