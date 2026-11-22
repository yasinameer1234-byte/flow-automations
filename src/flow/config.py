"""Runtime settings, loaded from environment variables only.

Secrets (API keys) are never read from files in the repo. See docs/06-secrets.md.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class TenantPolicy:
    """Per-client business rules. Loaded from config/tenants/<tenant>.toml."""

    tenant_id: str = "demo"
    company_name: str = "Demo Client Co"
    min_confidence: float = 0.70
    # Actions that always need a human before they run.
    approval_required: tuple[str, ...] = ("send_email", "issue_refund")
    # Deals above this value are flagged high-value in the approval request.
    high_value_deal_threshold: float = 10_000.0
    auto_acknowledge_support: bool = False


@dataclass(frozen=True)
class Settings:
    db_path: str = "var/flow.db"
    ai_provider: str = "simulated"  # "simulated" | "anthropic"
    ai_model: str = "claude-opus-5-5"
    ai_effort: str = "low"
    email_provider: str = "simulated"
    log_level: str = "INFO"
    log_json: bool = True
    retry_attempts: int = 4
    retry_base_delay: float = 0.5
    tenant: TenantPolicy = field(default_factory=TenantPolicy)

    @property
    def anthropic_key_present(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def load_tenant_policy(path: str | Path | None) -> TenantPolicy:
    if not path:
        return TenantPolicy()
    data = tomllib.loads(Path(path).read_text())
    t = data.get("tenant", {})
    p = data.get("policy", {})
    return TenantPolicy(
        tenant_id=t.get("id", "demo"),
        company_name=t.get("company_name", "Demo Client Co"),
        min_confidence=float(p.get("min_confidence", 0.70)),
        approval_required=tuple(p.get("approval_required", ["send_email", "issue_refund"])),
        high_value_deal_threshold=float(p.get("high_value_deal_threshold", 10_000)),
        auto_acknowledge_support=bool(p.get("auto_acknowledge_support", False)),
    )


def _tenant_path() -> Path | None:
    path = Path(os.environ.get("FLOW_TENANT_CONFIG", "config/tenants/demo.toml"))
    return path if path.exists() else None


def load_settings() -> Settings:
    return Settings(
        db_path=os.environ.get("FLOW_DB_PATH", "var/flow.db"),
        ai_provider=os.environ.get("FLOW_AI_PROVIDER", "simulated"),
        ai_model=os.environ.get("FLOW_AI_MODEL", "claude-opus-5-5"),
        ai_effort=os.environ.get("FLOW_AI_EFFORT", "low"),
        email_provider=os.environ.get("FLOW_EMAIL_PROVIDER", "simulated"),
        log_level=os.environ.get("FLOW_LOG_LEVEL", "INFO"),
        log_json=_env_bool("FLOW_LOG_JSON", True),
        retry_attempts=int(os.environ.get("FLOW_RETRY_ATTEMPTS", "4")),
        retry_base_delay=float(os.environ.get("FLOW_RETRY_BASE_DELAY", "0.5")),
        tenant=load_tenant_policy(_tenant_path()),
    )
