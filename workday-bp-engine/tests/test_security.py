from sqlalchemy.orm.exc import StaleDataError
import pytest

from app.engine.engine import BPEngine, InvalidState
from app.models import Task
from conftest import ALICE, BEN, CHEN, DANIEL, KEN, LENA, MARIA, PRIYA, RAJ, TOM


def test_missing_or_unknown_user_is_rejected(client):
    assert client.get("/api/tasks").status_code == 401
    assert client.get("/api/tasks", headers={"X-User-Id": "W9999"}).status_code == 401


def test_only_manager_or_hr_partner_can_initiate(api):
    body = api.hire(user=ALICE, expect=403)
    assert "Manager, HR Partner" in body["detail"]
    # Ken manages Data Team, not Platform Team
    api.hire(user=KEN, expect=403)
    # Raj manages Engineering; role access covers subordinate organizations
    api.hire(user=RAJ, org_id="ORG-210", expect=201)
    # Ben is HR Partner on the top-level org, so he can hire anywhere
    api.hire(user=BEN, org_id="ORG-400", job_profile_id="JP-CA", email="x@y.example", expect=201)


def test_cannot_start_a_process_on_yourself(api):
    body = api.change_job(MARIA, user=MARIA, salary=190_000, expect=403)
    assert "yourself" in body["detail"]


def test_cannot_act_on_someone_elses_task(api):
    api.hire()
    task = api.task(LENA, "HR Partner Approval")
    api.post(f"/api/tasks/{task['id']}/approve", CHEN, expect=403)
    api.post(f"/api/tasks/{task['id']}/approve", LENA)
    api.post(f"/api/tasks/{task['id']}/approve", LENA, expect=409)  # already completed


def test_wrong_action_for_step_type(api):
    api.hire(salary=110_000)
    api.approve(LENA, "HR Partner Approval")
    api.approve(RAJ, "Engineering Management Chain Approval")
    todo = api.task(MARIA, "Prepare Onboarding")
    body = api.post(f"/api/tasks/{todo['id']}/approve", MARIA, expect=409)
    assert "not available on a to do step" in body["detail"]


def test_event_visibility(api):
    event = api.change_job(PRIYA, user=KEN, salary=95_000)
    # The subject cannot see a pending change (or approvers' comments) about themselves
    api.get(f"/api/events/{event['id']}", PRIYA, expect=403)
    api.get(f"/api/events/{event['id']}", LENA)  # assignee, HR Partner on Engineering
    api.get(f"/api/events/{event['id']}", RAJ)  # manager of a superior org
    api.get(f"/api/events/{event['id']}", DANIEL)  # HR Administrator
    api.get(f"/api/events/{event['id']}", TOM, expect=403)  # unrelated peer
    api.get(f"/api/events/{event['id']}", MARIA, expect=403)  # manager of a sibling org
    assert all(e["id"] != event["id"] for e in api.get("/api/events", TOM))
    # Once completed, the subject can see it
    api.approve(LENA, "HR Partner Approval")
    assert api.get(f"/api/events/{event['id']}", PRIYA)["status"] == "Successfully Completed"


def test_cancel_and_rescind_do_not_leak_status_to_outsiders(api):
    event = api.hire()
    # Tom cannot see the event, so he gets 403 without learning its status
    body = api.post(f"/api/events/{event['id']}/rescind", TOM, expect=403)
    assert "In Progress" not in body["detail"]
    api.post(f"/api/events/{event['id']}/cancel", TOM, expect=403)


def test_salaries_are_only_visible_to_managers_hr_and_self(api):
    def salaries(user):
        return {w["id"]: w["salary"] for w in api.get("/api/workers", user)}

    tom_view = salaries(TOM)
    assert tom_view[TOM] == 118_000  # his own
    assert tom_view["W1001"] is None and tom_view[ALICE] is None
    maria_view = salaries(MARIA)
    assert maria_view[ALICE] == 152_000  # her team (Platform Team)
    assert maria_view[PRIYA] is None  # Data Team is not hers
    assert salaries(CHEN)["W1001"] == 380_000  # Compensation Partner on the top org


def test_payroll_feed_is_restricted(api):
    api.get("/api/integrations/outbound", MARIA, expect=403)
    api.get("/api/integrations/outbound", DANIEL)


def test_concurrent_approvals_cannot_both_win(app, api, clock):
    """Two browser tabs approve the same task at the same moment."""
    api.hire(salary=110_000)
    task_id = api.task(LENA, "HR Partner Approval")["id"]
    registry = app.state.registry

    first = app.state.session_factory()
    second = app.state.session_factory()
    try:
        # Both sessions read the task while it is still open. Keep references:
        # the identity map is weak, and a garbage-collected object would simply be
        # re-read from the database (which would hide the race we want to test).
        task_in_first = first.get(Task, task_id)
        task_in_second = second.get(Task, task_id)
        assert task_in_first.status == task_in_second.status == "Open"

        BPEngine(first, registry, clock).approve(task_id, LENA, "first")
        first.commit()

        # The second session still believes the task is open, so the status check
        # passes; the version column is what stops the double approval.
        with pytest.raises(StaleDataError):
            BPEngine(second, registry, clock).approve(task_id, LENA, "second")
            second.commit()
    finally:
        first.close()
        second.close()


def test_stale_write_maps_to_http_409(app, client):
    @app.get("/api/_test/stale")
    def _raise_stale():
        raise StaleDataError("simulated")

    response = client.get("/api/_test/stale")
    assert response.status_code == 409
    assert "same time" in response.json()["detail"]


def test_already_completed_task_is_rejected_cleanly(api, engine):
    api.hire(salary=110_000)
    task_id = api.task(LENA, "HR Partner Approval")["id"]
    api.post(f"/api/tasks/{task_id}/approve", LENA)
    with pytest.raises(InvalidState):
        engine.approve(task_id, LENA)
