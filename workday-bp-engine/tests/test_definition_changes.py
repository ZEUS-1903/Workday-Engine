"""Definition snapshots and less common step options."""

import shutil
from pathlib import Path

import yaml

from app.engine.definitions import DefinitionRegistry
from conftest import DANIEL, LENA, MARIA, SOFIA, TOM, step_status

DEFINITIONS = Path(__file__).resolve().parent.parent / "definitions"


def _edited_registry(tmp_path: Path, filename: str, edit) -> DefinitionRegistry:
    directory = tmp_path / "edited-definitions"
    if not directory.exists():
        shutil.copytree(DEFINITIONS, directory)
    path = directory / filename
    body = yaml.safe_load(path.read_text())
    edit(body)
    path.write_text(yaml.safe_dump(body, sort_keys=False))
    return DefinitionRegistry.from_directory(directory)


def test_running_events_keep_the_definitions_they_started_with(app, api, tmp_path):
    event = api.hire(salary=130_000)

    # An administrator edits both definitions while the hire is in progress
    def rename(suffix):
        return lambda body: body.update(definition_name=body["definition_name"] + suffix)

    _edited_registry(tmp_path, "hire_engineering.yaml", rename(" v2"))
    app.state.registry = _edited_registry(tmp_path, "propose_compensation_default.yaml", rename(" v2"))

    # Send back to the initiator; resubmitting restarts the sub-process
    task = api.task(LENA, "HR Partner Approval")
    target = next(t for t in task["send_back_targets"] if t["name"] == "Hire")
    api.post(f"/api/tasks/{task['id']}/send-back", LENA, {"comment": "Fix salary", "to_step_id": target["id"]})
    event = api.submit(MARIA, "Hire", data={"salary": 125_000})

    assert event["definition_name"] == "Hire (Engineering)"
    restarted_child = api.event(event["children"][-1]["id"])
    assert restarted_child["definition_name"] == "Propose Compensation (Default Definition)"
    assert "captured when" in restarted_child["definition_trace"][-1]

    # A new event picks up the edited definitions
    new_event = api.hire(name="Sam Rivera", email="sam.rivera@summit-analytics.example", salary=130_000)
    assert new_event["definition_name"] == "Hire (Engineering) v2"
    assert api.event(new_event["children"][0]["id"])["definition_name"] == "Propose Compensation (Default Definition) v2"


def test_all_must_approve(app, api, tmp_path):
    def require_everyone(body):
        body["steps"][1]["all_must_approve"] = True

    app.state.registry = _edited_registry(tmp_path, "termination_default.yaml", require_everyone)

    # Lena initiates, so the step routes to both HR Administrators
    event = api.terminate(TOM, user=LENA)
    api.approve(SOFIA, "HR Partner Approval")
    event = api.event(event["id"])
    assert step_status(event, "HR Partner Approval") == "Awaiting Action"  # still waiting for Daniel
    event = api.approve(DANIEL, "HR Partner Approval")
    assert step_status(event, "HR Partner Approval") == "Completed"
    assert step_status(event, "Offboarding Checklist") == "Awaiting Action"
