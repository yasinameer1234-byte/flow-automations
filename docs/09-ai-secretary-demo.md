# 09 · AI Secretary: first customer journey (v0.2)

**Journey:** a customer calls *Northside Plumbing & Heating* (synthetic), asks for help, gets
offered real open times, books one, receives a (simulated) text, and appears as a lead and
appointment in the owner's dashboard. The owner later marks the job won with its real value,
and only then does it count as verified revenue.

## What works today (local, synthetic)
| Feature | Status | Proof |
|---|---|---|
| Browser "phone call", typed or spoken (Chrome Web Speech) | Works | e2e/first_journey.js |
| AI disclosure spoken first, after-hours greeting | Works | test_after_hours_greeting |
| Collect name, phone and reason; read back before booking | Works | test_happy_path… |
| Availability from business hours minus bookings, 60-min lead time | Works | test_offered_times_always_come_from_availability |
| Book, reschedule, cancel; double-booking impossible (DB unique index) | Works | test_double_booking_is_impossible, test_cancel_and_reschedule |
| Answers only from **approved** knowledge entries; otherwise offers a callback | Works | test_only_approved_answers |
| Emergency keywords → safety line + transfer to on-call; nobody available → urgent callback | Works (transfer simulated) | test_emergency_* |
| "Speak to a person" → transfer, or callback when staff unavailable | Works (transfer simulated) | test_human_request_without_staff_takes_message |
| Calendar/API failure mid-call → no time promised, callback created, error logged | Works | test_calendar_outage_never_promises_a_time |
| Kill switch per business (assistant, confirmations) | Works | test_kill_switch…, test_admin_kill_switch |
| Call log with transcript, tool calls, outcome, billed minutes, usage ledger | Works | dashboard / call detail |
| Dashboard: calls, answered/missed, leads, bookings, verified vs estimated revenue/ROI, time saved, minutes vs allowance | Works | test_metrics_separate_verified_from_estimated |
| Login, roles (platform_admin / owner / staff / viewer), CSRF, tenant isolation | Works (MVP-grade) | test_web.py |
| Voice-agent tool endpoints shaped for Retell custom functions, tenant API key | Works locally; not connected to Retell | test_voice_tools_need_tenant_key |

## Simulated or not built
- **Speech and phone:** the demo uses the browser's speech features. Real calls need Retell +
  a phone number (account, cost).
- **Language understanding in the demo is rule-based**, not an LLM. In production the LLM runs
  inside Retell and calls our same tool endpoints. The rules handle common booking phrasings;
  unusual phrasing falls back to a callback.
- **SMS** is written to a `messages` table. **Transfers** are recorded, not dialled.
- **Calendar** is our own appointments table; Google/Microsoft sync is next.
- Call recording storage, Revenue Recovery follow-ups, Payment Recovery, document automation,
  billing: planned (see the plan doc).

## Why the agent cannot invent things
1. Times reach the caller only from `check_availability` output.
2. A booking is confirmed only after `book_appointment` succeeds; the tool re-checks the slot
   against business hours, lead time and existing bookings, and the DB enforces uniqueness.
3. Answers come only from knowledge entries with `approved = 1`; drafts are never read out.
4. If any tool fails, the agent says so, promises nothing, and creates a callback task.
