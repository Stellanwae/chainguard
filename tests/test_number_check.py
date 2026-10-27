"""The deterministic evidence check must accept numbers that are in the evidence
(including rounding/percent/millions) and flag numbers that are not."""
from app.agent import unsupported_numbers

EVIDENCE = {"E1": {"result": {"rows": [{"revenue": 14836083.67, "revenue_share": 0.0123, "customers": 90858}]}}}


def test_accepts_supported_numbers():
    draft = {"why": ["Revenue was 14,836,083 (E1)", "share 1.23% (E1)", "14.8 million", "90,858 customers"]}
    assert unsupported_numbers(draft, EVIDENCE) == []


def test_flags_invented_numbers():
    draft = {"why": ["Revenue was 99,999,999 (E1)", "share 45%"]}
    assert set(unsupported_numbers(draft, EVIDENCE)) == {"99,999,999", "45%"}
