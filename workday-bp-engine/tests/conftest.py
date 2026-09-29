from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.engine.engine import BPEngine
from app.main import create_app

# Seed people used throughout the tests
GRACE, OMAR, RAJ, SOFIA = "W1001", "W1002", "W1003", "W1004"
MARIA, KEN, DANIEL = "W1005", "W1006", "W1007"
ALICE, TOM, PRIYA = "W1008", "W1009", "W1010"
LENA, CHEN, BEN = "W1011", "W1012", "W1013"


class FixedClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 9, 1, 9, 0, 0)

    def now(self) -> datetime:
        return self.current

    def today(self):
        return self.current.date()


class Api:
    """Small helper around TestClient so tests read like a story."""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    def request(self, method: str, path: str, user: str, body: Any = None, expect: int | None = 200):
        response = self.client.request(method, path, headers={"X-User-Id": user}, json=body)
        if expect is not None:
            assert response.status_code == expect, f"{method} {path} -> {response.status_code}: {response.text}"
        return response.json()

    def get(self, path: str, user: str, expect: int = 200):
        return self.request("GET", path, user, expect=expect)

    def post(self, path: str, user: str, body: Any = None, expect: int = 200):
        return self.request("POST", path, user, body if body is not None else {}, expect=expect)

    # ---- business process helpers ----
    def hire(self, user: str = MARIA, expect: int = 201, **overrides):
        body = {
            "name": "Jordan Blake",
            "email": "jordan.blake@summit-analytics.example",
            "job_profile_id": "JP-SE2",
            "org_id": "ORG-210",
            "salary": 110_000,
            "effective_date": "2026-10-01",
        }
        body.update(overrides)
        return self.post("/api/events/hire", user, body, expect=expect)

    def change_job(self, worker_id: str, user: str = MARIA, expect: int = 201, **overrides):
        body = {"worker_id": worker_id, "effective_date": "2026-10-01", "reason": "Merit Increase"}
        body.update(overrides)
        return self.post("/api/events/change-job", user, body, expect=expect)

    def terminate(self, worker_id: str, user: str = MARIA, expect: int = 201, **overrides):
        body = {"worker_id": worker_id, "effective_date": "2026-10-15", "reason": "Resignation"}
        body.update(overrides)
        return self.post("/api/events/termination", user, body, expect=expect)

    def inbox(self, user: str):
        return self.get("/api/tasks", user)

    def task(self, user: str, step_name: str):
        matches = [t for t in self.inbox(user) if t["step"]["name"] == step_name]
        assert matches, f"{user} has no open task '{step_name}'. Inbox: {[t['step']['name'] for t in self.inbox(user)]}"
        return matches[0]

    def approve(self, user: str, step_name: str, comment: str | None = None, expect: int = 200):
        task = self.task(user, step_name)
        return self.post(f"/api/tasks/{task['id']}/approve", user, {"comment": comment}, expect=expect)

    def submit(self, user: str, step_name: str, data: dict | None = None, comment: str | None = None):
        task = self.task(user, step_name)
        return self.post(f"/api/tasks/{task['id']}/submit", user, {"comment": comment, "data": data})

    def event(self, event_id: int, user: str = DANIEL):
        return self.get(f"/api/events/{event_id}", user)


def step_status(event: dict, name: str) -> str:
    """Status of the latest active instance of a step."""
    matches = [s for s in event["steps"] if s["name"] == name and s["active"]]
    assert matches, f"No active step named {name!r}"
    return matches[-1]["status"]


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def app(tmp_path, clock):
    application = create_app(database_url=f"sqlite:///{tmp_path / 'test.db'}", clock=clock)
    yield application
    application.state.db_engine.dispose()


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def api(client) -> Api:
    return Api(client)


@pytest.fixture
def session(app):
    with app.state.session_factory() as db_session:
        yield db_session


@pytest.fixture
def engine(app, session, clock) -> BPEngine:
    return BPEngine(session, app.state.registry, clock)
