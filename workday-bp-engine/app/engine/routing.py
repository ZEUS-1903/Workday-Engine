"""Who holds which role where.

Two different questions are answered here, on purpose:

* **Routing** (who gets the task?): the role holder on the event's
  organization. If nobody is assigned there, the assignment is inherited from
  the nearest superior organization that has one.
* **Security** (may this person initiate, cancel or view?): a role-based group
  grants access to the organization it is assigned on *and all of its
  subordinate organizations*. Workday lets administrators configure how far a
  role's access reaches; this engine uses the "including subordinates" option
  for every role to keep things predictable.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import GroupMembership, RoleAssignment, SupervisoryOrg, Worker
from .definitions import ROLE_BASED_GROUPS, USER_BASED_GROUPS


class Directory:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ---- organizations -------------------------------------------------
    def org_chain(self, org_id: str) -> list[SupervisoryOrg]:
        """The organization followed by its superiors, nearest first."""
        chain: list[SupervisoryOrg] = []
        seen: set[str] = set()
        current = self.session.get(SupervisoryOrg, org_id)
        while current is not None and current.id not in seen:
            chain.append(current)
            seen.add(current.id)
            current = self.session.get(SupervisoryOrg, current.superior_id) if current.superior_id else None
        return chain

    def org_chain_ids(self, org_id: str) -> list[str]:
        return [org.id for org in self.org_chain(org_id)]

    # ---- workers -------------------------------------------------------
    def is_active(self, worker_id: str) -> bool:
        worker = self.session.get(Worker, worker_id)
        return worker is not None and worker.status == "Active"

    def _direct_role_holders(self, role: str, org_id: str) -> list[str]:
        ids = self.session.scalars(
            select(RoleAssignment.worker_id)
            .where(RoleAssignment.role == role, RoleAssignment.org_id == org_id)
            .order_by(RoleAssignment.id)
        ).all()
        return [worker_id for worker_id in ids if self.is_active(worker_id)]

    # ---- routing -------------------------------------------------------
    def role_assignees(self, role: str, org_id: str) -> list[str]:
        """Role holders for routing, inherited from the nearest superior org."""
        for org in self.org_chain(org_id):
            holders = self._direct_role_holders(role, org.id)
            if holders:
                return holders
        return []

    def group_members(self, group: str) -> list[str]:
        ids = self.session.scalars(
            select(GroupMembership.worker_id)
            .where(GroupMembership.group == group)
            .order_by(GroupMembership.id)
        ).all()
        return [worker_id for worker_id in ids if self.is_active(worker_id)]

    def resolve_assignees(self, groups: list[str], org_id: str) -> list[str]:
        """Everyone who should receive a task for any of ``groups``, de-duplicated."""
        result: list[str] = []
        for group in groups:
            if group in ROLE_BASED_GROUPS:
                members = self.role_assignees(group, org_id)
            elif group in USER_BASED_GROUPS:
                members = self.group_members(group)
            else:
                raise ValueError(f"Cannot route to group {group!r}")
            for member in members:
                if member not in result:
                    result.append(member)
        return result

    def management_chain(self, org_id: str) -> list[str]:
        """Managers of the org and each superior org, nearest first (no inheritance)."""
        chain: list[str] = []
        for org in self.org_chain(org_id):
            for manager in self._direct_role_holders("Manager", org.id):
                if manager not in chain:
                    chain.append(manager)
        return chain

    # ---- security ------------------------------------------------------
    def has_role_access(self, worker_id: str, role: str, org_id: str) -> bool:
        """True if the worker holds ``role`` on the org or any superior org."""
        return any(worker_id in self._direct_role_holders(role, org.id) for org in self.org_chain(org_id))

    def user_in_any(
        self, worker_id: str, groups: list[str], org_id: str, initiator_id: str | None = None
    ) -> bool:
        if not self.is_active(worker_id):
            return False
        for group in groups:
            if group == "Initiator":
                if initiator_id is not None and worker_id == initiator_id:
                    return True
            elif group in ROLE_BASED_GROUPS:
                if self.has_role_access(worker_id, group, org_id):
                    return True
            elif group in USER_BASED_GROUPS:
                if worker_id in self.group_members(group):
                    return True
        return False

    def can_see_compensation(self, viewer_id: str, worker: Worker) -> bool:
        """Salary is visible to the worker, their managers, HR and admins only."""
        if viewer_id == worker.id:
            return True
        return self.user_in_any(
            viewer_id,
            ["Manager", "HR Partner", "Compensation Partner", "HR Administrator", "Business Process Administrator"],
            worker.org_id,
        )

    def security_summary(self, worker_id: str) -> dict:
        """Roles and groups for display in the UI."""
        roles = self.session.execute(
            select(RoleAssignment.role, SupervisoryOrg.id, SupervisoryOrg.name)
            .join(SupervisoryOrg, SupervisoryOrg.id == RoleAssignment.org_id)
            .where(RoleAssignment.worker_id == worker_id)
            .order_by(RoleAssignment.id)
        ).all()
        groups = self.session.scalars(
            select(GroupMembership.group).where(GroupMembership.worker_id == worker_id)
        ).all()
        return {
            "roles": [{"role": role, "org_id": oid, "org_name": oname} for role, oid, oname in roles],
            "groups": list(groups),
        }
