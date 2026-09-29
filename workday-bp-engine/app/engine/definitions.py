"""Business process definitions and security policies.

Concept mapping to Workday:

* A **business process type** (Hire, Change Job, Termination ...) is delivered
  by the system. Each type has exactly one **security policy** that says which
  security groups may initiate, cancel or rescind it.
* A type can have several **definitions**: one *default* definition plus
  optional *organization-specific* definitions. When an event starts, the
  engine checks the event's supervisory organization, then each superior
  organization, and falls back to the default definition.
* A definition is an ordered list of **steps**. Workday orders steps with
  letters and sorts them alphabetically ("a", "b", "ba", "c"); steps that share
  the same letter run **in parallel**, and all of them must finish before the
  next letter starts. The Initiation step is always first.

Definitions live in YAML files under ``definitions/`` so you can change routing
without touching Python code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .conditions import ConditionError, compile_condition

StepType = Literal["initiation", "approval", "approval_chain", "to_do", "subprocess", "integration"]
RoutingRestriction = Literal["exclude_initiator", "exclude_prior_approvers", "exclude_event_subject"]

ROLE_BASED_GROUPS = frozenset({"Manager", "HR Partner", "Compensation Partner"})
USER_BASED_GROUPS = frozenset({"HR Administrator", "Business Process Administrator"})
PSEUDO_GROUPS = frozenset({"Initiator"})
ALL_GROUPS = ROLE_BASED_GROUPS | USER_BASED_GROUPS | PSEUDO_GROUPS


def _check_condition(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        compile_condition(value)
    except ConditionError as exc:
        raise ValueError(str(exc)) from None
    return value


def _check_groups(groups: list[str], allow_pseudo: bool = False) -> list[str]:
    allowed = ALL_GROUPS if allow_pseudo else ROLE_BASED_GROUPS | USER_BASED_GROUPS
    for group in groups:
        if group not in allowed:
            raise ValueError(f"Unknown security group {group!r}. Known: {sorted(allowed)}")
    return groups


class StepDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: str = Field(pattern=r"^[a-z]{1,4}$", description="Workday-style step order letter(s)")
    type: StepType
    name: str
    groups: list[str] = Field(default_factory=list)
    alternate_groups: list[str] = Field(default_factory=list)
    all_must_approve: bool = False
    routing_restrictions: list[RoutingRestriction] = Field(default_factory=list)
    entry_condition: str | None = None
    while_running_condition: str | None = None
    max_approvers: int | None = Field(default=None, ge=1)
    due_in_days: int | None = Field(default=None, ge=0)
    subprocess: str | None = None
    integration: str | None = None
    instructions: str | None = None

    @field_validator("entry_condition", "while_running_condition")
    @classmethod
    def check_conditions(cls, value: str | None) -> str | None:
        return _check_condition(value)

    @field_validator("groups", "alternate_groups")
    @classmethod
    def check_groups(cls, value: list[str]) -> list[str]:
        return _check_groups(value)

    @model_validator(mode="after")
    def _check_type_specific_fields(self) -> "StepDefinition":
        if self.type in ("approval", "to_do") and not self.groups:
            raise ValueError(f"Step {self.name!r}: {self.type} steps need at least one security group")
        if self.while_running_condition and self.type != "approval_chain":
            raise ValueError(
                f"Step {self.name!r}: while-running conditions only apply to approval_chain steps"
            )
        if self.type == "subprocess" and not self.subprocess:
            raise ValueError(f"Step {self.name!r}: subprocess steps must name a subprocess")
        if self.type == "integration" and not self.integration:
            raise ValueError(f"Step {self.name!r}: integration steps must name an integration")
        if self.type == "initiation" and self.entry_condition:
            raise ValueError("The Initiation step cannot have an entry condition")
        return self


class ValidationRule(BaseModel):
    """A validation condition checked when the event is submitted.

    ``error_when`` is written so that the rule *fires* (shows the message)
    when the expression is true. ``critical`` blocks submission; ``warning`` is
    recorded on the event and shown to approvers.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    error_when: str
    severity: Literal["critical", "warning"] = "critical"
    message: str

    @field_validator("error_when")
    @classmethod
    def check_condition(cls, value: str) -> str:
        return _check_condition(value)


class BusinessProcessDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    bp_type: str
    definition_name: str
    org_id: str | None = Field(
        default=None, description="None for the default definition, else the supervisory org it applies to"
    )
    description: str | None = None
    validations: list[ValidationRule] = Field(default_factory=list)
    steps: list[StepDefinition]

    @model_validator(mode="after")
    def _check_steps(self) -> "BusinessProcessDefinition":
        initiations = [s for s in self.steps if s.type == "initiation"]
        if len(initiations) != 1:
            raise ValueError(f"{self.id}: exactly one initiation step is required")
        first_order = min(s.order for s in self.steps)
        if initiations[0].order != first_order:
            raise ValueError(f"{self.id}: the initiation step must have the lowest order")
        if sum(1 for s in self.steps if s.order == first_order) > 1:
            raise ValueError(f"{self.id}: no other step may share the initiation step's order")
        return self

    def ordered_groups(self) -> list[str]:
        """Distinct step orders in execution order (alphabetical, like Workday)."""
        return sorted({step.order for step in self.steps})

    def steps_in_group(self, order: str) -> list[tuple[int, StepDefinition]]:
        return [(index, step) for index, step in enumerate(self.steps) if step.order == order]


class SecurityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    initiate: list[str] = Field(default_factory=list)
    cancel: list[str] = Field(default_factory=list)
    rescind: list[str] = Field(default_factory=list)

    @field_validator("initiate", "cancel", "rescind")
    @classmethod
    def check_groups(cls, value: list[str]) -> list[str]:
        return _check_groups(value, allow_pseudo=True)


class DefinitionRegistry:
    """Loads definitions and security policies and resolves which one applies."""

    def __init__(
        self,
        definitions: list[BusinessProcessDefinition],
        policies: dict[str, SecurityPolicy],
    ) -> None:
        self._by_id: dict[str, BusinessProcessDefinition] = {}
        for definition in definitions:
            if definition.id in self._by_id:
                raise ValueError(f"Duplicate definition id {definition.id!r}")
            self._by_id[definition.id] = definition
        self.policies = policies

        for bp_type in {d.bp_type for d in definitions}:
            defaults = [d for d in definitions if d.bp_type == bp_type and d.org_id is None]
            if len(defaults) != 1:
                raise ValueError(f"Business process type {bp_type!r} needs exactly one default definition")
            if bp_type not in policies:
                raise ValueError(f"Business process type {bp_type!r} has no security policy")
            org_ids = [d.org_id for d in definitions if d.bp_type == bp_type and d.org_id]
            if len(org_ids) != len(set(org_ids)):
                raise ValueError(f"{bp_type!r} has two definitions for the same organization")

        for definition in definitions:
            for step in definition.steps:
                if step.subprocess and not any(d.bp_type == step.subprocess for d in definitions):
                    raise ValueError(
                        f"{definition.id}: subprocess {step.subprocess!r} has no definition"
                    )

    @classmethod
    def from_directory(cls, directory: Path) -> "DefinitionRegistry":
        policies_file = directory / "security_policies.yaml"
        raw_policies = yaml.safe_load(policies_file.read_text()) or {}
        policies = {name: SecurityPolicy.model_validate(body) for name, body in raw_policies.items()}
        definitions = []
        for path in sorted(directory.glob("*.yaml")):
            if path.name == policies_file.name:
                continue
            try:
                definitions.append(BusinessProcessDefinition.model_validate(yaml.safe_load(path.read_text())))
            except Exception as exc:  # re-raise with the file name for a friendlier error
                raise ValueError(f"Invalid definition file {path.name}: {exc}") from exc
        return cls(definitions, policies)

    def get(self, definition_id: str) -> BusinessProcessDefinition:
        return self._by_id[definition_id]

    def bp_types(self) -> list[str]:
        return sorted({d.bp_type for d in self._by_id.values()})

    def definitions_for(self, bp_type: str) -> list[BusinessProcessDefinition]:
        return [d for d in self._by_id.values() if d.bp_type == bp_type]

    def policy(self, bp_type: str) -> SecurityPolicy:
        return self.policies[bp_type]

    def resolve(
        self, bp_type: str, org_chain_ids: list[str]
    ) -> tuple[BusinessProcessDefinition, list[str]]:
        """Pick the definition for an event in the first org of ``org_chain_ids``.

        ``org_chain_ids`` is the event's organization followed by its superior
        organizations up to the top. Returns the definition plus a readable
        trace of the lookup, which the UI shows to explain the choice.
        """
        candidates = self.definitions_for(bp_type)
        if not candidates:
            raise KeyError(bp_type)
        by_org = {d.org_id: d for d in candidates if d.org_id}
        trace: list[str] = []
        for org_id in org_chain_ids:
            if org_id in by_org:
                trace.append(f"{org_id}: organization-specific definition found")
                return by_org[org_id], trace
            trace.append(f"{org_id}: no organization-specific definition")
        default = next(d for d in candidates if d.org_id is None)
        trace.append("Using default definition")
        return default, trace
