"""Demo tenant data for a fictional company, Summit Analytics.

Org chart (supervisory organizations)::

    ORG-100 Summit Analytics ............ Manager: Grace Lee (CEO)
    ├── ORG-200 Engineering ............. Manager: Raj Patel (VP)
    │   ├── ORG-210 Platform Team ....... Manager: Maria Gomez
    │   └── ORG-220 Data Team ........... Manager: Ken Adams
    ├── ORG-300 People & Culture ........ Manager: Sofia Martins (VP)
    │   └── ORG-310 HR Operations ....... Manager: Daniel Kim
    └── ORG-400 Finance ................. Manager: Omar Haddad (CFO)

Role assignments (inherited down the hierarchy unless overridden):

* HR Partner: Lena Fischer on Engineering, Ben Carter on Summit Analytics
* Compensation Partner: Chen Wei on Summit Analytics

User-based security groups:

* HR Administrator: Sofia Martins, Daniel Kim
* Business Process Administrator: Daniel Kim
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import GroupMembership, IdSequence, JobProfile, RoleAssignment, SupervisoryOrg, Worker

JOB_PROFILES = [
    # id, name, management level, min, mid, max
    ("JP-CEO", "Chief Executive Officer", 1, 250_000, 350_000, 450_000),
    ("JP-CFO", "Chief Financial Officer", 2, 220_000, 290_000, 360_000),
    ("JP-VP", "Vice President", 3, 190_000, 240_000, 290_000),
    ("JP-DIR", "Director", 4, 160_000, 195_000, 230_000),
    ("JP-EM", "Engineering Manager", 5, 150_000, 180_000, 210_000),
    ("JP-HRM", "HR Manager", 5, 110_000, 130_000, 150_000),
    ("JP-SSE", "Senior Software Engineer", 6, 125_000, 150_000, 175_000),
    ("JP-SE2", "Software Engineer II", 6, 95_000, 117_000, 140_000),
    ("JP-DA", "Data Analyst", 6, 70_000, 87_000, 105_000),
    ("JP-HRP", "HR Partner", 6, 80_000, 97_000, 115_000),
    ("JP-CA", "Compensation Analyst", 6, 85_000, 100_000, 120_000),
]

MANAGEMENT_LEVELS = {
    1: "Chief Executive",
    2: "C-Suite",
    3: "Vice President",
    4: "Director",
    5: "Manager",
    6: "Individual Contributor",
}

ORGS = [
    ("ORG-100", "Summit Analytics", None),
    ("ORG-200", "Engineering", "ORG-100"),
    ("ORG-210", "Platform Team", "ORG-200"),
    ("ORG-220", "Data Team", "ORG-200"),
    ("ORG-300", "People & Culture", "ORG-100"),
    ("ORG-310", "HR Operations", "ORG-300"),
    ("ORG-400", "Finance", "ORG-100"),
]

WORKERS = [
    # id, name, job profile, org the worker sits in, salary, hire date
    ("W1001", "Grace Lee", "JP-CEO", "ORG-100", 380_000, "2015-04-01"),
    ("W1002", "Omar Haddad", "JP-CFO", "ORG-100", 300_000, "2016-02-15"),
    ("W1003", "Raj Patel", "JP-VP", "ORG-100", 255_000, "2017-06-01"),
    ("W1004", "Sofia Martins", "JP-VP", "ORG-100", 230_000, "2018-01-08"),
    ("W1005", "Maria Gomez", "JP-EM", "ORG-200", 182_000, "2019-03-11"),
    ("W1006", "Ken Adams", "JP-EM", "ORG-200", 176_000, "2020-05-04"),
    ("W1007", "Daniel Kim", "JP-HRM", "ORG-300", 128_000, "2019-09-16"),
    ("W1008", "Alice Johnson", "JP-SSE", "ORG-210", 152_000, "2020-03-15"),
    ("W1009", "Tom Nguyen", "JP-SE2", "ORG-210", 118_000, "2022-08-01"),
    ("W1010", "Priya Shah", "JP-DA", "ORG-220", 92_000, "2021-11-29"),
    ("W1011", "Lena Fischer", "JP-HRP", "ORG-310", 99_000, "2021-01-19"),
    ("W1012", "Chen Wei", "JP-CA", "ORG-310", 104_000, "2022-04-04"),
    ("W1013", "Ben Carter", "JP-HRP", "ORG-310", 96_000, "2023-02-06"),
]

ROLE_ASSIGNMENTS = [
    ("Manager", "ORG-100", "W1001"),
    ("Manager", "ORG-200", "W1003"),
    ("Manager", "ORG-210", "W1005"),
    ("Manager", "ORG-220", "W1006"),
    ("Manager", "ORG-300", "W1004"),
    ("Manager", "ORG-310", "W1007"),
    ("Manager", "ORG-400", "W1002"),
    ("HR Partner", "ORG-200", "W1011"),
    ("HR Partner", "ORG-100", "W1013"),
    ("Compensation Partner", "ORG-100", "W1012"),
]

GROUP_MEMBERSHIPS = [
    ("HR Administrator", "W1004"),
    ("HR Administrator", "W1007"),
    ("Business Process Administrator", "W1007"),
]


def email_for(name: str) -> str:
    return name.lower().replace(" ", ".") + "@summit-analytics.example"


def seed(session: Session) -> None:
    """Insert demo data. Safe to call on an empty database only."""
    if session.scalar(select(Worker).limit(1)) is not None:
        return

    session.add_all(
        JobProfile(
            id=jp_id,
            name=name,
            management_level=level,
            pay_range_min=low,
            pay_range_mid=mid,
            pay_range_max=high,
        )
        for jp_id, name, level, low, mid, high in JOB_PROFILES
    )
    session.flush()
    for org_id, name, superior in ORGS:  # parents are listed before children
        session.add(SupervisoryOrg(id=org_id, name=name, superior_id=superior))
        session.flush()
    session.add_all(
        Worker(
            id=w_id,
            name=name,
            email=email_for(name),
            job_profile_id=jp,
            org_id=org,
            salary=salary,
            hire_date=hired,
            status="Active",
        )
        for w_id, name, jp, org, salary, hired in WORKERS
    )
    session.flush()
    session.add_all(RoleAssignment(role=r, org_id=o, worker_id=w) for r, o, w in ROLE_ASSIGNMENTS)
    session.add_all(GroupMembership(group=g, worker_id=w) for g, w in GROUP_MEMBERSHIPS)
    session.add(IdSequence(name="worker", next_value=2001))
    session.flush()
