"""End-to-end Hire scenarios through the REST API."""

from conftest import BEN, CHEN, DANIEL, GRACE, LENA, MARIA, RAJ, SOFIA, step_status


def test_engineering_hire_happy_path(api):
    event = api.hire(salary=130_000)  # above the Software Engineer II midpoint of 117k
    assert event["status"] == "In Progress"
    assert event["definition_id"] == "hire_engineering"  # found via the superior org

    # Step b runs two steps in parallel
    assert step_status(event, "HR Partner Approval") == "Awaiting Action"
    assert step_status(event, "Propose Compensation") == "In Progress"
    assert api.task(LENA, "HR Partner Approval")
    assert api.task(CHEN, "Compensation Partner Approval")  # task lives in the sub-process
    assert api.inbox(RAJ) == []  # step c has not started yet

    api.approve(LENA, "HR Partner Approval")
    assert api.inbox(RAJ) == []  # still waiting for the parallel sub-process
    api.approve(CHEN, "Compensation Partner Approval", "Justified by interview feedback")

    # Step c: approval chain. Maria is the initiator (excluded); Raj is a VP (level 3)
    # so he approves; Grace (CEO, level 1) fails the while-running condition.
    event = api.event(event["id"])
    chain_step = next(s for s in event["steps"] if s["type"] == "approval_chain")
    assert [p["id"] for p in chain_step["chain"]] == [RAJ]
    assert "Maria Gomez (routing restriction)" in chain_step["note"]
    assert "Grace Lee (while-running condition)" in chain_step["note"]

    api.approve(RAJ, "Engineering Management Chain Approval")
    event = api.submit(MARIA, "Prepare Onboarding", comment="Laptop ordered")

    assert event["status"] == "Successfully Completed"
    new_id = event["subject"]["id"]
    assert new_id == event["data"]["worker_id"] == "W2001"
    worker = next(w for w in api.get("/api/workers", DANIEL) if w["id"] == new_id)
    assert worker["salary"] == 130_000 and worker["hire_date"] == "2026-10-01"
    assert [m["record_type"] for m in event["outbound"]] == ["HIRE"]
    assert event["outbound"][0]["payload"]["fields"]["annual_salary"] == 130_000


def test_compensation_approval_not_required_below_midpoint(api):
    event = api.hire(salary=110_000)  # below the 117k midpoint
    child_id = next(s for s in event["steps"] if s["type"] == "subprocess")["child_event_id"]
    child = api.event(child_id)
    assert child["status"] == "Successfully Completed"
    assert step_status(child, "Compensation Partner Approval") == "Not Required"
    assert api.inbox(CHEN) == []
    assert step_status(event, "Propose Compensation") == "Completed"


def test_default_definition_skips_chain_for_normal_salary(api):
    # HR Operations has no org-specific Hire definition -> default definition
    event = api.hire(user=DANIEL, org_id="ORG-310", job_profile_id="JP-HRP", salary=90_000)
    assert event["definition_id"] == "hire_default"
    # HR Partner is inherited from the top org (Ben), not Lena (who is on Engineering)
    assert api.task(BEN, "HR Partner Approval")
    api.approve(BEN, "HR Partner Approval")
    event = api.event(event["id"])
    assert step_status(event, "Management Chain Approval (high salary)") == "Not Required"
    # To Do goes to the manager of HR Operations, who is Daniel himself
    event = api.submit(DANIEL, "Prepare Onboarding")
    assert event["status"] == "Successfully Completed"


def test_default_definition_uses_chain_for_high_salary(api):
    event = api.hire(user=DANIEL, org_id="ORG-310", job_profile_id="JP-HRM", salary=150_000)
    api.approve(BEN, "HR Partner Approval")
    api.approve(CHEN, "Compensation Partner Approval")  # 150k > 130k midpoint
    event = api.event(event["id"])
    chain = next(s for s in event["steps"] if s["type"] == "approval_chain")
    # Daniel (initiator) excluded, Sofia (VP) approves, Grace skipped
    assert [p["id"] for p in chain["chain"]] == [SOFIA]
    assert GRACE not in [p["id"] for p in chain["chain"]]


def test_validation_warning_is_recorded_but_does_not_block(api):
    event = api.hire(salary=150_000)  # above the 140k max for Software Engineer II
    assert event["status"] == "In Progress"
    assert event["warnings"] == ["Proposed salary is outside the pay range for the selected job profile."]


def test_unknown_job_profile_is_rejected(api):
    body = api.hire(job_profile_id="JP-NOPE", expect=422)
    assert "Unknown job profile" in body["detail"]


def test_request_schema_validation(api):
    body = api.hire(salary=-5, expect=422)
    assert body["detail"][0]["loc"][-1] == "salary"
