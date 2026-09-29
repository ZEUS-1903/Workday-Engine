"""Turn ORM objects into JSON-friendly dictionaries for the API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..engine.engine import BP_TYPES, BPEngine
from ..models import (
    BusinessProcessEvent,
    DataChange,
    JobProfile,
    OutboundMessage,
    StepInstance,
    SupervisoryOrg,
    Task,
    Worker,
)


def iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") + "Z" if value else None


class Lookup:
    """Per-request cache of names so serializers do not query in loops."""

    def __init__(self, session: Session) -> None:
        self.workers = {w.id: w.name for w in session.scalars(select(Worker))}
        self.orgs = {o.id: o.name for o in session.scalars(select(SupervisoryOrg))}
        self.job_profiles = {j.id: j.name for j in session.scalars(select(JobProfile))}

    def person(self, worker_id: str | None) -> dict[str, str] | None:
        if worker_id is None:
            return None
        return {"id": worker_id, "name": self.workers.get(worker_id, worker_id)}

    def org(self, org_id: str | None) -> dict[str, str] | None:
        if org_id is None:
            return None
        return {"id": org_id, "name": self.orgs.get(org_id, org_id)}


def event_summary(event: BusinessProcessEvent, lookup: Lookup) -> dict[str, Any]:
    open_steps = [s.name for s in event.steps if s.active and s.status in ("Awaiting Action", "In Progress")]
    return {
        "id": event.id,
        "bp_type": event.bp_type,
        "bp_label": BP_TYPES[event.bp_type].label,
        "title": f"{BP_TYPES[event.bp_type].label}: {event.subject_name}",
        "subject": {"id": event.subject_worker_id, "name": event.subject_name},
        "org": lookup.org(event.org_id),
        "initiator": lookup.person(event.initiator_id),
        "effective_date": event.effective_date,
        "status": event.status,
        "awaiting": open_steps,
        "created_at": iso(event.created_at),
        "completed_at": iso(event.completed_at),
        "parent_event_id": event.parent_event_id,
        "definition_name": event.definition.get("definition_name"),
    }


def step_dict(step: StepInstance, lookup: Lookup) -> dict[str, Any]:
    return {
        "id": step.id,
        "step_index": step.step_index,
        "order": step.order,
        "type": step.type,
        "name": step.name,
        "status": step.status,
        "active": step.active,
        "assignees": [lookup.person(a) for a in step.assignees],
        "chain": [lookup.person(a) for a in step.chain],
        "chain_position": step.chain_position,
        "completed_by": lookup.person(step.completed_by),
        "started_at": iso(step.started_at),
        "completed_at": iso(step.completed_at),
        "note": step.note,
        "child_event_id": step.child_event_id,
    }


def task_dict(engine: BPEngine, task: Task, lookup: Lookup) -> dict[str, Any]:
    event = task.event
    root = event
    while root.parent_event_id is not None:
        root = engine.s.get(BusinessProcessEvent, root.parent_event_id)
    definition = engine.definition_for(event)
    step_def = definition.steps[task.step.step_index]
    return {
        "id": task.id,
        "kind": task.kind,
        "status": task.status,
        "outcome": task.outcome,
        "comment": task.comment,
        "assignee": lookup.person(task.assignee_id),
        "step": {"id": task.step.id, "name": task.step.name, "order": task.step.order},
        "instructions": step_def.instructions,
        "due_date": task.due_date,
        "created_at": iso(task.created_at),
        "completed_at": iso(task.completed_at),
        "event": event_summary(event, lookup),
        "root_event_id": root.id,
        "send_back_targets": [
            {"id": s.id, "name": s.name, "order": s.order, "completed_by": lookup.person(s.completed_by)}
            for s in engine.send_back_targets(task)
        ],
    }


def event_detail(engine: BPEngine, event: BusinessProcessEvent, user_id: str, lookup: Lookup) -> dict[str, Any]:
    session = engine.s
    definition = engine.definition_for(event)
    current = None
    if event.subject_worker_id and event.status == "In Progress":
        worker = session.get(Worker, event.subject_worker_id)
        if worker is not None:
            current = {
                "job_profile_id": worker.job_profile_id,
                "job_profile": lookup.job_profiles.get(worker.job_profile_id),
                "org": lookup.org(worker.org_id),
                "salary": worker.salary,
                "status": worker.status,
                "hire_date": worker.hire_date,
            }
    children = session.scalars(
        select(BusinessProcessEvent).where(BusinessProcessEvent.parent_event_id == event.id)
    ).all()
    changes = session.scalars(select(DataChange).where(DataChange.event_id == event.id)).all()
    outbound = session.scalars(select(OutboundMessage).where(OutboundMessage.event_id == event.id)).all()
    my_tasks = [t for t in event.tasks if t.assignee_id == user_id and t.status == "Open"]

    data = dict(event.data)
    if "job_profile_id" in data:
        data["job_profile"] = lookup.job_profiles.get(data["job_profile_id"])
    if "org_id" in data:
        data["org"] = lookup.org(data["org_id"])

    return {
        **event_summary(event, lookup),
        "definition_id": event.definition_id,
        "definition_trace": event.definition_trace,
        "definition_steps": [
            {
                "index": index,
                "order": s.order,
                "type": s.type,
                "name": s.name,
                "groups": s.groups,
                "alternate_groups": s.alternate_groups,
                "routing_restrictions": s.routing_restrictions,
                "entry_condition": s.entry_condition,
                "while_running_condition": s.while_running_condition,
            }
            for index, s in enumerate(definition.steps)
        ],
        "data": data,
        "current": current,
        "warnings": event.warnings,
        "steps": [step_dict(s, lookup) for s in event.steps],
        "tasks": [
            {
                "id": t.id,
                "step_id": t.step_id,
                "kind": t.kind,
                "assignee": lookup.person(t.assignee_id),
                "status": t.status,
                "outcome": t.outcome,
                "comment": t.comment,
                "due_date": t.due_date,
                "completed_at": iso(t.completed_at),
            }
            for t in event.tasks
        ],
        "history": [
            {
                "at": iso(a.at),
                "actor": lookup.person(a.actor_id) or {"id": None, "name": "System"},
                "action": a.action,
                "step_name": a.step_name,
                "detail": a.detail,
            }
            for a in event.audit
        ],
        "children": [event_summary(c, lookup) for c in children],
        "data_changes": [
            {
                "worker_id": c.worker_id,
                "change_type": c.change_type,
                "before": c.before,
                "after": c.after,
                "applied_at": iso(c.applied_at),
                "reverted_at": iso(c.reverted_at),
            }
            for c in changes
        ],
        "outbound": [outbound_dict(m) for m in outbound],
        "available_actions": engine.available_actions(event, user_id),
        "my_open_tasks": [task_dict(engine, t, lookup) for t in my_tasks],
    }


def outbound_dict(message: OutboundMessage) -> dict[str, Any]:
    return {
        "id": message.id,
        "event_id": message.event_id,
        "integration": message.integration,
        "record_type": message.change_type,
        "worker_id": message.worker_id,
        "effective_date": message.effective_date,
        "payload": message.payload,
        "created_at": iso(message.created_at),
    }


def worker_dict(worker: Worker, lookup: Lookup, show_compensation: bool = True) -> dict[str, Any]:
    return {
        "id": worker.id,
        "name": worker.name,
        "email": worker.email,
        "job_profile": {"id": worker.job_profile_id, "name": lookup.job_profiles.get(worker.job_profile_id)},
        "management_level": worker.job_profile.management_level,
        "org": lookup.org(worker.org_id),
        "salary": worker.salary if show_compensation else None,
        "hire_date": worker.hire_date,
        "status": worker.status,
        "termination_date": worker.termination_date,
    }
