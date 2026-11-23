"""Deployment environment checks.

FLOW_ENV picks the environment: ``local`` (default), ``staging`` or ``production``.
Outside ``local`` the web app refuses to start unless every required secret is set,
so a hosted instance can never fall back to the built-in demo passwords or a random
session key. Values are never printed or logged, only whether each name is set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

ENVIRONMENTS = ("local", "staging", "production")


@dataclass(frozen=True)
class SecretSpec:
    name: str
    purpose: str
    where: str  # who creates it
    required_in: tuple[str, ...]  # environments where startup fails without it
    min_length: int = 0


SECRETS: tuple[SecretSpec, ...] = (
    SecretSpec(
        "FLOW_SECRET_KEY",
        "Signs login sessions and form tokens",
        "Generate locally: openssl rand -hex 32",
        ("staging", "production"),
        min_length=32,
    ),
    SecretSpec(
        "FLOW_DEMO_PASSWORD",
        "Password for the seeded demo admin and owner logins",
        "Choose one; store it in your password manager",
        ("staging",),
        min_length=12,
    ),
    SecretSpec(
        "FLOW_VOICE_TOOL_KEY",
        "Bearer key Retell sends when it calls our booking tools",
        "Generate locally: openssl rand -hex 32; also paste into the Retell tool headers",
        ("staging", "production"),
        min_length=32,
    ),
    SecretSpec(
        "RETELL_API_KEY",
        "Retell API access (agent setup, call records, webhook signatures)",
        "Retell dashboard > Settings > API Keys",
        (),
    ),
    SecretSpec(
        "FLOW_RETELL_PUBLIC_KEY",
        "Retell browser key for the 'talk to the real voice agent' demo button",
        "Retell dashboard > Settings > Public Keys; allow only the app's domain",
        (),
    ),
    SecretSpec(
        "TWILIO_ACCOUNT_SID",
        "Real SMS (with FLOW_SMS_PROVIDER=twilio and TWILIO_FROM_NUMBER)",
        "Twilio console > Account info (a free trial account works for testing)",
        (),
    ),
    SecretSpec(
        "TWILIO_AUTH_TOKEN",
        "Sends texts and checks Twilio's webhook signatures",
        "Twilio console > Account info",
        (),
    ),
    SecretSpec(
        "ANTHROPIC_API_KEY",
        "Claude for message triage and call summaries (not the live call LLM)",
        "console.anthropic.com > API Keys, in a workspace with a spend limit",
        (),
    ),
)


def current_env() -> str:
    env = os.environ.get("FLOW_ENV", "local").strip().lower()
    if env not in ENVIRONMENTS:
        raise ConfigError(f"FLOW_ENV must be one of {', '.join(ENVIRONMENTS)}, got {env!r}")
    return env


class ConfigError(RuntimeError):
    pass


def secret_status(env: str | None = None) -> list[dict]:
    """Report on each secret: set or not, required here or not. Never includes a value."""
    env = env or current_env()
    rows = []
    for s in SECRETS:
        value = os.environ.get(s.name, "")
        ok = bool(value) and len(value) >= s.min_length
        rows.append(
            {
                "name": s.name,
                "set": bool(value),
                "valid": ok,
                "required": env in s.required_in,
                "purpose": s.purpose,
                "where": s.where,
                "min_length": s.min_length,
            }
        )
    return rows


def check_required(env: str | None = None) -> None:
    """Raise ConfigError listing every missing or too-short required secret."""
    env = env or current_env()
    problems = []
    for row in secret_status(env):
        if not row["required"]:
            continue
        if not row["set"]:
            problems.append(f"{row['name']} is not set")
        elif not row["valid"]:
            problems.append(f"{row['name']} is shorter than {row['min_length']} characters")
    if problems:
        raise ConfigError(f"FLOW_ENV={env}: " + "; ".join(problems))


def is_hosted() -> bool:
    return current_env() != "local"
