"""The business process engine: a state machine over events, steps and tasks.

Lifecycle of an event::

    initiate ──> In Progress ──(all step groups finish)──> Successfully Completed ──rescind──> Rescinded
                     │  │
                     │  └── deny (at an approval step) ──> Denied
                     └───── cancel (while in progress) ──> Canceled

Design decisions worth knowing about (they are simplifications, not claims
about how Workday itself is implemented):

* Worker data is written only when the top-level event completes. Deny and
  Cancel therefore have nothing to undo; Rescind restores the before-values
  recorded in :class:`~app.models.DataChange`.
* A worker can have only one in-progress top-level event at a time.
* If a sub-process is denied, its parent is denied as well.
* Routing restrictions that exclude every assignee send the step to its
  alternate groups; with no alternate, the step is skipped (Workday documents
  this behavior). If nobody holds the role at all, the step tries the alternate
  groups and then Business Process Administrators (an engine choice).
* When a Termination completes, the worker's open tasks are routed again as if
  the step had just started (an engine choice; Workday has separate reassignment
  tools).
* Deny and Send Back require a comment (an engine choice).
* Every action touches the top-level event, so two actions on the same business
  process (even on different parallel steps) cannot both commit on stale state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from ..models import (
    AuditEntry,
    BusinessProcessEvent,
    DataChange,
    GroupMembership,
    IdSequence,
    JobProfile,
    OutboundMessage,
    RoleAssignment,
    StepInstance,
    SupervisoryOrg,
    Task,
    Worker,
)
from .conditions import ConditionError, is_true
from .definitions import BusinessProcessDefinition, DefinitionRegistry, StepDefinition
from .routing import Directory

# ---- statuses -----------------------------------------------------------
IN_PROGRESS = "In Progress"
SUCCESSFULLY_COMPLETED = "Successfully Completed"
DENIED = "Denied"
CANCELED = "Canceled"
RESCINDED = "Rescinded"

STEP_AWAITING = "Awaiting Action"
STEP_IN_PROGRESS = "In Progress"  # a sub-process step whose child event is running
STEP_COMPLETED = "Completed"
STEP_NOT_REQUIRED = "Not Required"
STEP_DENIED = "Denied"
STEP_CANCELED = "Canceled"
STEP_SENT_BACK = "Sent Back"
OPEN_STEP_STATUSES = frozenset({STEP_AWAITING, STEP_IN_PROGRESS})

TASK_OPEN = "Open"
TASK_COMPLETED = "Completed"
TASK_CANCELED = "Canceled"

SEND_BACK_TARGET_TYPES = frozenset({"initiation", "approval", "to_do"})


# ---- errors -------------------------------------------------------------
class EngineError(Exception):
    status_code = 400

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.errors = errors or []


class NotFound(EngineError):
    status_code = 404


class PermissionDenied(EngineError):
    status_code = 403


class InvalidState(EngineError):
    status_code = 409


class ValidationFailed(EngineError):
    status_code = 422


# ---- time ---------------------------------------------------------------
class Clock(Protocol):
    def now(self) -> datetime: ...

    def today(self) -> date: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def today(self) -> date:
        return self.now().date()


# ---- business process types ---------------------------------------------
@dataclass(frozen=True)
class BPType:
    key: str
    label: str
    subject: str  # "new_hire" | "existing_worker" | "inherited"
    payroll_record: str
    subprocess_only: bool = False
    editable_fields: frozenset[str] = frozenset()
    reasons: tuple[str, ...] = ()


CHANGE_JOB_REASONS = ("Promotion", "Transfer", "Merit Increase", "Market Adjustment", "Other")
TERMINATION_REASONS = ("Resignation", "Retirement", "Layoff", "Performance", "Other")

BP_TYPES: dict[str, BPType] = {
    "hire": BPType(
        "hire", "Hire", "new_hire", "HIRE",
        editable_fields=frozenset({"name", "email", "job_profile_id", "salary", "effective_date"}),
    ),
    "change_job": BPType(
        "change_job", "Change Job", "existing_worker", "JOB_CHANGE",
        editable_fields=frozenset({"job_profile_id", "org_id", "salary", "reason", "effective_date"}),
        reasons=CHANGE_JOB_REASONS,
    ),
    "termination": BPType(
        "termination", "Termination", "existing_worker", "TERMINATION",
        editable_fields=frozenset({"reason", "effective_date"}),
        reasons=TERMINATION_REASONS,
    ),
    "propose_compensation": BPType(
        "propose_compensation", "Propose Compensation", "inherited", "COMPENSATION",
        subprocess_only=True,
    ),
}

JOB_FIELDS = ("job_profile_id", "org_id", "salary")


class BPEngine:
    """Operates on one database session. The caller owns the transaction."""

    def __init__(self, session: Session, registry: DefinitionRegistry, clock: Clock | None = None) -> None:
        self.s = session
        self.registry = registry
        self.clock = clock or SystemClock()
        self.directory = Directory(session)
        self._advancing: set[int] = set()
        self._definition_cache: dict[int, BusinessProcessDefinition] = {}

    # =====================================================================
    # Public API
    # =====================================================================
    def initiate(
        self,
        bp_type: str,
        initiator_id: str,
        effective_date: str,
        data: dict[str, Any],
        subject_worker_id: str | None = None,
    ) -> BusinessProcessEvent:
        bpt = BP_TYPES.get(bp_type)
        if bpt is None or bp_type not in self.registry.bp_types():
            raise NotFound(f"Unknown business process type {bp_type!r}")
        if bpt.subprocess_only:
            raise InvalidState(f"{bpt.label} only runs as a sub-process of another business process")
        initiator = self._require_active_worker(initiator_id)

        data = {k: v for k, v in data.items() if v is not None}
        data["effective_date"] = effective_date
        subject: Worker | None = None

        if bpt.subject == "new_hire":
            org_id = data.get("org_id")
            self._require_org(org_id)
            self._require_job_profile(data.get("job_profile_id"))
            subject_name = data["name"]
        else:
            subject = self.s.get(Worker, subject_worker_id) if subject_worker_id else None
            if subject is None:
                raise NotFound("Worker not found")
            if subject.status != "Active":
                raise InvalidState(f"{subject.name} is not an active worker")
            if subject.id == initiator_id:
                raise PermissionDenied(
                    "You cannot start this business process on yourself (self-service is not modeled)"
                )
            org_id = subject.org_id
            subject_name = subject.name
            if bp_type == "change_job":
                for field in JOB_FIELDS:
                    data.setdefault(field, getattr(subject, field))
                self._require_org(data["org_id"])
                self._require_job_profile(data["job_profile_id"])
            pending = self.s.scalar(
                select(BusinessProcessEvent).where(
                    BusinessProcessEvent.subject_worker_id == subject.id,
                    BusinessProcessEvent.status == IN_PROGRESS,
                    BusinessProcessEvent.parent_event_id.is_(None),
                )
            )
            if pending is not None:
                raise InvalidState(
                    f"{subject.name} already has an in-progress {BP_TYPES[pending.bp_type].label} "
                    f"(#{pending.id}). Complete or cancel it first."
                )

        policy = self.registry.policy(bp_type)
        if not self.directory.user_in_any(initiator_id, policy.initiate, org_id):
            raise PermissionDenied(
                f"{initiator.name} cannot initiate {bpt.label} for this organization. "
                f"Allowed security groups: {', '.join(policy.initiate)}."
            )

        self._validate_reason(bpt, data)
        definition, trace = self.registry.resolve(bp_type, self.directory.org_chain_ids(org_id))
        warnings = self._run_validations(definition, data, subject.id if subject else None, initiator_id)
        if bpt.subject == "new_hire":
            data["worker_id"] = self._next_worker_id()

        event = BusinessProcessEvent(
            bp_type=bp_type,
            definition_id=definition.id,
            definition=definition.model_dump(mode="json"),
            definition_trace=trace,
            subprocess_definitions=self._snapshot_subprocess_definitions(definition, org_id),
            org_id=org_id,
            subject_worker_id=subject.id if subject else None,
            subject_name=subject_name,
            initiator_id=initiator_id,
            effective_date=effective_date,
            data=data,
            warnings=warnings,
            status=IN_PROGRESS,
            created_at=self.clock.now(),
        )
        self.s.add(event)
        self.s.flush()
        self._definition_cache[event.id] = definition
        self._audit(event, "Initiated", initiator_id, definition.steps[0].name, f"Definition: {definition.definition_name}")
        for warning in warnings:
            self._audit(event, "Validation Warning", None, None, warning)
        self._complete_initiation(event, definition, initiator_id)
        self._advance(event)
        return event

    def approve(self, task_id: int, user_id: str, comment: str | None = None) -> BusinessProcessEvent:
        task = self._open_task(task_id, user_id, {"approval", "approval_chain"}, "Approve")
        event, step = task.event, task.step
        definition = self._definition(event)
        step_def = definition.steps[step.step_index]
        self._close_task(task, "Approved", comment)
        self._audit(event, "Approved", user_id, step.name, comment)

        if step.type == "approval":
            still_open = [t for t in self._step_tasks(step) if t.status == TASK_OPEN]
            if step_def.all_must_approve and still_open:
                return event
            for other in still_open:
                self._cancel_task(other, f"Approved by {self._name(user_id)}")
            self._finish_step(step, user_id)
        else:
            next_approver = self._advance_chain(step)
            if next_approver is not None:
                self._create_task(event, step, next_approver, "approval_chain", step_def.due_in_days)
                self._audit(event, "Routed Up Chain", None, step.name, f"Next approver: {self._name(next_approver)}")
                return event
            self._finish_step(step, user_id)

        self._advance(event)
        return event

    def deny(self, task_id: int, user_id: str, comment: str | None) -> BusinessProcessEvent:
        if not comment or not comment.strip():
            raise ValidationFailed("A comment is required to deny", ["comment is required"])
        task = self._open_task(task_id, user_id, {"approval", "approval_chain"}, "Deny")
        event, step = task.event, task.step
        self._close_task(task, "Denied", comment)
        step.status = STEP_DENIED
        step.completed_at = self.clock.now()
        step.completed_by = user_id
        self._audit(event, "Denied", user_id, step.name, comment)
        self._terminate(event, DENIED, user_id, f"Denied at step '{step.name}'")
        return event

    def send_back(self, task_id: int, user_id: str, to_step_id: int, comment: str | None) -> BusinessProcessEvent:
        if not comment or not comment.strip():
            raise ValidationFailed("A comment is required to send back", ["comment is required"])
        task = self._open_task(task_id, user_id, {"approval", "approval_chain"}, "Send Back")
        event, step = task.event, task.step
        target = next((t for t in self.send_back_targets(task) if t.id == to_step_id), None)
        if target is None:
            raise InvalidState(
                "You can only send back to an earlier completed Initiation, Approval or To Do step "
                "of this business process (or the Initiation step of its parent)"
            )
        self._close_task(task, "Sent Back", comment)
        self._audit(event, "Sent Back", user_id, step.name, f"To '{target.name}' ({self._name(target.completed_by)}): {comment}")

        target_event = target.event
        if target_event.id != event.id:
            # Sending back out of a sub-process into its parent: the sub-process
            # is canceled when the parent's sub-process step is superseded.
            step.status = STEP_SENT_BACK
            step.completed_at = self.clock.now()
            self._audit(target_event, "Sent Back", user_id, step.name, f"From sub-process #{event.id}: {comment}")
        self._reopen(target_event, target, user_id)
        return target_event

    def _reopen(self, event: BusinessProcessEvent, target: StepInstance, user_id: str) -> None:
        """Supersede ``target`` and every later step, then reopen ``target``."""
        now = self.clock.now()
        for instance in list(event.steps):
            if not instance.active:
                continue
            if instance.id == target.id or instance.order > target.order:
                if instance.status in OPEN_STEP_STATUSES:
                    instance.status = STEP_SENT_BACK
                    instance.completed_at = now
                for open_task in self._step_tasks(instance):
                    if open_task.status == TASK_OPEN:
                        self._cancel_task(open_task, "Step was sent back")
                if instance.child_event_id:
                    child = self.s.get(BusinessProcessEvent, instance.child_event_id)
                    if child is not None and child.status == IN_PROGRESS:
                        self._terminate(child, CANCELED, user_id, "Parent step was sent back", propagate=False)
                instance.active = False

        step_def = self._definition(event).steps[target.step_index]
        reopened = StepInstance(
            event=event,
            step_index=target.step_index,
            order=target.order,
            type=target.type,
            name=target.name,
            status=STEP_AWAITING,
            started_at=now,
            assignees=[target.completed_by],
            chain=[],
            note=f"Sent back by {self._name(user_id)}",
        )
        self.s.add(reopened)
        self.s.flush()
        self._create_task(event, reopened, target.completed_by, target.type, step_def.due_in_days or 2)
        event.current_order = target.order

    def submit(
        self, task_id: int, user_id: str, data: dict[str, Any] | None = None, comment: str | None = None
    ) -> BusinessProcessEvent:
        """Complete a To Do, or resubmit an initiation step that was sent back."""
        task = self._open_task(task_id, user_id, {"initiation", "to_do"}, "Submit")
        event, step = task.event, task.step

        if task.kind == "initiation":
            bpt = BP_TYPES[event.bp_type]
            changes = {k: v for k, v in (data or {}).items() if v is not None}
            unknown = set(changes) - bpt.editable_fields
            if unknown:
                raise ValidationFailed(
                    f"These fields cannot be changed on resubmission: {', '.join(sorted(unknown))}",
                    [f"{field} is not editable" for field in sorted(unknown)],
                )
            new_data = {**event.data, **changes}
            self._validate_reason(bpt, new_data)
            if "job_profile_id" in changes:
                self._require_job_profile(changes["job_profile_id"])
            if "org_id" in changes:
                self._require_org(changes["org_id"])
            warnings = self._run_validations(
                self._definition(event), new_data, event.subject_worker_id, event.initiator_id
            )
            event.data = new_data
            event.effective_date = new_data["effective_date"]
            event.warnings = warnings
            if event.bp_type == "hire":
                event.subject_name = new_data["name"]
            outcome = "Resubmitted"
        else:
            outcome = "Done"

        self._close_task(task, outcome, comment)
        for other in self._step_tasks(step):
            if other.status == TASK_OPEN:
                self._cancel_task(other, f"Completed by {self._name(user_id)}")
        self._finish_step(step, user_id)
        self._audit(event, outcome, user_id, step.name, comment)
        self._advance(event)
        return event

    def cancel(self, event_id: int, user_id: str, comment: str | None = None) -> BusinessProcessEvent:
        event = self._get_visible_event(event_id, user_id)
        if event.parent_event_id is not None:
            raise InvalidState("This is a sub-process. Cancel its parent business process instead.")
        policy = self.registry.policy(event.bp_type)
        if not self.directory.user_in_any(user_id, policy.cancel, event.org_id, event.initiator_id):
            raise PermissionDenied(f"Cancel is limited to: {', '.join(policy.cancel)}")
        if event.status != IN_PROGRESS:
            raise InvalidState(
                f"Only in-progress business processes can be canceled (this one is {event.status}). "
                "A completed business process is reversed with Rescind."
            )
        self._touch(event)
        self._terminate(event, CANCELED, user_id, comment or "Canceled")
        return event

    def rescind(self, event_id: int, user_id: str, comment: str | None = None) -> BusinessProcessEvent:
        event = self._get_visible_event(event_id, user_id)
        if event.parent_event_id is not None:
            raise InvalidState("This is a sub-process. Rescind its parent business process instead.")
        policy = self.registry.policy(event.bp_type)
        if not self.directory.user_in_any(user_id, policy.rescind, event.org_id, event.initiator_id):
            raise PermissionDenied(f"Rescind is limited to: {', '.join(policy.rescind)}")
        if event.status != SUCCESSFULLY_COMPLETED:
            raise InvalidState(
                f"Only successfully completed business processes can be rescinded (this one is {event.status}). "
                "An in-progress business process is stopped with Cancel."
            )
        self._touch(event)

        changes = self.s.scalars(
            select(DataChange)
            .where(DataChange.event_id == event.id, DataChange.reverted_at.is_(None))
            .order_by(DataChange.id.desc())
        ).all()
        for change in changes:
            later = self.s.scalars(
                select(DataChange.event_id).where(
                    DataChange.worker_id == change.worker_id,
                    DataChange.id > change.id,
                    DataChange.reverted_at.is_(None),
                )
            ).all()
            if later:
                ids = ", ".join(f"#{i}" for i in sorted(set(later)))
                raise InvalidState(f"Rescind the later business processes for this worker first: {ids}")
            pending = self.s.scalars(
                select(BusinessProcessEvent.id).where(
                    BusinessProcessEvent.subject_worker_id == change.worker_id,
                    BusinessProcessEvent.status == IN_PROGRESS,
                )
            ).all()
            if pending:
                ids = ", ".join(f"#{i}" for i in pending)
                raise InvalidState(f"Cancel the in-progress business processes for this worker first: {ids}")

        now = self.clock.now()
        for change in changes:
            worker = self.s.get(Worker, change.worker_id)
            if worker is None:
                continue
            if change.change_type == "create":
                self._delete_hired_worker(worker)
            else:
                for field, value in (change.before or {}).items():
                    setattr(worker, field, value)
            change.reverted_at = now

        event.status = RESCINDED
        for child in self._children(event):
            if child.status == SUCCESSFULLY_COMPLETED:
                child.status = RESCINDED
        self._audit(event, "Rescinded", user_id, None, comment)
        self._send_reversal(event, "RESCIND", comment)
        return event

    # ---- queries used by the API ------------------------------------------
    def inbox(self, user_id: str) -> list[Task]:
        tasks = self.s.scalars(
            select(Task).where(Task.assignee_id == user_id, Task.status == TASK_OPEN).order_by(Task.id)
        ).all()
        return sorted(tasks, key=lambda t: (t.due_date is None, t.due_date or "", t.id))

    def send_back_targets(self, task: Task) -> list[StepInstance]:
        if task.kind not in ("approval", "approval_chain") or task.status != TASK_OPEN:
            return []
        event = task.event
        in_subprocess = event.parent_event_id is not None
        targets = [
            step
            for step in event.steps
            if step.active
            and step.status == STEP_COMPLETED
            and step.type in SEND_BACK_TARGET_TYPES
            and step.order < task.step.order
            and step.completed_by is not None
            # a sub-process's own initiation was completed by the system
            and not (in_subprocess and step.type == "initiation")
        ]
        if in_subprocess:
            # From inside a sub-process you can also send back to the parent's Initiation
            parent = self.s.get(BusinessProcessEvent, event.parent_event_id)
            parent_step = self.s.get(StepInstance, event.parent_step_id)
            if parent is not None and parent_step is not None and parent_step.active:
                targets += [
                    step
                    for step in parent.steps
                    if step.active
                    and step.status == STEP_COMPLETED
                    and step.type == "initiation"
                    and step.order < parent_step.order
                    and step.completed_by is not None
                ]
        return targets

    def can_view(self, event: BusinessProcessEvent, user_id: str) -> bool:
        root = self._root(event)
        family = self._family(root)
        if any(e.initiator_id == user_id for e in family):
            return True
        # Workers see their own events only once completed, so a pending
        # termination or pay change (and approvers' comments) stay private.
        if root.subject_worker_id == user_id and root.status == SUCCESSFULLY_COMPLETED:
            return True
        if any(t.assignee_id == user_id for e in family for t in e.tasks):
            return True
        return self.directory.user_in_any(
            user_id,
            ["HR Administrator", "Business Process Administrator", "Manager", "HR Partner", "Compensation Partner"],
            root.org_id,
        )

    def available_actions(self, event: BusinessProcessEvent, user_id: str) -> dict[str, bool]:
        if event.parent_event_id is not None:
            return {"cancel": False, "rescind": False}
        policy = self.registry.policy(event.bp_type)
        return {
            "cancel": event.status == IN_PROGRESS
            and self.directory.user_in_any(user_id, policy.cancel, event.org_id, event.initiator_id),
            "rescind": event.status == SUCCESSFULLY_COMPLETED
            and self.directory.user_in_any(user_id, policy.rescind, event.org_id, event.initiator_id),
        }

    def definition_for(self, event: BusinessProcessEvent) -> BusinessProcessDefinition:
        return self._definition(event)

    # =====================================================================
    # Step execution
    # =====================================================================
    def _advance(self, event: BusinessProcessEvent) -> None:
        """Start the next group of steps whenever the current group is finished."""
        if event.id in self._advancing:
            return  # an outer call on the same event will continue the loop
        self._advancing.add(event.id)
        try:
            definition = self._definition(event)
            while event.status == IN_PROGRESS:
                if event.current_order is not None and self._group_has_open_steps(event, event.current_order):
                    return
                next_order = next((o for o in definition.ordered_groups() if event.current_order is None or o > event.current_order), None)
                if next_order is None:
                    self._complete_event(event)
                    return
                event.current_order = next_order
                for index, step_def in definition.steps_in_group(next_order):
                    if event.status != IN_PROGRESS:
                        break
                    self._start_step(event, index, step_def)
        finally:
            self._advancing.discard(event.id)

    def _start_step(self, event: BusinessProcessEvent, index: int, step_def: StepDefinition) -> None:
        now = self.clock.now()
        step = StepInstance(
            event=event,
            step_index=index,
            order=step_def.order,
            type=step_def.type,
            name=step_def.name,
            status=STEP_AWAITING,
            started_at=now,
            assignees=[],
            chain=[],
        )
        self.s.add(step)
        self.s.flush()
        context = self._event_context(event)

        if step_def.entry_condition is not None:
            try:
                required = is_true(step_def.entry_condition, context)
            except ConditionError as exc:
                required = True  # failing safe: keep the approval rather than skip it
                step.note = f"Entry condition could not be evaluated ({exc}); step kept"
            if not required:
                step.status = STEP_NOT_REQUIRED
                step.completed_at = now
                step.note = f"Entry condition not met: {' '.join(step_def.entry_condition.split())}"
                self._audit(event, "Step Not Required", None, step.name, step.note)
                return

        if step_def.type in ("approval", "to_do"):
            self._start_assigned_step(event, step, step_def)
        elif step_def.type == "approval_chain":
            self._start_approval_chain(event, step, step_def, context)
        elif step_def.type == "subprocess":
            self._start_subprocess(event, step, step_def)
        elif step_def.type == "integration":
            self._run_integration(event, step, step_def)
        else:  # pragma: no cover - initiation is handled separately
            raise InvalidState(f"Unexpected step type {step_def.type}")

    def _start_assigned_step(self, event: BusinessProcessEvent, step: StepInstance, step_def: StepDefinition) -> None:
        assignees, note = self._route(event, step_def)
        if note:
            step.note = note
        if not assignees:
            step.status = STEP_NOT_REQUIRED
            step.completed_at = self.clock.now()
            self._audit(event, "Step Not Required", None, step.name, note)
            return
        step.assignees = assignees
        for assignee in assignees:
            self._create_task(event, step, assignee, step_def.type, step_def.due_in_days)
        self._audit(event, "Awaiting Action", None, step.name, "Assigned to " + ", ".join(self._name(a) for a in assignees))

    def _start_approval_chain(
        self, event: BusinessProcessEvent, step: StepInstance, step_def: StepDefinition, context: dict[str, Any]
    ) -> None:
        prior = self._prior_approvers(event)
        approvers: list[str] = []
        skipped: list[str] = []
        for candidate in self.directory.management_chain(event.org_id):
            if self._excluded(candidate, event, step_def.routing_restrictions, prior):
                skipped.append(f"{self._name(candidate)} (routing restriction)")
                continue
            if step_def.while_running_condition:
                approver_context = {**context, "approver": self._worker_snapshot(self.s.get(Worker, candidate))}
                try:
                    needed = is_true(step_def.while_running_condition, approver_context)
                except ConditionError:
                    needed = True
                if not needed:
                    skipped.append(f"{self._name(candidate)} (while-running condition)")
                    continue
            approvers.append(candidate)
            if step_def.max_approvers and len(approvers) >= step_def.max_approvers:
                break

        step.note = ("Skipped: " + "; ".join(skipped)) if skipped else None
        if not approvers:
            step.status = STEP_NOT_REQUIRED
            step.completed_at = self.clock.now()
            self._audit(event, "Step Not Required", None, step.name, "No manager in the chain needs to approve")
            return
        step.chain = approvers
        step.chain_position = 0
        step.assignees = [approvers[0]]
        self._create_task(event, step, approvers[0], "approval_chain", step_def.due_in_days)
        self._audit(
            event,
            "Awaiting Action",
            None,
            step.name,
            "Approval chain: " + " -> ".join(self._name(a) for a in approvers),
        )

    def _start_subprocess(self, event: BusinessProcessEvent, step: StepInstance, step_def: StepDefinition) -> None:
        assert step_def.subprocess is not None
        snapshot = (event.subprocess_definitions or {}).get(step_def.subprocess)
        if snapshot is not None:
            child_definition = BusinessProcessDefinition.model_validate(snapshot["definition"])
            trace = [*snapshot["trace"], f"Definition captured when #{event.id} was initiated"]
        else:
            child_definition, trace = self.registry.resolve(
                step_def.subprocess, self.directory.org_chain_ids(event.org_id)
            )
        child = BusinessProcessEvent(
            bp_type=step_def.subprocess,
            definition_id=child_definition.id,
            definition=child_definition.model_dump(mode="json"),
            definition_trace=trace,
            subprocess_definitions=self._snapshot_subprocess_definitions(child_definition, event.org_id),
            org_id=event.org_id,
            subject_worker_id=event.subject_worker_id,
            subject_name=event.subject_name,
            initiator_id=event.initiator_id,
            effective_date=event.effective_date,
            data=dict(event.data),
            warnings=[],
            status=IN_PROGRESS,
            created_at=self.clock.now(),
            parent_event_id=event.id,
            parent_step_id=step.id,
        )
        self.s.add(child)
        self.s.flush()
        self._definition_cache[child.id] = child_definition
        step.status = STEP_IN_PROGRESS
        step.child_event_id = child.id
        step.note = f"Sub-process #{child.id}"
        self._audit(event, "Sub-process Started", None, step.name, f"#{child.id} {child_definition.definition_name}")
        self._audit(child, "Initiated", event.initiator_id, child_definition.steps[0].name, f"Sub-process of #{event.id}")
        self._complete_initiation(child, child_definition, event.initiator_id)
        self._advance(child)

    def _run_integration(self, event: BusinessProcessEvent, step: StepInstance, step_def: StepDefinition) -> None:
        root = self._root(event)
        record_type = BP_TYPES[root.bp_type].payroll_record
        message = OutboundMessage(
            event_id=root.id,
            integration=step_def.integration,
            change_type=record_type,
            worker_id=root.subject_worker_id or root.data.get("worker_id"),
            effective_date=root.effective_date,
            payload=self._payroll_payload(root, record_type, step_def.integration),
            created_at=self.clock.now(),
        )
        self.s.add(message)
        step.status = STEP_COMPLETED
        step.completed_at = self.clock.now()
        step.note = f"Sent {record_type} record to {step_def.integration}"
        self._audit(event, "Integration Sent", None, step.name, step.note)

    def _complete_initiation(self, event: BusinessProcessEvent, definition: BusinessProcessDefinition, actor_id: str) -> None:
        index, step_def = next((i, s) for i, s in enumerate(definition.steps) if s.type == "initiation")
        now = self.clock.now()
        self.s.add(
            StepInstance(
                event=event,
                step_index=index,
                order=step_def.order,
                type="initiation",
                name=step_def.name,
                status=STEP_COMPLETED,
                started_at=now,
                completed_at=now,
                completed_by=actor_id,
                assignees=[actor_id],
                chain=[],
            )
        )
        event.current_order = step_def.order
        self.s.flush()

    def _complete_event(self, event: BusinessProcessEvent) -> None:
        event.status = SUCCESSFULLY_COMPLETED
        event.completed_at = self.clock.now()
        if event.parent_event_id is None:
            self._apply_effects(event)
        self._audit(event, "Successfully Completed", None, None, None)

        if event.parent_event_id is not None:
            parent = self.s.get(BusinessProcessEvent, event.parent_event_id)
            parent_step = self.s.get(StepInstance, event.parent_step_id)
            if parent is not None and parent_step is not None and parent_step.active and parent_step.status == STEP_IN_PROGRESS:
                parent_step.status = STEP_COMPLETED
                parent_step.completed_at = self.clock.now()
                self._audit(parent, "Sub-process Completed", None, parent_step.name, f"#{event.id}")
                self._advance(parent)

    def _terminate(
        self, event: BusinessProcessEvent, status: str, actor_id: str | None, reason: str, propagate: bool = True
    ) -> None:
        now = self.clock.now()
        event.status = status
        event.completed_at = now
        for task in event.tasks:
            if task.status == TASK_OPEN:
                self._cancel_task(task, f"Business process {status.lower()}")
        for step in event.steps:
            if step.active and step.status in OPEN_STEP_STATUSES:
                step.status = STEP_CANCELED
                step.completed_at = now
        for child in self._children(event):
            if child.status == IN_PROGRESS:
                self._terminate(child, CANCELED, actor_id, f"Parent #{event.id} {status.lower()}", propagate=False)
        self._audit(event, status, actor_id, None, reason)

        if event.parent_event_id is None:
            self._send_reversal(event, "CANCEL", reason)
        elif propagate and status == DENIED:
            parent = self.s.get(BusinessProcessEvent, event.parent_event_id)
            parent_step = self.s.get(StepInstance, event.parent_step_id)
            if parent_step is not None and parent_step.status in OPEN_STEP_STATUSES:
                parent_step.status = STEP_DENIED
                parent_step.completed_at = now
            if parent is not None and parent.status == IN_PROGRESS:
                self._terminate(parent, DENIED, actor_id, f"Sub-process #{event.id} was denied")

    # =====================================================================
    # Effects on worker data
    # =====================================================================
    def _apply_effects(self, event: BusinessProcessEvent) -> None:
        now = self.clock.now()
        data = event.data
        if event.bp_type == "hire":
            worker = Worker(
                id=data["worker_id"],
                name=data["name"],
                email=data["email"],
                job_profile_id=data["job_profile_id"],
                org_id=data["org_id"],
                salary=data["salary"],
                hire_date=event.effective_date,
                status="Active",
            )
            self.s.add(worker)
            self.s.flush()
            event.subject_worker_id = worker.id
            for child in self._children(event):
                child.subject_worker_id = worker.id
            self.s.add(
                DataChange(event_id=event.id, worker_id=worker.id, change_type="create", before=None,
                           after=self._worker_snapshot(worker), applied_at=now)
            )
        elif event.bp_type == "change_job":
            worker = self._require_worker(event.subject_worker_id)
            before = {field: getattr(worker, field) for field in JOB_FIELDS}
            for field in JOB_FIELDS:
                setattr(worker, field, data[field])
            after = {field: getattr(worker, field) for field in JOB_FIELDS}
            self.s.add(DataChange(event_id=event.id, worker_id=worker.id, change_type="update",
                                  before=before, after=after, applied_at=now))
        elif event.bp_type == "termination":
            worker = self._require_worker(event.subject_worker_id)
            before = {"status": worker.status, "termination_date": worker.termination_date}
            worker.status = "Terminated"
            worker.termination_date = event.effective_date
            after = {"status": worker.status, "termination_date": worker.termination_date}
            self.s.add(DataChange(event_id=event.id, worker_id=worker.id, change_type="update",
                                  before=before, after=after, applied_at=now))
            self._reroute_open_tasks(worker.id)

    def _delete_hired_worker(self, worker: Worker) -> None:
        references = [
            self.s.scalar(select(Task.id).where(Task.assignee_id == worker.id).limit(1)),
            self.s.scalar(select(BusinessProcessEvent.id).where(BusinessProcessEvent.initiator_id == worker.id).limit(1)),
            self.s.scalar(select(RoleAssignment.id).where(RoleAssignment.worker_id == worker.id).limit(1)),
            self.s.scalar(select(GroupMembership.id).where(GroupMembership.worker_id == worker.id).limit(1)),
        ]
        if any(ref is not None for ref in references):
            raise InvalidState(f"{worker.name} is referenced by tasks, roles or other events and cannot be removed")
        for event in self.s.scalars(
            select(BusinessProcessEvent).where(BusinessProcessEvent.subject_worker_id == worker.id)
        ):
            event.subject_worker_id = None
        self.s.flush()
        self.s.delete(worker)

    # =====================================================================
    # Payroll integration (simulated, PECI-inspired)
    # =====================================================================
    def _payroll_payload(self, event: BusinessProcessEvent, record_type: str, interface: str | None) -> dict[str, Any]:
        data = event.data
        worker = self.s.get(Worker, event.subject_worker_id) if event.subject_worker_id else None
        payload: dict[str, Any] = {
            "interface": interface,
            "record_type": record_type,
            "event_id": event.id,
            "worker_id": worker.id if worker else data.get("worker_id"),
            "worker_name": event.subject_name,
            "effective_date": event.effective_date,
        }
        if record_type == "HIRE":
            payload["fields"] = {
                "job_profile_id": data["job_profile_id"],
                "org_id": data["org_id"],
                "annual_salary": data["salary"],
                "hire_date": event.effective_date,
            }
        elif record_type == "JOB_CHANGE" and worker is not None:
            payload["fields"] = {
                field: {"from": getattr(worker, field), "to": data[field]}
                for field in JOB_FIELDS
                if getattr(worker, field) != data[field]
            }
        elif record_type == "TERMINATION":
            payload["fields"] = {
                "status": {"from": "Active", "to": "Terminated"},
                "termination_date": event.effective_date,
                "reason": data.get("reason"),
            }
        return payload

    def _send_reversal(self, event: BusinessProcessEvent, record_type: str, reason: str | None) -> None:
        sent = self.s.scalars(
            select(OutboundMessage).where(
                OutboundMessage.event_id == event.id, OutboundMessage.change_type == BP_TYPES[event.bp_type].payroll_record
            )
        ).all()
        if not sent:
            return
        self.s.add(
            OutboundMessage(
                event_id=event.id,
                integration=sent[0].integration,
                change_type=record_type,
                worker_id=sent[0].worker_id,
                effective_date=event.effective_date,
                payload={
                    "interface": sent[0].integration,
                    "record_type": record_type,
                    "event_id": event.id,
                    "reverses_record_ids": [m.id for m in sent],
                    "reason": reason,
                },
                created_at=self.clock.now(),
            )
        )

    # =====================================================================
    # Routing helpers
    # =====================================================================
    def _route(self, event: BusinessProcessEvent, step_def: StepDefinition) -> tuple[list[str], str | None]:
        prior = self._prior_approvers(event)

        def eligible(candidates: Iterable[str]) -> list[str]:
            return [c for c in candidates if not self._excluded(c, event, step_def.routing_restrictions, prior)]

        groups = ", ".join(step_def.groups)
        candidates = self.directory.resolve_assignees(step_def.groups, event.org_id)
        primary = eligible(candidates)
        if primary:
            return primary, None
        why = "excluded by routing restrictions" if candidates else "has no active members"
        if step_def.alternate_groups:
            alternate = eligible(self.directory.resolve_assignees(step_def.alternate_groups, event.org_id))
            if alternate:
                return alternate, f"{groups} {why}; routed to alternate group {', '.join(step_def.alternate_groups)}"
        if candidates:
            # Workday: if routing restrictions exclude all assigned workers, the step is skipped.
            return [], f"Every {groups} member was excluded by routing restrictions; step skipped"
        fallback = eligible(self.directory.group_members("Business Process Administrator"))
        if fallback:
            return fallback, f"Nobody holds {groups}; routed to Business Process Administrator"
        return [], f"Nobody holds {groups} and no Business Process Administrator is available; step skipped"

    def _excluded(
        self, candidate: str, event: BusinessProcessEvent, restrictions: list[str], prior_approvers: set[str]
    ) -> bool:
        root = self._root(event)
        if "exclude_initiator" in restrictions and candidate == event.initiator_id:
            return True
        if "exclude_event_subject" in restrictions and candidate in {event.subject_worker_id, root.subject_worker_id}:
            return True
        if "exclude_prior_approvers" in restrictions and candidate in prior_approvers:
            return True
        return False

    def _prior_approvers(self, event: BusinessProcessEvent) -> set[str]:
        return {
            task.assignee_id
            for member in self._family(self._root(event))
            for task in member.tasks
            if task.outcome == "Approved"
        }

    # =====================================================================
    # Small helpers
    # =====================================================================
    def _touch(self, event: BusinessProcessEvent) -> None:
        """Mark the top-level event as changed so its version number is checked.

        Without this, two people approving *different* parallel steps at the
        same moment would each see the other's step as still open, neither
        request would advance the process, and it would stall.
        """
        root = self._root(event)
        root.updated_at = self.clock.now()
        flag_modified(root, "updated_at")

    def _advance_chain(self, step: StepInstance) -> str | None:
        """Move an approval chain to its next active approver; None when finished."""
        step.chain_position += 1
        while step.chain_position < len(step.chain):
            candidate = step.chain[step.chain_position]
            if self.directory.is_active(candidate):
                step.assignees = [*step.assignees, candidate]
                return candidate
            step.chain_position += 1
        return None

    def _reroute_open_tasks(self, worker_id: str) -> None:
        """Give a departing worker's open tasks to whoever the routing picks now."""
        name = self._name(worker_id)
        open_tasks = self.s.scalars(
            select(Task).where(Task.assignee_id == worker_id, Task.status == TASK_OPEN).order_by(Task.id)
        ).all()
        for task in open_tasks:
            event, step = task.event, task.step
            self._cancel_task(task, f"{name} is no longer an active worker")
            if event.status != IN_PROGRESS or not step.active or step.status not in OPEN_STEP_STATUSES:
                continue
            self._touch(event)
            step_def = self._definition(event).steps[step.step_index]

            if step.type == "approval_chain":
                next_approver = self._advance_chain(step)
                if next_approver is not None:
                    self._create_task(event, step, next_approver, "approval_chain", step_def.due_in_days)
                    self._audit(event, "Reassigned", None, step.name, f"{name} left; next approver: {self._name(next_approver)}")
                else:
                    self._finish_step(step, None)
                    self._audit(event, "Step Completed", None, step.name, f"{name} left; no approvers remain in the chain")
                    self._advance(event)
                continue

            step_tasks = self._step_tasks(step)
            if any(t.status == TASK_OPEN for t in step_tasks):
                continue  # another member of the group can still act
            if task.kind == "initiation":
                assignees = self.directory.group_members("Business Process Administrator")
            else:
                assignees, _note = self._route(event, step_def)
            already_done = {t.assignee_id for t in step_tasks if t.status == TASK_COMPLETED}
            assignees = [a for a in assignees if a != worker_id and a not in already_done]
            if not assignees:
                if already_done:
                    self._finish_step(step, None)
                else:
                    step.status = STEP_NOT_REQUIRED
                    step.completed_at = self.clock.now()
                self._audit(event, "Step Completed", None, step.name, f"{name} left; nobody else to route to")
                self._advance(event)
                continue
            step.assignees = assignees
            for assignee in assignees:
                self._create_task(event, step, assignee, task.kind, step_def.due_in_days)
            self._audit(
                event, "Reassigned", None, step.name,
                f"{name} left; assigned to " + ", ".join(self._name(a) for a in assignees),
            )

    def _validate_reason(self, bpt: BPType, data: dict[str, Any]) -> None:
        if bpt.reasons and data.get("reason") not in bpt.reasons:
            raise ValidationFailed(
                f"Reason must be one of: {', '.join(bpt.reasons)}", ["reason is invalid"]
            )

    def _snapshot_subprocess_definitions(
        self, definition: BusinessProcessDefinition, org_id: str
    ) -> dict[str, Any]:
        snapshots: dict[str, Any] = {}
        for step in definition.steps:
            if step.subprocess and step.subprocess not in snapshots:
                child, trace = self.registry.resolve(step.subprocess, self.directory.org_chain_ids(org_id))
                snapshots[step.subprocess] = {"definition": child.model_dump(mode="json"), "trace": trace}
        return snapshots

    def _definition(self, event: BusinessProcessEvent) -> BusinessProcessDefinition:
        cached = self._definition_cache.get(event.id)
        if cached is None:
            cached = BusinessProcessDefinition.model_validate(event.definition)
            self._definition_cache[event.id] = cached
        return cached

    def _event_context(self, event: BusinessProcessEvent) -> dict[str, Any]:
        return self._context(event.data, event.subject_worker_id, event.initiator_id)

    def _context(self, data: dict[str, Any], subject_id: str | None, initiator_id: str) -> dict[str, Any]:
        worker = self._worker_snapshot(self.s.get(Worker, subject_id)) if subject_id else None
        job_profile = self._job_profile_snapshot(data.get("job_profile_id"))
        current_job_profile = self._job_profile_snapshot(worker["job_profile_id"]) if worker else None
        pay_change_pct = None
        if worker and worker["salary"] and data.get("salary") is not None:
            pay_change_pct = round((data["salary"] - worker["salary"]) / worker["salary"] * 100, 2)
        return {
            "data": dict(data),
            "worker": worker,
            "job_profile": job_profile,
            "current_job_profile": current_job_profile,
            "pay_change_pct": pay_change_pct,
            "initiator": self._worker_snapshot(self.s.get(Worker, initiator_id)),
            "approver": None,
        }

    def _run_validations(
        self, definition: BusinessProcessDefinition, data: dict[str, Any], subject_id: str | None, initiator_id: str
    ) -> list[str]:
        context = self._context(data, subject_id, initiator_id)
        errors: list[str] = []
        warnings: list[str] = []
        for rule in definition.validations:
            try:
                fired = is_true(rule.error_when, context)
            except ConditionError as exc:
                errors.append(f"{rule.name}: rule could not be evaluated ({exc})")
                continue
            if fired:
                (errors if rule.severity == "critical" else warnings).append(rule.message)
        if errors:
            raise ValidationFailed(" ".join(errors), errors)
        return warnings

    def _worker_snapshot(self, worker: Worker | None) -> dict[str, Any] | None:
        if worker is None:
            return None
        job_profile = self.s.get(JobProfile, worker.job_profile_id)
        return {
            "id": worker.id,
            "name": worker.name,
            "email": worker.email,
            "job_profile_id": worker.job_profile_id,
            "org_id": worker.org_id,
            "salary": worker.salary,
            "hire_date": worker.hire_date,
            "status": worker.status,
            "termination_date": worker.termination_date,
            "management_level": job_profile.management_level if job_profile else None,
        }

    def _job_profile_snapshot(self, job_profile_id: str | None) -> dict[str, Any] | None:
        job_profile = self.s.get(JobProfile, job_profile_id) if job_profile_id else None
        if job_profile is None:
            return None
        return {
            "id": job_profile.id,
            "name": job_profile.name,
            "management_level": job_profile.management_level,
            "pay_range_min": job_profile.pay_range_min,
            "pay_range_mid": job_profile.pay_range_mid,
            "pay_range_max": job_profile.pay_range_max,
        }

    def _create_task(
        self, event: BusinessProcessEvent, step: StepInstance, assignee_id: str, kind: str, due_in_days: int | None
    ) -> Task:
        due = (self.clock.today() + timedelta(days=due_in_days)).isoformat() if due_in_days is not None else None
        task = Task(
            event=event,
            step_id=step.id,
            assignee_id=assignee_id,
            kind=kind,
            status=TASK_OPEN,
            created_at=self.clock.now(),
            due_date=due,
        )
        self.s.add(task)
        self.s.flush()
        return task

    def _open_task(self, task_id: int, user_id: str, kinds: set[str], action: str) -> Task:
        task = self.s.get(Task, task_id)
        if task is None:
            raise NotFound("Task not found")
        if task.assignee_id != user_id:
            raise PermissionDenied("This task is assigned to someone else")
        if task.status != TASK_OPEN:
            raise InvalidState(f"This task is already {task.status.lower()}")
        if task.event.status != IN_PROGRESS:
            raise InvalidState(f"The business process is {task.event.status}")
        if task.kind not in kinds:
            raise InvalidState(f"{action} is not available on a {task.kind.replace('_', ' ')} step")
        self._touch(task.event)
        return task

    def _close_task(self, task: Task, outcome: str, comment: str | None) -> None:
        task.status = TASK_COMPLETED
        task.outcome = outcome
        task.comment = comment
        task.completed_at = self.clock.now()

    def _cancel_task(self, task: Task, reason: str) -> None:
        task.status = TASK_CANCELED
        task.comment = reason
        task.completed_at = self.clock.now()

    def _finish_step(self, step: StepInstance, user_id: str | None) -> None:
        step.status = STEP_COMPLETED
        step.completed_at = self.clock.now()
        step.completed_by = user_id

    def _step_tasks(self, step: StepInstance) -> list[Task]:
        return list(self.s.scalars(select(Task).where(Task.step_id == step.id).order_by(Task.id)))

    def _group_has_open_steps(self, event: BusinessProcessEvent, order: str) -> bool:
        return any(s.active and s.order == order and s.status in OPEN_STEP_STATUSES for s in event.steps)

    def _children(self, event: BusinessProcessEvent) -> list[BusinessProcessEvent]:
        return list(
            self.s.scalars(select(BusinessProcessEvent).where(BusinessProcessEvent.parent_event_id == event.id))
        )

    def _root(self, event: BusinessProcessEvent) -> BusinessProcessEvent:
        current = event
        while current.parent_event_id is not None:
            parent = self.s.get(BusinessProcessEvent, current.parent_event_id)
            if parent is None:
                break
            current = parent
        return current

    def _family(self, root: BusinessProcessEvent) -> list[BusinessProcessEvent]:
        family = [root]
        index = 0
        while index < len(family):
            family.extend(self._children(family[index]))
            index += 1
        return family

    def _get_event(self, event_id: int) -> BusinessProcessEvent:
        event = self.s.get(BusinessProcessEvent, event_id)
        if event is None:
            raise NotFound("Business process not found")
        return event

    def _get_visible_event(self, event_id: int, user_id: str) -> BusinessProcessEvent:
        event = self._get_event(event_id)
        if not self.can_view(event, user_id):
            raise PermissionDenied("You do not have access to this business process")
        return event

    def _require_worker(self, worker_id: str | None) -> Worker:
        worker = self.s.get(Worker, worker_id) if worker_id else None
        if worker is None:
            raise NotFound("Worker not found")
        return worker

    def _require_active_worker(self, worker_id: str) -> Worker:
        worker = self._require_worker(worker_id)
        if worker.status != "Active":
            raise PermissionDenied(f"{worker.name} is not an active worker")
        return worker

    def _require_org(self, org_id: str | None) -> SupervisoryOrg:
        org = self.s.get(SupervisoryOrg, org_id) if org_id else None
        if org is None:
            raise ValidationFailed(f"Unknown supervisory organization {org_id!r}", ["org_id is invalid"])
        return org

    def _require_job_profile(self, job_profile_id: str | None) -> JobProfile:
        job_profile = self.s.get(JobProfile, job_profile_id) if job_profile_id else None
        if job_profile is None:
            raise ValidationFailed(f"Unknown job profile {job_profile_id!r}", ["job_profile_id is invalid"])
        return job_profile

    def _next_worker_id(self) -> str:
        sequence = self.s.get(IdSequence, "worker")
        if sequence is None:
            sequence = IdSequence(name="worker", next_value=2001)
            self.s.add(sequence)
        while True:
            candidate = f"W{sequence.next_value}"
            sequence.next_value += 1
            if self.s.get(Worker, candidate) is None:
                return candidate

    def _name(self, worker_id: str | None) -> str:
        if worker_id is None:
            return "System"
        worker = self.s.get(Worker, worker_id)
        return worker.name if worker else worker_id

    def _audit(
        self,
        event: BusinessProcessEvent,
        action: str,
        actor_id: str | None,
        step_name: str | None,
        detail: str | None,
    ) -> None:
        self.s.add(
            AuditEntry(
                event=event,
                at=self.clock.now(),
                actor_id=actor_id,
                action=action,
                step_name=step_name,
                detail=detail,
            )
        )
