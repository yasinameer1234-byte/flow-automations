# 12 · AI Secretary production readiness (2026-11-23)

What works, what was tested and how, what is still simulated, and exactly what is needed
to finish. "Tested" below means automated tests or a real browser run in the cloud sandbox,
always on synthetic data. Nothing has been tested against a live phone line or a real
customer yet.

## Status by part

| Part | Status | How it was verified |
|---|---|---|
| Browser call demo (rule-based receptionist) | **Works, simulated voice** | 171 pytest tests and the Playwright journey (call, book, dashboard, mark won) |
| Demo access control | **Works** | Tests: strangers get 403 in invite mode; forged, expired and wrong-business links are refused; a demo link is not a login; admin link lets a prospect book |
| Login lockout, demo rate limits | **Works** | Tests, including a spoofed `X-Forwarded-For` that no longer gets around the limit |
| Demo data clean-up | **Works** | Test: browser-demo bookings, leads and calls older than 24 h are deleted on the next demo call; seeded history untouched |
| Caller names and repeated questions | **Works in the browser demo** | Tests replay the audit transcripts ("making a noise", the price loop) plus new cases (name volunteered first, phone before name, name plus question, Emily/Kelly/Ming, "sorry?") |
| Name guard for the voice agent | **Works at the tool level** | Test: `book_appointment` returns `name_unclear` for "Making", "First", digits |
| Retell voice agent (phone or browser) | **Built, never run with a real key** | The demo page's voice button loads the bundled Retell SDK and reaches `api.retellai.com/v3/create-web-call`; with a placeholder key Retell answers "Invalid Public Key", which the page shows. A real conversation needs the keys below |
| Booking through Retell tools | **Built, tested with signed fake requests** | Existing tests sign requests the way Retell does. Not yet called by Retell itself |
| SMS confirmations and follow-ups | **Built for Twilio, simulated until keys exist** | Tests with a fake Twilio: send, allow-list, failure handling, signed delivery receipts. No real text has been sent. `api.twilio.com` is blocked from this sandbox |
| Emergency transfer | **Simulated** | Becomes an urgent callback task for the on-call person. A live transfer needs a Retell phone number (paid) |
| Client onboarding | **Works from a profile file** | `flow create-tenant config/clients/<client>.toml` (tests). No web form yet |
| Hosting | **Not deployed** | `render.yaml` is ready; `onrender.com` is blocked from this sandbox |
| Calendar sync (Google/Microsoft) | **Not built** | Bookings live in Flow's own calendar |

## Demo access (sales calls)
Hosted instances start with `FLOW_DEMO_ACCESS=invite`. In **Admin → Demo links for
prospects**, pick the demo business, type the prospect's name and how long the link should
work (default 72 hours), and copy the link. The prospect opens it, and the browser keeps
access until the link expires. Signed-in admins and the demo business's owner don't need a
link. Real clients never have a demo page. Locally (`FLOW_ENV` unset) the demo stays public
so development and the e2e test work as before.

Admin also shows "your address as this app sees it". After the first Render deploy, check
that it matches your real public IP. If it shows a Render or Cloudflare address, change
`FLOW_PROXY_HOPS` (see below).

## What's needed from Render (free plan, no card)
1. Sign in at dashboard.render.com with GitHub, then **New → Blueprint → flow-automations →
   Apply**. Render generates `FLOW_SECRET_KEY`, `FLOW_VOICE_TOOL_KEY` and
   `FLOW_DEMO_PASSWORD`.
2. In the service's **Environment** tab, set:
   - `FLOW_PUBLIC_URL` = the service URL, e.g. `https://flow-automations-demo.onrender.com`
   - `RETELL_API_KEY` (from Retell, below)
3. Tell Claude the service URL (a URL, not a secret). Claude runs `scripts/retell_setup.py`
   against it and does the first test calls.
4. After the first deploy, sign in as admin and check the "your address" line (above).

Known free-plan limits: the service sleeps after 15 minutes idle (about a minute to wake),
and the disk resets on every restart, so data written by test calls disappears. That's fine
for demos. A paying client needs a paid instance with a persistent disk or Postgres (about
USD $7–25/month), which needs your approval first.

## What's needed from Retell (free credit, no number)
1. Sign up at retellai.com (USD $10 free credit).
2. **API key** → Render `RETELL_API_KEY` and the Claude cloud environment variable
   `RETELL_API_KEY`.
3. **Public key** (Settings → Public Keys), limited to the Render domain →
   `FLOW_RETELL_PUBLIC_KEY` in Render.
4. After Claude creates the agent, put its id in `FLOW_RETELL_AGENT_NORTHSIDE` in Render.
   The demo page then shows "Talk to the real voice agent". It's a free web call. Each call is
   capped at 10 minutes.

## Real SMS (Twilio): off until the first paying client
Decision 2026-11-23: no Twilio trial for now; SMS stays simulated and callers are never
told a text is coming. When a client pays, the steps are:
A Twilio trial account needs no card and comes with trial credit. It can text only phone
numbers you verify in Twilio, and every message starts with "Sent from your Twilio trial
account". The trial also includes one Twilio number, paid from the trial credit. That needs
an explicit yes because of the no-phone-numbers rule.
Set in Render: `FLOW_SMS_PROVIDER=twilio`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
`TWILIO_FROM_NUMBER`, and `FLOW_SMS_ALLOWLIST` = your own cell number. In Twilio, set the
number's incoming-message webhook to `<FLOW_PUBLIC_URL>/sms/twilio/inbound/northside`.
Then `flow sms-test <your number>` sends one test text. The delivery receipt changes the
outbox status from `sent` to `delivered`. Outside production, texts go only to numbers on
the allow-list, and demo businesses never text anyone else.

## Real-call test plan (once the keys above exist)
1. Web call from the demo page: book a visit; check the call, lead and appointment on the
   dashboard; say "it's making a noise" and check the name isn't "Making"; ask the price
   three times and check the answers get shorter and end with a callback offer.
2. Emergency phrase ("water is pouring from the ceiling"): urgent callback task, no fake
   transfer.
3. With SMS on: book with your own number; check that a real text arrives and the outbox
   shows `delivered`.
4. Phone call: only after a client pays and approves a Retell number (about USD $2/month).

## Settings added in this round
| Variable | Default | Purpose |
|---|---|---|
| `FLOW_DEMO_ACCESS` | `public` locally, `invite` hosted | Who can open the demo business |
| `FLOW_DEMO_RETENTION_HOURS` | `24` | Browser-demo bookings older than this are deleted |
| `FLOW_PROXY_HOPS` | `0` (Render: `1`) | Which `X-Forwarded-For` entry is the real caller |
| `FLOW_PUBLIC_URL` | unset | Public URL, used to check Twilio signatures |
| `FLOW_RETELL_PUBLIC_KEY`, `FLOW_RETELL_AGENT_<SLUG>` | unset | Real voice agent button on the demo page |
| `FLOW_SMS_PROVIDER`, `TWILIO_*`, `FLOW_SMS_ALLOWLIST` | simulated | Real SMS |

## Rebuilding the bundled Retell browser SDK
`src/flow/web/static/vendor/retell-sdk-3.0.3.js` is `retell-client-js-sdk@3.0.3` bundled with
its dependencies (livekit-client, eventemitter3) by esbuild, so the page loads no third-party
CDN:
```bash
npm i retell-client-js-sdk@3.0.3 esbuild@0.24.2
echo 'export { RetellClient } from "retell-client-js-sdk";' > entry.js
npx esbuild entry.js --bundle --format=esm --minify --target=es2020 --outfile=retell-sdk.js
```
