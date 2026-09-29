from conftest import ALICE, BEN, CHEN, DANIEL, LENA, MARIA, PRIYA, RAJ, TOM, step_status


def test_small_raise_only_needs_hr_partner(api):
    event = api.change_job(TOM, salary=124_000)  # +5.1%
    assert step_status(event, "Compensation Partner Approval") == "Not Required"
    event = api.approve(LENA, "HR Partner Approval")
    assert event["status"] == "Successfully Completed"
    assert step_status(event, "Management Chain Approval") == "Not Required"
    change = event["data_changes"][0]
    assert change["before"]["salary"] == 118_000 and change["after"]["salary"] == 124_000


def test_promotion_goes_up_the_management_chain(api):
    # Senior Software Engineer (level 6) -> Engineering Manager (level 5), +8.6%
    event = api.change_job(ALICE, job_profile_id="JP-EM", salary=165_000, reason="Promotion")
    assert step_status(event, "Compensation Partner Approval") == "Not Required"
    api.approve(LENA, "HR Partner Approval")
    event = api.event(event["id"])
    chain = next(s for s in event["steps"] if s["type"] == "approval_chain")
    assert [p["id"] for p in chain["chain"]] == [RAJ]
    event = api.approve(RAJ, "Management Chain Approval")
    assert event["status"] == "Successfully Completed"
    payload = event["outbound"][0]["payload"]
    assert payload["record_type"] == "JOB_CHANGE"
    assert payload["fields"]["job_profile_id"] == {"from": "JP-SSE", "to": "JP-EM"}


def test_large_raise_needs_compensation_partner(api):
    event = api.change_job(PRIYA, user="W1006", salary=104_000)  # Ken raises Priya by 13%
    assert step_status(event, "Compensation Partner Approval") == "Awaiting Action"
    assert api.task(CHEN, "Compensation Partner Approval")


def test_critical_validations_block_submission(api):
    body = api.change_job(TOM, expect=422)  # nothing changes
    assert body["errors"] == ["No changes were proposed."]
    body = api.change_job(TOM, salary=120_000, effective_date="2020-01-01", expect=422)
    assert "before the worker's hire date" in body["detail"]


def test_one_in_progress_event_per_worker(api):
    api.change_job(TOM, salary=124_000)
    body = api.terminate(TOM, expect=409)
    assert "already has an in-progress Change Job" in body["detail"]


def test_termination_runs_todo_and_payroll_in_parallel(api):
    event = api.terminate(TOM)
    event = api.approve(LENA, "HR Partner Approval")
    # Both "c" steps started together: the integration finished immediately,
    # the To Do is still waiting, so the event is not complete yet.
    assert step_status(event, "Send to Payroll") == "Completed"
    assert step_status(event, "Offboarding Checklist") == "Awaiting Action"
    assert event["status"] == "In Progress"
    event = api.submit(MARIA, "Offboarding Checklist", comment="Laptop returned")
    assert event["status"] == "Successfully Completed"
    tom = next(w for w in api.get("/api/workers?include_terminated=true", DANIEL) if w["id"] == TOM)
    assert tom["status"] == "Terminated" and tom["termination_date"] == "2026-10-15"


def test_cancel_after_integration_sends_reversal(api):
    event = api.terminate(TOM)
    api.approve(LENA, "HR Partner Approval")
    event = api.post(f"/api/events/{event['id']}/cancel", MARIA, {"comment": "Employee stayed"})
    assert [m["record_type"] for m in event["outbound"]] == ["TERMINATION", "CANCEL"]
    tom = next(w for w in api.get("/api/workers", DANIEL) if w["id"] == TOM)
    assert tom["status"] == "Active"


def test_terminated_worker_can_no_longer_act(api):
    event = api.terminate(LENA, user=DANIEL)
    # Lena sits in HR Operations, whose HR Partner is inherited from the top org (Ben)
    api.approve(BEN, "HR Partner Approval")
    api.submit(DANIEL, "Offboarding Checklist")
    assert api.event(event["id"])["status"] == "Successfully Completed"
    api.get("/api/tasks", LENA, expect=401)
