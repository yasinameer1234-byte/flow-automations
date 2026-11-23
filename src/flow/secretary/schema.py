"""Schema for the platform (tenants, users) and the AI Secretary module.

Plain SQL that ports to Postgres. Every business table carries tenant_id.
"""

PLATFORM_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    package TEXT NOT NULL,              -- starter | growth | scale
    status TEXT NOT NULL DEFAULT 'active',
    api_key_hash TEXT,
    is_demo INTEGER NOT NULL DEFAULT 0,  -- synthetic demo business: public demo page allowed
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,                 -- platform_admin | owner | staff | viewer
    tenant_id TEXT REFERENCES tenants(id),  -- NULL only for platform_admin
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS business_profiles (
    tenant_id TEXT PRIMARY KEY REFERENCES tenants(id),
    timezone TEXT NOT NULL,
    hours_json TEXT NOT NULL,           -- {"mon": ["08:00","17:00"], ...}
    disclosure TEXT NOT NULL,
    recording_enabled INTEGER NOT NULL DEFAULT 0,
    emergency_keywords_json TEXT NOT NULL,
    emergency_instructions TEXT NOT NULL,
    avg_job_value REAL NOT NULL,
    est_close_rate REAL NOT NULL,
    minutes_saved_per_call REAL NOT NULL,
    booking_lead_minutes INTEGER NOT NULL DEFAULT 60,
    booking_horizon_days INTEGER NOT NULL DEFAULT 7
);
CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,
    keywords_json TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL,
    bookable INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS knowledge_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    question TEXT NOT NULL,
    keywords_json TEXT NOT NULL,
    answer TEXT NOT NULL,
    approved INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS staff_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    handles_emergencies INTEGER NOT NULL DEFAULT 0,
    available INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    lead_id INTEGER,
    service_id INTEGER NOT NULL,
    customer_name TEXT NOT NULL,
    customer_phone TEXT NOT NULL,
    starts_at TEXT NOT NULL,            -- UTC ISO
    ends_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'booked',   -- booked | cancelled | completed
    source_call_id TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_appt_slot
    ON appointments (tenant_id, starts_at) WHERE status = 'booked';
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    name TEXT,
    phone TEXT,
    reason TEXT,
    service_id INTEGER,
    status TEXT NOT NULL DEFAULT 'new',  -- new | booked | callback | won | lost
    qualified INTEGER NOT NULL DEFAULT 0,
    estimated_value REAL,
    confirmed_value REAL,                -- only set when a job is marked won
    source TEXT NOT NULL,
    source_call_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calls (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    channel TEXT NOT NULL,              -- browser_demo | phone
    caller_number TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_seconds INTEGER,
    billed_minutes INTEGER,
    after_hours INTEGER NOT NULL DEFAULT 0,
    -- outcome: booked | answered | callback | transferred | emergency | abandoned | disabled
    outcome TEXT,
    summary TEXT,
    lead_id INTEGER,
    appointment_id INTEGER,
    state_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS call_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL REFERENCES calls(id),
    tenant_id TEXT NOT NULL,
    speaker TEXT NOT NULL,              -- caller | agent | system
    text TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    kind TEXT NOT NULL,                 -- callback | emergency_callback
    title TEXT NOT NULL,
    lead_id INTEGER,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    channel TEXT NOT NULL,              -- sms | email
    to_addr TEXT NOT NULL,
    body TEXT NOT NULL,
    purpose TEXT NOT NULL,              -- transactional | marketing
    -- sent_simulated | suppressed | sent (provider accepted) | delivered | undelivered | failed
    status TEXT NOT NULL,
    related_appointment_id INTEGER,
    provider TEXT,                      -- simulated | twilio
    provider_id TEXT,                   -- the provider's message id, for delivery receipts
    error TEXT,
    updated_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    period TEXT NOT NULL,               -- YYYY-MM
    kind TEXT NOT NULL,                 -- voice_minutes | sms | ai_calls
    quantity REAL NOT NULL,
    ref TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS automations (
    tenant_id TEXT NOT NULL,
    key TEXT NOT NULL,                  -- ai_secretary | booking_confirmations | ...
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_by TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, key)
);
"""

PACKAGES = {
    "starter": {"label": "Starter: AI Secretary", "monthly_cad": 399, "minutes": 300},
    "growth": {"label": "Growth: Revenue Engine", "monthly_cad": 999, "minutes": 500},
    "scale": {"label": "Scale: Business Automation", "monthly_cad": 1999, "minutes": 1000},
}
