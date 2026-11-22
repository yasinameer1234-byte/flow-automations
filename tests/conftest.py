import pytest

from flow.app import build_app
from flow.config import Settings, TenantPolicy
from flow.core.db import Database
from flow.core.models import InboundMessage
from flow.core.retry import RetryPolicy


def no_sleep(_):
    return None


@pytest.fixture
def retry():
    return RetryPolicy(attempts=3, base_delay=0.01, sleep=no_sleep)


@pytest.fixture
def settings():
    return Settings(
        db_path=":memory:",
        log_json=False,
        log_level="WARNING",
        tenant=TenantPolicy(company_name="Test Co"),
    )


@pytest.fixture
def flow(settings, retry):
    return build_app(settings, db=Database(":memory:"), retry=retry)


def msg(**kw) -> InboundMessage:
    base = dict(source="email", body="hello")
    base.update(kw)
    return InboundMessage(**base)


LEAD = dict(
    external_id="l1",
    sender_name="Priya Raman",
    sender_email="Priya@Example.com",
    subject="Quote for automation",
    fields={"company": "Northwind"},
    body="We want a quote and a demo to automate intake. Budget $12,000.",
)
SUPPORT = dict(
    external_id="s1",
    sender_name="Tom",
    sender_email="tom@example.com",
    subject="Portal down",
    body="Login is broken, error every time. Urgent.",
)
BILLING = dict(
    external_id="b1",
    sender_name="Ana",
    sender_email="ana@example.com",
    subject="Charged twice",
    body="I was charged twice on my invoice. Please refund $249.",
)
SPAM = dict(
    external_id="x1",
    sender_email="spam@example.com",
    subject="Winner",
    body="SEO services with guaranteed traffic, click here",
)
