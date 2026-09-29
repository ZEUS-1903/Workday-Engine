from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.engine.definitions import BusinessProcessDefinition, DefinitionRegistry

DEFINITIONS = Path(__file__).resolve().parent.parent / "definitions"


def test_shipped_definitions_load():
    registry = DefinitionRegistry.from_directory(DEFINITIONS)
    assert registry.bp_types() == ["change_job", "hire", "propose_compensation", "termination"]


def test_org_specific_definition_is_found_through_superior_org():
    registry = DefinitionRegistry.from_directory(DEFINITIONS)
    definition, trace = registry.resolve("hire", ["ORG-210", "ORG-200", "ORG-100"])
    assert definition.id == "hire_engineering"
    assert trace[-1].startswith("ORG-200")


def test_default_definition_when_no_org_specific_one():
    registry = DefinitionRegistry.from_directory(DEFINITIONS)
    definition, trace = registry.resolve("hire", ["ORG-310", "ORG-300", "ORG-100"])
    assert definition.id == "hire_default"
    assert trace[-1] == "Using default definition"


def test_steps_are_grouped_alphabetically_like_workday():
    definition = BusinessProcessDefinition.model_validate(
        {
            "id": "x",
            "bp_type": "hire",
            "definition_name": "X",
            "steps": [
                {"order": "a", "type": "initiation", "name": "Start"},
                {"order": "c", "type": "integration", "name": "Last", "integration": "Payroll"},
                {"order": "ba", "type": "approval", "name": "Second", "groups": ["HR Partner"]},
                {"order": "b", "type": "approval", "name": "First", "groups": ["HR Partner"]},
                {"order": "b", "type": "to_do", "name": "Parallel", "groups": ["Manager"]},
            ],
        }
    )
    # "b" < "ba" < "c", and the two "b" steps form one parallel group
    assert definition.ordered_groups() == ["a", "b", "ba", "c"]
    assert [s.name for _, s in definition.steps_in_group("b")] == ["First", "Parallel"]


def _base(**changes):
    body = yaml.safe_load((DEFINITIONS / "termination_default.yaml").read_text())
    body.update(changes)
    return body


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["steps"].pop(0), "exactly one initiation step"),
        (lambda d: d["steps"][1].update(groups=["Janitor"]), "Unknown security group"),
        (lambda d: d["steps"][1].update(entry_condition="salary > 5"), "Unknown name"),
        (lambda d: d["steps"][1].update(while_running_condition="approver.management_level > 2"), "approval_chain"),
        (lambda d: d["steps"][1].update(order="B"), "pattern"),
        (lambda d: d["steps"][1].update(surprise=True), "Extra inputs"),
        (lambda d: d["steps"][0].update(order="z"), "lowest order"),
    ],
)
def test_invalid_definitions_are_rejected(mutate, message):
    body = _base()
    mutate(body)
    with pytest.raises(ValidationError) as exc_info:
        BusinessProcessDefinition.model_validate(body)
    assert message in str(exc_info.value)


def test_registry_requires_one_default_and_a_policy(tmp_path):
    registry = DefinitionRegistry.from_directory(DEFINITIONS)
    definitions = registry.definitions_for("termination")
    with pytest.raises(ValueError, match="security policy"):
        DefinitionRegistry(definitions, {})
    org_specific = definitions[0].model_copy(update={"id": "t2", "org_id": "ORG-200"})
    with pytest.raises(ValueError, match="exactly one default"):
        DefinitionRegistry([org_specific], registry.policies)
