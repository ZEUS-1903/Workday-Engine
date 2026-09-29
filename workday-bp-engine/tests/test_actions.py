"""Deny, Send Back, Cancel and Rescind."""

from conftest import ALICE, CHEN, DANIEL, LENA, MARIA, RAJ, SOFIA, TOM, step_status


def test_deny_terminates_and_writes_no_data(api):
    event = api.hire(salary=130_000)
    task = api.task(LENA, "HR Partner Approval")

    body = api.post(f"/api/tasks/{task['id']}/deny", LENA, {"comment": None}, expect=422)
    assert "comment is required" in body["errors"]

    event = api.post(f"/api/tasks/{task['id']}/deny", LENA, {"comment": "Headcount frozen"})
    assert event["status"] == "Denied"
    assert api.inbox(CHEN) == []  # the sub-process was canceled with it
    child = api.event(event["children"][0]["id"])
    assert child["status"] == "Canceled"
    assert all(w["name"] != "Jordan Blake" for w in api.get("/api/workers", DANIEL))


def test_denied_subprocess_denies_parent(api):
    event = api.hire(salary=130_000)
    task = api.task(CHEN, "Compensation Partner Approval")
    api.post(f"/api/tasks/{task['id']}/deny", CHEN, {"comment": "Offer too high"})
    event = api.event(event["id"])
    assert event["status"] == "Denied"
    assert api.inbox(LENA) == []


def test_send_back_to_initiator_and_resubmit(api):
    event = api.hire(salary=130_000)
    api.approve(LENA, "HR Partner Approval")
    api.approve(CHEN, "Compensation Partner Approval")

    task = api.task(RAJ, "Engineering Management Chain Approval")
    targets = {t["name"]: t["id"] for t in task["send_back_targets"]}
    assert set(targets) == {"Hire", "HR Partner Approval"}  # not the sub-process
    api.post(f"/api/tasks/{task['id']}/send-back", RAJ, {"comment": "Offer 120k instead", "to_step_id": targets["Hire"]})

    event = api.event(event["id"])
    assert event["status"] == "In Progress"
    resubmit = api.task(MARIA, "Hire")
    assert resubmit["kind"] == "initiation"

    # Editing a field that is not allowed is rejected
    body = api.post(f"/api/tasks/{resubmit['id']}/submit", MARIA, {"data": {"org_id": "ORG-220"}}, expect=422)
    assert "cannot be changed" in body["detail"]

    event = api.submit(MARIA, "Hire", data={"salary": 120_000}, comment="Lowered offer")
    assert event["data"]["salary"] == 120_000
    # Everything after the initiation runs again, with the new data
    assert api.task(LENA, "HR Partner Approval")
    child_ids = [c["id"] for c in event["children"]]
    assert len(child_ids) == 2  # the old sub-process stays in history, a new one runs
    # The first sub-process had already finished before the send back, so it is not
    # canceled; its parent step is simply superseded.
    assert api.event(child_ids[0])["status"] == "Successfully Completed"
    assert api.event(child_ids[1])["status"] == "In Progress"  # 120k is still above the 117k midpoint
    superseded = [s for s in event["steps"] if not s["active"]]
    assert {s["name"] for s in superseded} >= {"Hire", "HR Partner Approval", "Engineering Management Chain Approval"}


def test_send_back_to_an_approver(api):
    event = api.change_job(ALICE, salary=180_000, job_profile_id="JP-EM", reason="Promotion")  # +18.4%
    api.approve(LENA, "HR Partner Approval")
    api.approve(CHEN, "Compensation Partner Approval")
    task = api.task(RAJ, "Management Chain Approval")
    target = next(t for t in task["send_back_targets"] if t["name"] == "HR Partner Approval")
    api.post(f"/api/tasks/{task['id']}/send-back", RAJ, {"comment": "Check the level", "to_step_id": target["id"]})
    assert api.task(LENA, "HR Partner Approval")
    api.approve(LENA, "HR Partner Approval")
    # Compensation approval in the same parallel group stays completed; the chain restarts
    event = api.event(event["id"])
    assert step_status(event, "Compensation Partner Approval") == "Completed"
    assert api.task(RAJ, "Management Chain Approval")


def test_cancel_rules(api):
    event = api.hire()
    # Lena is not the initiator and holds no admin group
    api.post(f"/api/events/{event['id']}/cancel", LENA, {"comment": "no"}, expect=403)
    event = api.post(f"/api/events/{event['id']}/cancel", MARIA, {"comment": "Candidate declined"})
    assert event["status"] == "Canceled"
    assert api.inbox(LENA) == []
    # Cannot cancel twice, and a canceled process cannot be rescinded
    api.post(f"/api/events/{event['id']}/cancel", MARIA, expect=409)
    api.post(f"/api/events/{event['id']}/rescind", DANIEL, expect=409)


def test_cannot_cancel_a_subprocess_directly(api):
    event = api.hire(salary=130_000)
    child_id = event["children"][0]["id"]
    body = api.post(f"/api/events/{child_id}/cancel", DANIEL, expect=409)
    assert "sub-process" in body["detail"]


def _complete_raise(api, worker_id, salary):
    event = api.change_job(worker_id, salary=salary)
    api.approve(LENA, "HR Partner Approval")
    return api.event(event["id"])


def test_rescind_restores_data_and_requires_rescinding_later_events_first(api):
    first = _complete_raise(api, TOM, 124_000)  # +5%: HR Partner only
    assert first["status"] == "Successfully Completed"
    second = _complete_raise(api, TOM, 130_000)
    assert second["status"] == "Successfully Completed"

    # Only Business Process Administrators may rescind
    api.post(f"/api/events/{first['id']}/rescind", MARIA, expect=403)
    body = api.post(f"/api/events/{first['id']}/rescind", DANIEL, expect=409)
    assert f"#{second['id']}" in body["detail"]

    api.post(f"/api/events/{second['id']}/rescind", DANIEL, {"comment": "Entered twice"})
    tom = next(w for w in api.get("/api/workers", DANIEL) if w["id"] == TOM)
    assert tom["salary"] == 124_000
    rescinded = api.post(f"/api/events/{first['id']}/rescind", DANIEL)
    assert rescinded["status"] == "Rescinded"
    tom = next(w for w in api.get("/api/workers", DANIEL) if w["id"] == TOM)
    assert tom["salary"] == 118_000
    # A reversal record was sent to payroll
    assert [m["record_type"] for m in rescinded["outbound"]] == ["JOB_CHANGE", "RESCIND"]


def test_rescind_hire_removes_worker(api):
    event = api.hire(salary=110_000)
    api.approve(LENA, "HR Partner Approval")
    api.approve(RAJ, "Engineering Management Chain Approval")
    event = api.submit(MARIA, "Prepare Onboarding")
    worker_id = event["subject"]["id"]
    assert any(w["id"] == worker_id for w in api.get("/api/workers", DANIEL))
    event = api.post(f"/api/events/{event['id']}/rescind", DANIEL, {"comment": "Offer withdrawn"})
    assert event["status"] == "Rescinded"
    assert event["subject"]["name"] == "Jordan Blake"
    assert all(w["id"] != worker_id for w in api.get("/api/workers?include_terminated=true", DANIEL))


def test_rescind_only_for_completed(api):
    event = api.hire()
    body = api.post(f"/api/events/{event['id']}/rescind", DANIEL, expect=409)
    assert "Cancel" in body["detail"]


def test_approval_by_first_group_member_closes_the_others(api):
    # Lena initiates, so she is excluded from HR Partner Approval and the step
    # routes to the alternate group: both HR Administrators get the task.
    event = api.terminate(TOM, user=LENA)
    step = next(s for s in event["steps"] if s["name"] == "HR Partner Approval")
    assert {p["id"] for p in step["assignees"]} == {SOFIA, DANIEL}
    assert "alternate group" in step["note"]
    api.approve(SOFIA, "HR Partner Approval")
    assert all(t["step"]["name"] != "HR Partner Approval" for t in api.inbox(DANIEL))
