-- Phase 6F recommended schema additions.
-- Apply to the existing Phase 6E database after backing it up.

CREATE TABLE IF NOT EXISTS candidate_access (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL,
    examination_id INTEGER NOT NULL,
    eligible INTEGER NOT NULL DEFAULT 1,
    max_attempts INTEGER NOT NULL DEFAULT 1,
    attempts_used INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(candidate_id, examination_id)
);

CREATE TABLE IF NOT EXISTS security_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type VARCHAR(80) NOT NULL,
    candidate_id INTEGER,
    attempt_id INTEGER,
    ip_address VARCHAR(64),
    details TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_security_audit_candidate
ON security_audit(candidate_id);

CREATE INDEX IF NOT EXISTS idx_security_audit_attempt
ON security_audit(attempt_id);
