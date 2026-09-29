"""ORM models.

Dates that are business data (hire date, effective date) are stored as ISO
``YYYY-MM-DD`` strings: they compare correctly as strings, they serialize to
JSON without conversion, and they can be used directly inside condition rules.
Timestamps (when something happened) are naive UTC datetimes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class JobProfile(Base):
    __tablename__ = "job_profiles"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    # 1 = Chief Executive ... 6 = Individual Contributor (lower is more senior)
    management_level: Mapped[int]
    pay_range_min: Mapped[int]
    pay_range_mid: Mapped[int]
    pay_range_max: Mapped[int]


class SupervisoryOrg(Base):
    __tablename__ = "supervisory_orgs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    superior_id: Mapped[str | None] = mapped_column(ForeignKey("supervisory_orgs.id"))


class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(200))
    job_profile_id: Mapped[str] = mapped_column(ForeignKey("job_profiles.id"))
    org_id: Mapped[str] = mapped_column(ForeignKey("supervisory_orgs.id"))
    salary: Mapped[int]
    hire_date: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default="Active")
    termination_date: Mapped[str | None] = mapped_column(String(10))

    job_profile: Mapped[JobProfile] = relationship()
    org: Mapped[SupervisoryOrg] = relationship()


class RoleAssignment(Base):
    """A role-based security group assignment on a supervisory organization."""

    __tablename__ = "role_assignments"
    __table_args__ = (UniqueConstraint("role", "org_id", "worker_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(String(60))
    org_id: Mapped[str] = mapped_column(ForeignKey("supervisory_orgs.id"))
    worker_id: Mapped[str] = mapped_column(ForeignKey("workers.id"))


class GroupMembership(Base):
    """Membership in a user-based security group (not tied to an org)."""

    __tablename__ = "group_memberships"
    __table_args__ = (UniqueConstraint("group", "worker_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    group: Mapped[str] = mapped_column(String(60))
    worker_id: Mapped[str] = mapped_column(ForeignKey("workers.id"))


class IdSequence(Base):
    """Simple counter table used to hand out worker IDs for new hires."""

    __tablename__ = "id_sequences"

    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    next_value: Mapped[int]
    version: Mapped[int] = mapped_column(default=1)

    __mapper_args__ = {"version_id_col": version}


class BusinessProcessEvent(Base):
    __tablename__ = "bp_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    bp_type: Mapped[str] = mapped_column(String(40))
    definition_id: Mapped[str] = mapped_column(String(80))
    # Snapshot of the definition taken at initiation, so edits to the YAML
    # files never change the routing of events that are already running.
    definition: Mapped[dict[str, Any]] = mapped_column(JSON)
    definition_trace: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Definitions of this event's sub-processes, also captured at initiation
    # (Workday: a sub-process uses the definition in effect when the parent started).
    subprocess_definitions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    org_id: Mapped[str] = mapped_column(ForeignKey("supervisory_orgs.id"))
    subject_worker_id: Mapped[str | None] = mapped_column(ForeignKey("workers.id"))
    subject_name: Mapped[str] = mapped_column(String(120))
    initiator_id: Mapped[str] = mapped_column(ForeignKey("workers.id"))
    effective_date: Mapped[str] = mapped_column(String(10))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30))
    current_order: Mapped[str | None] = mapped_column(String(8))
    created_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]
    # Touched by every action on the event or any of its sub-processes, so that
    # concurrent actions on *different* parallel steps also conflict (see version).
    updated_at: Mapped[datetime | None]
    parent_event_id: Mapped[int | None] = mapped_column(ForeignKey("bp_events.id"))
    parent_step_id: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(default=1)

    steps: Mapped[list["StepInstance"]] = relationship(
        back_populates="event", order_by="StepInstance.id", cascade="all, delete-orphan"
    )
    tasks: Mapped[list["Task"]] = relationship(
        back_populates="event", order_by="Task.id", cascade="all, delete-orphan"
    )
    audit: Mapped[list["AuditEntry"]] = relationship(
        back_populates="event", order_by="AuditEntry.id", cascade="all, delete-orphan"
    )

    # Optimistic concurrency: two people acting on the same event at the same
    # moment cannot both win; the second write fails with StaleDataError.
    __mapper_args__ = {"version_id_col": version}


class StepInstance(Base):
    """One execution of a definition step for an event.

    When a step is sent back, the affected instances are marked inactive and
    new ones are created, so the full history is kept.
    """

    __tablename__ = "step_instances"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("bp_events.id"))
    step_index: Mapped[int]
    order: Mapped[str] = mapped_column(String(8))
    type: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(30))
    started_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]
    assignees: Mapped[list[str]] = mapped_column(JSON, default=list)
    chain: Mapped[list[str]] = mapped_column(JSON, default=list)
    chain_position: Mapped[int] = mapped_column(default=0)
    completed_by: Mapped[str | None] = mapped_column(String(40))
    note: Mapped[str | None] = mapped_column(Text)
    child_event_id: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(default=True)
    version: Mapped[int] = mapped_column(default=1)

    event: Mapped[BusinessProcessEvent] = relationship(back_populates="steps")

    __mapper_args__ = {"version_id_col": version}


class Task(Base):
    """An item in someone's My Tasks inbox."""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("bp_events.id"))
    step_id: Mapped[int] = mapped_column(ForeignKey("step_instances.id"))
    assignee_id: Mapped[str] = mapped_column(ForeignKey("workers.id"))
    kind: Mapped[str] = mapped_column(String(30))  # initiation | approval | approval_chain | to_do
    status: Mapped[str] = mapped_column(String(20), default="Open")  # Open | Completed | Canceled
    outcome: Mapped[str | None] = mapped_column(String(30))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]
    due_date: Mapped[str | None] = mapped_column(String(10))
    completed_at: Mapped[datetime | None]
    version: Mapped[int] = mapped_column(default=1)

    event: Mapped[BusinessProcessEvent] = relationship(back_populates="tasks")
    step: Mapped[StepInstance] = relationship()

    __mapper_args__ = {"version_id_col": version}


class AuditEntry(Base):
    """Process history: who did what, when."""

    __tablename__ = "audit_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("bp_events.id"))
    at: Mapped[datetime]
    actor_id: Mapped[str | None] = mapped_column(String(40))  # None = system
    action: Mapped[str] = mapped_column(String(60))
    step_name: Mapped[str | None] = mapped_column(String(120))
    detail: Mapped[str | None] = mapped_column(Text)

    event: Mapped[BusinessProcessEvent] = relationship(back_populates="audit")


class DataChange(Base):
    """Before/after snapshot of worker data written when an event completes.

    Rescind uses this to restore the original values.
    """

    __tablename__ = "data_changes"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("bp_events.id"))
    worker_id: Mapped[str] = mapped_column(String(40))
    change_type: Mapped[str] = mapped_column(String(20))  # create | update
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    applied_at: Mapped[datetime]
    reverted_at: Mapped[datetime | None]


class OutboundMessage(Base):
    """A record sent to a (simulated) downstream system by an Integration step."""

    __tablename__ = "outbound_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("bp_events.id"))
    integration: Mapped[str] = mapped_column(String(120))
    change_type: Mapped[str] = mapped_column(String(30))
    worker_id: Mapped[str | None] = mapped_column(String(40))
    effective_date: Mapped[str] = mapped_column(String(10))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime]
