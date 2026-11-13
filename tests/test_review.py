from __future__ import annotations

import pytest

from quote_sign.checks import (
    EXTRACT_SYSTEM, REVIEW_SYSTEM, build_extract_user, build_review_user,
    parse_review, review_notes,
)


def test_blockers_and_notes_separate():
    data = {"verdict": "fail", "findings": [
        {"clause": "付款", "severity": "blocker", "reason": "付款期限矛盾。"},
        {"clause": "交付", "severity": "note", "reason": "建議補充交付方式。"},
    ]}
    issues = parse_review(data)
    assert len(issues) == 1 and issues[0].layer == "clauses" and issues[0].code == "blocker"
    assert issues[0].message == "付款：付款期限矛盾。"
    assert review_notes(data) == ["交付：建議補充交付方式。"]


def test_pass():
    assert parse_review({"verdict": "pass", "findings": []}) == []


def test_fail_without_explicit_blocker():
    issues = parse_review({"verdict": "fail", "findings": []})
    assert len(issues) == 1 and issues[0].code == "blocker"
    assert issues[0].message == "條款審查未通過"


@pytest.mark.parametrize("data", [None, [], {}, {"verdict": "unknown", "findings": []},
                                 {"verdict": "pass"}, {"verdict": "pass", "findings": {}},
                                 {"verdict": "pass", "findings": [None]},
                                 {"verdict": "pass", "findings": [{"severity": "blocker"}]},
                                 {"verdict": "pass", "findings": [
                                     {"severity": "other", "clause": "付款", "reason": "原因"}]},
                                 {"verdict": "pass", "findings": [
                                     {"severity": "note", "clause": "付款", "reason": " "}]}])
def test_malformed_review_fails_closed(data):
    issues = parse_review(data)
    assert len(issues) == 1 and issues[0].code == "bad_review"
    assert review_notes(data) == []


def test_prompt_preserves_document_as_data(parsed):
    assert build_extract_user(parsed) == parsed.markdown
    assert build_review_user(parsed) == parsed.markdown
    assert "DATA" in EXTRACT_SYSTEM and "DATA" in REVIEW_SYSTEM
    assert "Never recompute, never correct" in EXTRACT_SYSTEM
    assert "null" in EXTRACT_SYSTEM and "YYYY-MM-DD" in EXTRACT_SYSTEM
    assert "Traditional Chinese" in REVIEW_SYSTEM
