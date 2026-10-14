"""SQLAlchemy Core schema. Timestamps are Unix epoch nanoseconds (BigInteger).

Integer nanoseconds keep window arithmetic exact and identical on
PostgreSQL and SQLite, and match OTLP's native time representation.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
)

metadata = MetaData()

span_facts = Table(
    "span_facts",
    metadata,
    Column("trace_id", String(32), nullable=False),
    Column("span_id", String(16), nullable=False),
    Column("parent_span_id", String(16)),
    Column("kind", String(16), nullable=False),
    Column("name", String(128), nullable=False),
    Column("operation", String(64), nullable=False),
    Column("agent", String(128), nullable=False),
    Column("team", String(128), nullable=False),
    Column("service", String(128), nullable=False),
    Column("provider", String(128)),
    Column("model", String(128)),
    Column("start_ns", BigInteger, nullable=False),
    Column("end_ns", BigInteger, nullable=False),
    Column("duration_ms", Float, nullable=False),
    Column("is_error", Boolean, nullable=False),
    Column("error_type", String(256)),
    Column("error_message", String(512)),
    Column("http_status", Integer),
    Column("finish_reasons", String(256), nullable=False, default=""),
    Column("input_tokens", BigInteger, nullable=False, default=0),
    Column("output_tokens", BigInteger, nullable=False, default=0),
    Column("cached_input_tokens", BigInteger, nullable=False, default=0),
    Column("cost_usd", Float),
    Column("retry_attempt", Integer),
    Column("workflow", String(128)),
    Column("ingested_by", String(512), nullable=False),
    Column("ingested_ns", BigInteger, nullable=False),
    PrimaryKeyConstraint("trace_id", "span_id", name="pk_span_facts"),
    Index("ix_span_facts_kind_end", "kind", "end_ns"),
    Index("ix_span_facts_name_end", "name", "end_ns"),
    Index("ix_span_facts_agent_end", "agent", "end_ns"),
    Index("ix_span_facts_team_end", "team", "end_ns"),
)

workflow_runs = Table(
    "workflow_runs",
    metadata,
    Column("trace_id", String(32), primary_key=True),
    Column("workflow", String(128)),
    Column("agent", String(128)),
    Column("team", String(128)),
    Column("root_seen", Boolean, nullable=False, default=False),
    Column("first_seen_ns", BigInteger, nullable=False),
    Column("last_seen_ns", BigInteger, nullable=False),
    Column("finalized", Boolean, nullable=False, default=False),
    Column("outcome", String(16)),
    Column("failure_class", String(32)),
    Column("started_ns", BigInteger),
    Column("ended_ns", BigInteger),
    Column("cost_usd", Float),
    Column("diagnosis", Text),
    Index("ix_workflow_runs_pending", "finalized", "last_seen_ns"),
    Index("ix_workflow_runs_workflow_end", "workflow", "ended_ns"),
    Index("ix_workflow_runs_outcome_end", "outcome", "ended_ns"),
)

prices = Table(
    "prices",
    metadata,
    Column("model", String(128), nullable=False),
    Column("effective_from_ns", BigInteger, nullable=False),
    Column("document", Text, nullable=False),
    Column("set_by", String(512), nullable=False),
    Column("set_ns", BigInteger, nullable=False),
    PrimaryKeyConstraint("model", "effective_from_ns", name="pk_prices"),
)

slos = Table(
    "slos",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("document", Text, nullable=False),
    Column("created_ns", BigInteger, nullable=False),
    Column("updated_ns", BigInteger, nullable=False),
    Column("updated_by", String(512), nullable=False),
)

slo_alert_state = Table(
    "slo_alert_state",
    metadata,
    Column("slo_id", String(64), nullable=False),
    Column("rule_id", String(64), nullable=False),
    Column("firing", Boolean, nullable=False, default=False),
    Column("since_ns", BigInteger, nullable=False),
    PrimaryKeyConstraint("slo_id", "rule_id", name="pk_slo_alert_state"),
)

slo_alert_events = Table(
    "slo_alert_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("slo_id", String(64), nullable=False),
    Column("rule_id", String(64), nullable=False),
    Column("severity", String(16), nullable=False),
    Column("transition", String(16), nullable=False),
    Column("at_ns", BigInteger, nullable=False),
    Column("long_burn", Float),
    Column("short_burn", Float),
    Index("ix_slo_alert_events_slo", "slo_id", "at_ns"),
)

recovery_actions = Table(
    "recovery_actions",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("status", String(24), nullable=False),
    Column("target_kind", String(16), nullable=False),
    Column("target_name", String(128), nullable=False),
    Column("action", String(32), nullable=False),
    Column("expires_ns", BigInteger, nullable=False),
    Column("proposed_ns", BigInteger, nullable=False),
    Column("document", Text, nullable=False),
    Index("ix_recovery_actions_status", "status", "expires_ns"),
    Index("ix_recovery_actions_target", "target_kind", "target_name"),
)

schema_version = Table(
    "schema_version",
    metadata,
    Column("version", Integer, primary_key=True),
)

SCHEMA_VERSION = 1
