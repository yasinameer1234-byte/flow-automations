# 07 · Test results

Run on 2026-11-22 in the development container (Python 3.13.18, Linux). Everything ran against
**simulated services and synthetic data**.

## Automated tests: 38 passed, 0 failed
`pytest -v` (1.4 s). Lint (`ruff check`) and format check: clean.

| File | Tests | What it proves |
|---|---|---|
| test_retry.py | 4 | Backoff schedule; permanent errors are not retried; exhaustion; delay cap |
| test_classifier.py | 10 | Simulated classifier categories and extraction; Claude adapter request shape (model, schema, effort, prompt-injection guard), refusal → human, 429 → retryable, 400 → permanent |
| test_workflow.py | 14 | Lead → CRM + held email; approve sends exactly once; reject sends nothing; support ticket priority; refund + reply need two approvals; spam makes no CRM writes; low confidence → review; duplicate webhook is idempotent; AI 503s retried; AI outage → human review; email retry then success; email outage → dead letter + failed run; refund cap enforced after approval; workflow crash is recorded |
| test_approvals_and_audit.py | 4 | No double approval; decider required; tenant isolation; secret redaction |
| test_api.py | 2 | HTTP end-to-end: intake → approvals → approve → 409 on repeat → run audit → CRM; validation 422/404 |
| test_config.py | 4 | Tenant policy file; safe defaults (simulated AI, approvals on); auto-acknowledge policy |

## Demo run (`flow demo --auto-approve`)
```
Processing 8 synthetic messages with classifier 'simulated-rules-v1'

  [awaiting_approval] sales_lead       conf=0.95  New lead captured (deal #1)
      -> approval needed: apr_8767e1de2908
  [completed        ] support_request  conf=0.95  Support ticket #1 opened (high)
  [awaiting_approval] billing          conf=0.95  Billing ticket #2 opened
      -> approval needed: apr_dd13c455ca49
      -> approval needed: apr_5bf0c9ffc03d
  [ignored          ] spam             conf=0.95  Spam; no action taken
  [awaiting_approval] sales_lead       conf=0.95  New lead captured (deal #2)
      -> approval needed: apr_75ef06c38145
  [needs_review     ] other            conf=0.30  Low confidence (0.30); queued for a person
  [needs_review     ] sales_lead       conf=0.95  Lead has no email; needs a person
  [duplicate        ] -                conf=-  Already processed (status: completed)

CRM now holds: {'contacts': 4, 'deals': 2, 'tickets': 2, 'notes': 2}
Pending approvals: 4  (run `flow approvals` to review)
  approved apr_8767e1de2908 (send_email): executed
  approved apr_dd13c455ca49 (issue_refund): executed
  approved apr_5bf0c9ffc03d (send_email): executed
  approved apr_75ef06c38145 (send_email): executed
Simulated outbox now holds 3 email(s).
```

## Container smoke test
Built `flow-automations:dev` (python:3.13-slim, non-root user), started it, then over HTTP:
`/health` ok → `POST /intake` (lead) → `awaiting_approval` → approve → `executed`
(`sim-msg-1`) → CRM summary `{"contacts":1,"deals":1,"tickets":0,"notes":1}`. JSON logs emitted.

## Not tested
- Live Claude API calls (no approved key). The adapter is tested only against a fake client.
- The CI workflow on GitHub (no repo yet).
- Postgres, real email/CRM/payment providers, load, security/penetration, and n8n.
