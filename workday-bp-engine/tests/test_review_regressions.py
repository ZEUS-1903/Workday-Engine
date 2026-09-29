"""Regression tests for issues found in an independent code review."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm.exc import StaleDataError

from app.engine.conditions import ConditionError, evaluate
from app.engine.engine import BPEngine
from app.models import BusinessProcessEvent, Task
from conftest import BEN, CHEN, DANIEL, GRACE, LENA, MARIA, RAJ, SOFIA, TOM, step_status
from test_definition_changes import _edited_registry


def _load_everything(session, event_id):
    """Read the whole event family so a session holds a (soon stale) snapshot."""
    loaded = []
    event = session.get(BusinessProcessEvent, event_id)
    loaded.append(event)
    loaded += list(event.steps) + list(event.tasks)
    for child in session.scalars(select(BusinessProcessEvent).where(BusinessProcessEvent.parent_event_id == event_id)):
        loaded += [child, *child.steps, *child.tasks]
    return loaded


def test_parallel_approvals_at_the_same_moment_cannot_stall_the_process(app, api, clock):
    # Group "b" = HR Partner Approval (Lena) in parallel with a sub-process (Chen)
    event = api.hire(salary=130_000)
    lena_task = api.task(LENA, "HR Partner Approval")["id"]
    chen_task = api.task(CHEN, "Compensation Partner Approval")["id"]

    first, second = app.state.session_factory(), app.state.session_factory()
    try:
        keep_alive = [_load_everything(first, event["id"]), _load_everything(second, event["id"])]
        BPEngine(first, app.state.registry, clock).approve(lena_task, LENA)
        first.commit()
        # Without the top-level version bump, this would commit and the process
        # would sit In Progress forever with every step completed.
        with pytest.raises(StaleDataError):
            BPEngine(second, app.state.registry, clock).approve(chen_task, CHEN)
            second.commit()
        second.rollback()
        del keep_alive
    finally:
        first.close()
        second.close()

    # Chen simply retries and the process moves on to the approval chain
    api.approve(CHEN, "Compensation Partner Approval")
    assert api.task(RAJ, "Engineering Management Chain Approval")


def test_send_back_from_a_subprocess_goes_to_the_parent_initiation(api):
    event = api.hire(salary=130_000)
    task = api.task(CHEN, "Compensation Partner Approval")
    targets = task["send_back_targets"]
    # The sub-process's own system-completed initiation is not offered
    assert [(t["name"], t["completed_by"]["id"]) for t in targets] == [("Hire", MARIA)]

    parent = api.post(
        f"/api/tasks/{task['id']}/send-back", CHEN, {"comment": "Offer is too high", "to_step_id": targets[0]["id"]}
    )
    assert parent["id"] == event["id"]
    child = api.event(event["children"][0]["id"])
    assert child["status"] == "Canceled"
    assert step_status(child, "Compensation Partner Approval") == "Sent Back"
    assert api.inbox(LENA) == []  # the parallel HR step was superseded too

    parent = api.submit(MARIA, "Hire", data={"salary": 115_000}, comment="Lowered")
    new_child = api.event(parent["children"][-1]["id"])
    assert new_child["status"] == "Successfully Completed"  # below the midpoint now
    assert api.task(LENA, "HR Partner Approval")


def test_terminated_approver_tasks_are_rerouted(api):
    hire = api.hire(salary=110_000)
    assert api.task(LENA, "HR Partner Approval")

    termination = api.terminate(LENA, user=DANIEL)
    api.approve(BEN, "HR Partner Approval")
    api.submit(DANIEL, "Offboarding Checklist")
    assert api.event(termination["id"])["status"] == "Successfully Completed"

    # Engineering's HR Partner (Lena) is gone, so the role is inherited from the top org
    hire = api.event(hire["id"])
    step = next(s for s in hire["steps"] if s["name"] == "HR Partner Approval" and s["active"])
    assert [p["id"] for p in step["assignees"]] == [BEN]
    assert any(h["action"] == "Reassigned" for h in hire["history"])
    api.approve(BEN, "HR Partner Approval")
    assert api.task(RAJ, "Engineering Management Chain Approval")


def test_terminated_chain_approver_is_skipped(api):
    hire = api.hire(salary=110_000)
    api.approve(LENA, "HR Partner Approval")
    assert api.task(RAJ, "Engineering Management Chain Approval")

    termination = api.terminate(RAJ, user=BEN)  # Ben is HR Partner on the top org
    api.approve(SOFIA, "HR Partner Approval")  # Ben is the initiator -> alternate group
    api.submit(GRACE, "Offboarding Checklist")  # Grace manages Raj's org
    assert api.event(termination["id"])["status"] == "Successfully Completed"

    hire = api.event(hire["id"])
    assert step_status(hire, "Engineering Management Chain Approval") == "Completed"
    assert api.task(MARIA, "Prepare Onboarding")


def test_resubmit_cannot_bypass_the_reason_list(api):
    api.change_job(TOM, salary=124_000, user=MARIA)
    lena_task = api.task(LENA, "HR Partner Approval")
    target = lena_task["send_back_targets"][0]
    api.post(f"/api/tasks/{lena_task['id']}/send-back", LENA, {"comment": "Check reason", "to_step_id": target["id"]})
    resubmit = api.task(MARIA, "Change Job")
    body = api.post(f"/api/tasks/{resubmit['id']}/submit", MARIA, {"data": {"reason": "anything at all"}}, expect=422)
    assert "Reason must be one of" in body["detail"]
    api.post(f"/api/tasks/{resubmit['id']}/submit", MARIA, {"data": {"reason": "Market Adjustment"}})


def test_restrictions_excluding_everyone_skip_the_step(app, api, tmp_path):
    def drop_alternate(body):
        body["steps"][1]["alternate_groups"] = []

    app.state.registry = _edited_registry(tmp_path, "termination_default.yaml", drop_alternate)
    # Lena initiates; she is the only HR Partner for Tom's org and is excluded
    event = api.terminate(TOM, user=LENA)
    step = next(s for s in event["steps"] if s["name"] == "HR Partner Approval")
    assert step["status"] == "Not Required"
    assert "excluded by routing restrictions" in step["note"]
    assert step_status(event, "Offboarding Checklist") == "Awaiting Action"


@pytest.mark.parametrize("expression", ["'x' * 3 == 'xxx'", "[1] * 3 == [1, 1, 1]", "'%s' % 1 == '1'", "-data.reason"])
def test_arithmetic_is_numbers_only(expression):
    with pytest.raises(ConditionError):
        evaluate(expression, {"data": {"reason": "Promotion"}})


def test_overflow_becomes_a_condition_error():
    with pytest.raises(ConditionError):
        evaluate("9" * 400 + " / 1 > 0", {})
