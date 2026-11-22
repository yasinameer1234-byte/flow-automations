"""Revenue Engine playbooks: who gets a follow-up, when, and what it says.

Every message template is owner-approved before anything is sent; changing the wording
changes its fingerprint and needs a fresh approval. Templates only use facts we hold
(business name and phone, the customer's first name, the review link the owner gave us),
so a message can never promise a price, a time or a discount.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

OPT_OUT = "Reply STOP to opt out."


@dataclass(frozen=True)
class Step:
    delay_minutes: int
    template: str


@dataclass(frozen=True)
class Playbook:
    key: str
    title: str
    why: str
    trigger: str
    consent_basis: str  # inquiry (6 months) | customer (2 years), per CASL implied consent
    steps: tuple[Step, ...]

    @property
    def fingerprint(self) -> str:
        text = "\n".join(f"{s.delay_minutes}|{s.template}" for s in self.steps)
        return hashlib.sha256(text.encode()).hexdigest()[:16]


PLAYBOOKS: dict[str, Playbook] = {
    p.key: p
    for p in (
        Playbook(
            key="missed_call",
            title="Missed-call text back",
            why="Most callers who reach voicemail call the next company instead.",
            trigger="A call that nobody answered, from a number we can text, with no booking.",
            consent_basis="inquiry",
            steps=(
                Step(
                    2,
                    "Hi, this is {business}. Sorry we missed your call. Reply here with what "
                    "you need and a good time, or call us back at {business_phone}. " + OPT_OUT,
                ),
                Step(
                    24 * 60,
                    "Hi{first_name_space}, {business} following up on your call yesterday. "
                    "Still need a hand? Reply or call {business_phone}. " + OPT_OUT,
                ),
            ),
        ),
        Playbook(
            key="lead_follow_up",
            title="Unbooked lead follow-up",
            why="Leads that asked for a callback or quote go cold within a day or two.",
            trigger="A lead marked new or callback that has not booked after an hour.",
            consent_basis="inquiry",
            steps=(
                Step(
                    60,
                    "Hi{first_name_space}, it's {business}. Thanks for reaching out about "
                    "{reason_short}. Want us to book a visit? Reply YES or call "
                    "{business_phone}. " + OPT_OUT,
                ),
                Step(
                    24 * 60,
                    "Hi{first_name_space}, {business} checking in. We still have openings this "
                    "week. Reply YES and we'll call you to book. " + OPT_OUT,
                ),
                Step(
                    3 * 24 * 60,
                    "Last note from {business}: if you still need help with {reason_short}, "
                    "reply or call {business_phone}. " + OPT_OUT,
                ),
            ),
        ),
        Playbook(
            key="review_request",
            title="Review request after a job",
            why="Recent reviews drive the next calls; most happy customers just need a nudge.",
            trigger="A job marked won in the last 14 days, once per job.",
            consent_basis="customer",
            steps=(
                Step(
                    24 * 60,
                    "Hi{first_name_space}, thanks for choosing {business}. If we did a good job, "
                    "a quick review helps a lot: {review_url} " + OPT_OUT,
                ),
            ),
        ),
        Playbook(
            key="reactivation",
            title="Past-customer reminder",
            why="Customers from about a year ago are due for maintenance and rarely remember.",
            trigger="A customer whose last job was 11 to 23 months ago, at most once a year.",
            consent_basis="customer",
            steps=(
                Step(
                    0,
                    "Hi{first_name_space}, it's {business}. It's been about a year since your "
                    "last visit. Want us to book a check-up? Reply YES or call "
                    "{business_phone}. " + OPT_OUT,
                ),
            ),
        ),
    )
}


def render(
    template: str,
    *,
    business: str,
    business_phone: str,
    review_url: str | None,
    name: str | None,
    reason: str | None,
) -> str:
    first = (name or "").split()[0] if name else ""
    reason_short = (reason or "your request").strip().rstrip(".")
    if len(reason_short) > 40:
        reason_short = "your request"
    return template.format(
        business=business,
        business_phone=business_phone,
        review_url=review_url or "",
        first_name_space=f" {first}" if first else "",
        reason_short=reason_short[:1].lower() + reason_short[1:],
    )
