# 11 · Revenue Engine (Growth and Scale packages)

Status 2026-11-22: **built and tested locally; all texts are simulated.** Nothing reaches a real
phone until an SMS provider is approved (a paid step) and a client has authorised it.

## What it does
| Playbook | Trigger | Messages | Stops when |
|---|---|---|---|
| Missed-call text back | Unanswered call in the last 48 h from a textable number, no booking | +2 min, +1 day | they book, reply, text STOP |
| Unbooked lead follow-up | Lead `new`/`callback`, older than 1 h, younger than 14 days | +1 h, +1 day, +3 days | they book, lead won/lost, reply, STOP |
| Review request | Job marked won in the last 14 days | +1 day, once | sent, STOP |
| Past-customer reminder | Last job 11–23 months ago, no newer job | once a year | sent, reply, STOP |

## Rules enforced in code, before every message
1. Package includes it (Growth/Scale) and the admin kill switch `revenue_engine` is on. When
   switched off, messages wait; they are never silently dropped.
2. **The owner approved the exact wording.** Templates are fingerprinted; any wording change
   needs a new approval and in-flight sequences stop.
3. Not opted out. STOP, UNSUBSCRIBE, CANCEL, END, QUIT, ARRÊT/ARRET suppress the number for every
   playbook and send a one-time confirmation.
4. CASL implied consent: 6 months after an inquiry (call or lead), 2 years after a job.
5. Local sending hours 09:00–20:00, no Sundays (per business, configurable).
6. At most one follow-up per number per 20 hours, and one active sequence per number.
7. Every message names the business and its phone number and says "Reply STOP to opt out".
   Templates contain no prices, times or offers, so the engine cannot invent them.
8. Any reply other than STOP stops the automation and creates a staff task.

## How results are counted
- **Recovered booking:** an appointment for that number created *after* our first follow-up.
- **Verified recovered revenue:** recovered bookings whose lead was later marked won with a value.
- **Estimated recovered revenue:** recovered bookings × average job × close rate, labelled.

## Where it runs
- `flow serve` runs the engine every 60 seconds (`FLOW_REVENUE_LOOP=0` turns it off).
- `flow revenue-tick` runs it once (for cron).
- Owner page: `/app/revenue`. Simulated inbound text for demos: `POST /api/demo/{slug}/sms`.

## Not built yet
Real SMS sending (provider and Canadian number registration), email channel, quote tracking from
a job system, and per-business template editing.
