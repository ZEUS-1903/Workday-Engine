"""REST API.

Identity is simulated with an ``X-User-Id`` header so you can switch between
people in the demo. A real system would use OAuth 2.0 / SSO instead; never
ship header-based identity to production.
"""

from __future__ import annotations

from typing import Annotated, Iterator

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import Base
from ..engine.engine import BP_TYPES, BPEngine, NotFound, PermissionDenied
from ..engine.routing import Directory
from ..models import BusinessProcessEvent, JobProfile, OutboundMessage, RoleAssignment, SupervisoryOrg, Worker
from ..seed import MANAGEMENT_LEVELS, seed
from .schemas import ChangeJobRequest, CommentBody, HireRequest, SendBackBody, SubmitBody, TerminationRequest
from .serializers import Lookup, event_detail, event_summary, outbound_dict, task_dict, worker_dict

router = APIRouter(prefix="/api")


# ---- dependencies ---------------------------------------------------------
def get_session(request: Request) -> Iterator[Session]:
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()  # anything not committed explicitly is rolled back


SessionDep = Annotated[Session, Depends(get_session)]


def get_engine(request: Request, session: SessionDep) -> BPEngine:
    return BPEngine(session, request.app.state.registry, request.app.state.clock)


EngineDep = Annotated[BPEngine, Depends(get_engine)]


def current_user(
    session: SessionDep,
    x_user_id: Annotated[str | None, Header(description="Demo identity: the worker ID you act as")] = None,
) -> Worker:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Missing X-User-Id header")
    worker = session.get(Worker, x_user_id)
    if worker is None or worker.status != "Active":
        raise HTTPException(status_code=401, detail="Unknown or inactive user")
    return worker


UserDep = Annotated[Worker, Depends(current_user)]


def _visible_event(engine: BPEngine, event_id: int, user: Worker) -> BusinessProcessEvent:
    event = engine.s.get(BusinessProcessEvent, event_id)
    if event is None:
        raise NotFound("Business process not found")
    if not engine.can_view(event, user.id):
        raise PermissionDenied("You do not have access to this business process")
    return event


def _detail_after_commit(engine: BPEngine, event: BusinessProcessEvent, user: Worker) -> dict:
    engine.s.commit()
    return event_detail(engine, event, user.id, Lookup(engine.s))


# ---- reference data -------------------------------------------------------
@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/users", summary="People you can act as in the demo")
def list_users(session: SessionDep) -> list[dict]:
    directory = Directory(session)
    lookup = Lookup(session)
    users = []
    for worker in session.scalars(select(Worker).where(Worker.status == "Active").order_by(Worker.id)):
        users.append(
            {
                "id": worker.id,
                "name": worker.name,
                "title": lookup.job_profiles.get(worker.job_profile_id),
                **directory.security_summary(worker.id),
            }
        )
    return users


@router.get("/me")
def me(user: UserDep, session: SessionDep) -> dict:
    lookup = Lookup(session)
    return {**worker_dict(user, lookup), **Directory(session).security_summary(user.id)}


@router.get("/workers")
def list_workers(user: UserDep, session: SessionDep, include_terminated: bool = False) -> list[dict]:
    lookup = Lookup(session)
    directory = Directory(session)
    query = select(Worker).order_by(Worker.id)
    if not include_terminated:
        query = query.where(Worker.status == "Active")
    return [
        worker_dict(w, lookup, show_compensation=directory.can_see_compensation(user.id, w))
        for w in session.scalars(query)
    ]


@router.get("/job-profiles")
def list_job_profiles(user: UserDep, session: SessionDep) -> list[dict]:
    return [
        {
            "id": j.id,
            "name": j.name,
            "management_level": j.management_level,
            "management_level_name": MANAGEMENT_LEVELS[j.management_level],
            "pay_range": {"min": j.pay_range_min, "mid": j.pay_range_mid, "max": j.pay_range_max},
        }
        for j in session.scalars(select(JobProfile).order_by(JobProfile.management_level, JobProfile.name))
    ]


@router.get("/orgs", summary="Supervisory organizations with role assignments")
def list_orgs(user: UserDep, session: SessionDep) -> list[dict]:
    lookup = Lookup(session)
    orgs = []
    for org in session.scalars(select(SupervisoryOrg).order_by(SupervisoryOrg.id)):
        assignments = session.scalars(select(RoleAssignment).where(RoleAssignment.org_id == org.id)).all()
        members = session.scalars(
            select(Worker).where(Worker.org_id == org.id, Worker.status == "Active").order_by(Worker.id)
        ).all()
        orgs.append(
            {
                "id": org.id,
                "name": org.name,
                "superior_id": org.superior_id,
                "roles": [{"role": a.role, "worker": lookup.person(a.worker_id)} for a in assignments],
                "members": [lookup.person(m.id) for m in members],
            }
        )
    return orgs


# ---- definitions ----------------------------------------------------------
@router.get("/business-processes", summary="Business process types, policies and definitions")
def list_business_processes(user: UserDep, request: Request) -> list[dict]:
    registry = request.app.state.registry
    result = []
    for bp_type in registry.bp_types():
        policy = registry.policy(bp_type)
        result.append(
            {
                "bp_type": bp_type,
                "label": BP_TYPES[bp_type].label,
                "subprocess_only": BP_TYPES[bp_type].subprocess_only,
                "security_policy": policy.model_dump(),
                "definitions": [
                    {"id": d.id, "definition_name": d.definition_name, "org_id": d.org_id}
                    for d in registry.definitions_for(bp_type)
                ],
            }
        )
    return result


@router.get("/business-processes/{bp_type}/definition", summary="Which definition applies to an organization")
def resolve_definition(bp_type: str, org_id: str, user: UserDep, request: Request, session: SessionDep) -> dict:
    registry = request.app.state.registry
    if bp_type not in registry.bp_types():
        raise NotFound(f"Unknown business process type {bp_type!r}")
    directory = Directory(session)
    if session.get(SupervisoryOrg, org_id) is None:
        raise NotFound("Unknown organization")
    definition, trace = registry.resolve(bp_type, directory.org_chain_ids(org_id))
    policy = registry.policy(bp_type)
    return {
        "definition": definition.model_dump(mode="json"),
        "trace": trace,
        "security_policy": policy.model_dump(),
        "you_can_initiate": directory.user_in_any(user.id, policy.initiate, org_id),
    }


# ---- initiating business processes ----------------------------------------
@router.post("/events/hire", status_code=201, summary="Initiate a Hire")
def initiate_hire(body: HireRequest, user: UserDep, engine: EngineDep) -> dict:
    data = body.model_dump(exclude={"effective_date"})
    event = engine.initiate("hire", user.id, body.effective_date.isoformat(), data)
    return _detail_after_commit(engine, event, user)


@router.post("/events/change-job", status_code=201, summary="Initiate a Change Job")
def initiate_change_job(body: ChangeJobRequest, user: UserDep, engine: EngineDep) -> dict:
    data = body.model_dump(exclude={"effective_date", "worker_id"})
    event = engine.initiate("change_job", user.id, body.effective_date.isoformat(), data, body.worker_id)
    return _detail_after_commit(engine, event, user)


@router.post("/events/termination", status_code=201, summary="Initiate a Termination")
def initiate_termination(body: TerminationRequest, user: UserDep, engine: EngineDep) -> dict:
    data = body.model_dump(exclude={"effective_date", "worker_id"})
    event = engine.initiate("termination", user.id, body.effective_date.isoformat(), data, body.worker_id)
    return _detail_after_commit(engine, event, user)


# ---- events ----------------------------------------------------------------
@router.get("/events", summary="Business processes you can see")
def list_events(
    user: UserDep,
    engine: EngineDep,
    status: str | None = None,
    bp_type: str | None = None,
    include_subprocesses: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    query = select(BusinessProcessEvent).order_by(BusinessProcessEvent.id.desc())
    if status:
        query = query.where(BusinessProcessEvent.status == status)
    if bp_type:
        query = query.where(BusinessProcessEvent.bp_type == bp_type)
    if not include_subprocesses:
        query = query.where(BusinessProcessEvent.parent_event_id.is_(None))
    lookup = Lookup(engine.s)
    visible = [e for e in engine.s.scalars(query) if engine.can_view(e, user.id)]
    return [event_summary(e, lookup) for e in visible[:limit]]


@router.get("/events/{event_id}")
def get_event(event_id: int, user: UserDep, engine: EngineDep) -> dict:
    event = _visible_event(engine, event_id, user)
    return event_detail(engine, event, user.id, Lookup(engine.s))


@router.post("/events/{event_id}/cancel")
def cancel_event(event_id: int, body: CommentBody, user: UserDep, engine: EngineDep) -> dict:
    event = engine.cancel(event_id, user.id, body.comment)
    return _detail_after_commit(engine, event, user)


@router.post("/events/{event_id}/rescind")
def rescind_event(event_id: int, body: CommentBody, user: UserDep, engine: EngineDep) -> dict:
    event = engine.rescind(event_id, user.id, body.comment)
    return _detail_after_commit(engine, event, user)


# ---- My Tasks ----------------------------------------------------------------
@router.get("/tasks", summary="My Tasks inbox")
def my_tasks(user: UserDep, engine: EngineDep) -> list[dict]:
    lookup = Lookup(engine.s)
    return [task_dict(engine, t, lookup) for t in engine.inbox(user.id)]


@router.post("/tasks/{task_id}/approve")
def approve_task(task_id: int, body: CommentBody, user: UserDep, engine: EngineDep) -> dict:
    event = engine.approve(task_id, user.id, body.comment)
    return _detail_after_commit(engine, event, user)


@router.post("/tasks/{task_id}/deny")
def deny_task(task_id: int, body: CommentBody, user: UserDep, engine: EngineDep) -> dict:
    event = engine.deny(task_id, user.id, body.comment)
    return _detail_after_commit(engine, event, user)


@router.post("/tasks/{task_id}/send-back")
def send_back_task(task_id: int, body: SendBackBody, user: UserDep, engine: EngineDep) -> dict:
    event = engine.send_back(task_id, user.id, body.to_step_id, body.comment)
    return _detail_after_commit(engine, event, user)


@router.post("/tasks/{task_id}/submit", summary="Complete a To Do or resubmit a sent-back initiation")
def submit_task(task_id: int, body: SubmitBody, user: UserDep, engine: EngineDep) -> dict:
    data = body.data.model_dump(exclude_none=True, mode="json") if body.data else None
    event = engine.submit(task_id, user.id, data, body.comment)
    return _detail_after_commit(engine, event, user)


# ---- integrations & admin ---------------------------------------------------
@router.get("/integrations/outbound", summary="Records sent by Integration steps")
def outbound_messages(user: UserDep, engine: EngineDep) -> list[dict]:
    if not engine.directory.user_in_any(
        user.id, ["HR Administrator", "Business Process Administrator", "Compensation Partner"], "ORG-100"
    ):
        raise PermissionDenied("Only HR Administrators, BP Administrators and Compensation Partners can view payroll records")
    messages = engine.s.scalars(select(OutboundMessage).order_by(OutboundMessage.id.desc())).all()
    return [outbound_dict(m) for m in messages]


@router.post("/admin/reset", summary="Reset the demo tenant to its seed data")
def reset_demo(user: UserDep, request: Request, engine: EngineDep) -> dict:
    if not engine.directory.user_in_any(user.id, ["HR Administrator", "Business Process Administrator"], "ORG-100"):
        raise PermissionDenied("Only administrators can reset the demo tenant")
    engine.s.close()
    db_engine = request.app.state.db_engine
    Base.metadata.drop_all(db_engine)
    Base.metadata.create_all(db_engine)
    with request.app.state.session_factory() as session:
        seed(session)
        session.commit()
    return {"status": "reset"}
