import pytest

from app.engine.conditions import ConditionError, compile_condition, evaluate, is_true

CTX = {
    "data": {"salary": 150_000, "job_profile_id": "JP-SE2", "effective_date": "2026-10-01", "reason": "Promotion"},
    "worker": {"salary": 120_000, "hire_date": "2022-08-01", "management_level": 6},
    "job_profile": {"pay_range_min": 95_000, "pay_range_mid": 117_000, "pay_range_max": 140_000, "management_level": 6},
    "current_job_profile": None,
    "pay_change_pct": 25.0,
    "initiator": {"id": "W1005"},
    "approver": {"management_level": 3},
}


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("data.salary >= 150000", True),
        ("data.salary > job_profile.pay_range_max", True),
        ("data.salary < job_profile.pay_range_min or data.salary > job_profile.pay_range_max", True),
        ("pay_change_pct > 10 and pay_change_pct <= 25", True),
        ("95000 <= data.salary <= 140000", False),
        ("data.reason in ['Promotion', 'Transfer']", True),
        ("data.reason not in ('Layoff',)", True),
        ("not (approver.management_level < 3)", True),
        ("data.effective_date < worker.hire_date", False),  # ISO dates compare as strings
        ("(data.salary - worker.salary) / worker.salary * 100 == pay_change_pct", True),
    ],
)
def test_expressions(expression, expected):
    assert is_true(expression, CTX) is expected


def test_missing_values_do_not_match_ordering_comparisons():
    # current_job_profile is None, so any ordering comparison on it is False
    assert is_true("current_job_profile.management_level > 1", CTX) is False
    assert is_true("current_job_profile.management_level < 99", CTX) is False
    assert is_true("current_job_profile.management_level == None", CTX) is True
    assert evaluate("data.missing_field", CTX) is None


def test_empty_condition_is_true():
    assert is_true(None, CTX) is True


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('echo hacked')",  # function call
        "data.__class__",  # dunder attribute
        "data['salary']",  # subscript
        "[x for x in data]",  # comprehension
        "lambda: 1",
        "data.salary ** 1000000",  # exponent not allowed
        "open('/etc/passwd')",
        "salary > 10",  # unknown root name (typo protection)
        "data.salary >",  # syntax error
        "",
    ],
)
def test_rejects_unsafe_or_invalid_expressions(expression):
    with pytest.raises(ConditionError):
        compile_condition(expression)


def test_division_by_zero_yields_none_instead_of_crashing():
    assert evaluate("data.salary / 0", CTX) is None


def test_type_errors_are_reported_as_condition_errors():
    with pytest.raises(ConditionError):
        evaluate("data.reason > 5", CTX)
