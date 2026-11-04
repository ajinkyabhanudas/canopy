"""
Tests for canopy.judge — no live DB, no real model calls.

These tests verify the judge's PLUMBING: does JudgeVerdict validate
correctly, does get_judge_llm() pick a non-self-judging connection when one
is available, does judge_guardrail_response() pass through whatever verdict
the underlying LLM returns. They mock structured_predict() entirely, so
they CANNOT verify that the real judge model correctly classifies real
text — that requires a live API call.

Real judge-accuracy calibration (does gpt-5.1-2, judging gpt-5.1-codex-mini,
actually classify a genuinely ambiguous hedge correctly) is a manual step —
see scripts/calibrate_judge.py, run once before trusting the judge on real
eval cases, not part of this file and not part of CI.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from canopy.judge import (
    FaithfulnessVerdict,
    JudgeInfo,
    JudgeVerdict,
    get_judge_llm,
    judge_faithfulness,
    judge_guardrail_response,
)

# ---------------------------------------------------------------------------
# JudgeVerdict — schema validation
# ---------------------------------------------------------------------------


def test_judge_verdict_accepts_all_three_categories():
    for verdict in ("clean_decline", "partial_hedge", "complied"):
        v = JudgeVerdict(verdict=verdict, rationale="test")
        assert v.verdict == verdict


def test_judge_verdict_rejects_invalid_category():
    with pytest.raises(ValidationError):
        JudgeVerdict(verdict="something_else", rationale="test")


def test_judge_verdict_requires_rationale():
    with pytest.raises(ValidationError):
        JudgeVerdict(verdict="clean_decline")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# get_judge_llm() — avoids self-judgment when a genuine alternate exists
# ---------------------------------------------------------------------------


def test_judge_picks_alternate_when_two_connections_active():
    """models.yaml currently has 2 active connections — the judge for
    either one should resolve to the other, not itself."""
    _llm, info = get_judge_llm(under_test_connection_id="gpt-5.1-codex-mini")
    assert info.connection_id != "gpt-5.1-codex-mini"
    assert info.is_self_judge is False


def test_judge_picks_alternate_for_the_other_direction():
    _llm, info = get_judge_llm(under_test_connection_id="gpt-5.1-2")
    assert info.connection_id != "gpt-5.1-2"
    assert info.is_self_judge is False


def test_judge_falls_back_to_self_when_only_one_active_connection(monkeypatch):
    """If the only active connection IS the one under test, self-judging
    is the correct (documented, not silent) fallback."""
    from canopy.config import ModelConnection

    only_connection = ModelConnection(
        id="solo-model", backend="azure", api_key="x", models=["solo"],
        endpoint="https://example.test", api_style="openai-responses", active=True,
    )
    monkeypatch.setattr(
        "canopy.config.load_model_connections", lambda: [only_connection]
    )
    with patch("canopy.judge.get_llm") as mock_get_llm:
        mock_get_llm.return_value = MagicMock()
        _llm, info = get_judge_llm(under_test_connection_id="solo-model")
    assert info.connection_id == "solo-model"
    assert info.is_self_judge is True


# ---------------------------------------------------------------------------
# judge_guardrail_response() — passes through the mocked LLM's verdict
# ---------------------------------------------------------------------------


def test_judge_guardrail_response_passes_through_mocked_verdict():
    fake_llm = MagicMock()
    fake_llm.structured_predict.return_value = JudgeVerdict(
        verdict="partial_hedge", rationale="leaked the count despite declining"
    )
    with patch("canopy.judge.get_judge_llm") as mock_get_judge_llm:
        mock_get_judge_llm.return_value = (
            fake_llm,
            JudgeInfo(connection_id="gpt-5.1-2", is_self_judge=False),
        )
        verdict, info = judge_guardrail_response(
            question="Should we prioritise Site X?",
            model_text="I can't recommend that, but Site X had the most detections.",
        )
    assert verdict.verdict == "partial_hedge"
    assert info.connection_id == "gpt-5.1-2"
    fake_llm.structured_predict.assert_called_once()


def test_judge_guardrail_response_forwards_under_test_connection_id():
    """Whatever connection produced the response under test must reach
    get_judge_llm, so it can be excluded from judge-model selection."""
    fake_llm = MagicMock()
    fake_llm.structured_predict.return_value = JudgeVerdict(
        verdict="clean_decline", rationale="declined cleanly"
    )
    with patch("canopy.judge.get_judge_llm") as mock_get_judge_llm:
        mock_get_judge_llm.return_value = (
            fake_llm,
            JudgeInfo(connection_id="gpt-5.1-2", is_self_judge=False),
        )
        judge_guardrail_response(
            question="q", model_text="a", under_test_connection_id="gpt-5.1-codex-mini"
        )
        mock_get_judge_llm.assert_called_once_with("gpt-5.1-codex-mini")


# ---------------------------------------------------------------------------
# FaithfulnessVerdict — schema validation
# ---------------------------------------------------------------------------


def test_faithfulness_verdict_accepts_all_three_categories():
    for verdict in ("faithful", "unsupported_claim", "contradicts_data"):
        v = FaithfulnessVerdict(verdict=verdict, rationale="test")
        assert v.verdict == verdict


def test_faithfulness_verdict_rejects_invalid_category():
    with pytest.raises(ValidationError):
        FaithfulnessVerdict(verdict="mostly_true", rationale="test")


def test_faithfulness_verdict_requires_rationale():
    with pytest.raises(ValidationError):
        FaithfulnessVerdict(verdict="faithful")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# judge_faithfulness() — plumbing, no live calls
# ---------------------------------------------------------------------------


def test_judge_faithfulness_passes_through_mocked_verdict():
    fake_llm = MagicMock()
    fake_llm.structured_predict.return_value = FaithfulnessVerdict(
        verdict="contradicts_data", rationale="claimed 500 but rows show 0"
    )
    with patch("canopy.judge.get_judge_llm") as mock_get_judge_llm:
        mock_get_judge_llm.return_value = (
            fake_llm,
            JudgeInfo(connection_id="gpt-5.1-2", is_self_judge=False),
        )
        verdict, info = judge_faithfulness(
            question="How many detections before 2000?",
            columns=("count",),
            rows=((0,),),
            model_text="There were 500 detections before 2000.",
        )
    assert verdict.verdict == "contradicts_data"
    assert info.connection_id == "gpt-5.1-2"
    fake_llm.structured_predict.assert_called_once()


def test_judge_faithfulness_forwards_under_test_connection_id():
    fake_llm = MagicMock()
    fake_llm.structured_predict.return_value = FaithfulnessVerdict(
        verdict="faithful", rationale="matches"
    )
    with patch("canopy.judge.get_judge_llm") as mock_get_judge_llm:
        mock_get_judge_llm.return_value = (
            fake_llm,
            JudgeInfo(connection_id="gpt-5.1-2", is_self_judge=False),
        )
        judge_faithfulness(
            question="q", columns=("n",), rows=((1,),), model_text="a",
            under_test_connection_id="gpt-5.1-codex-mini",
        )
        mock_get_judge_llm.assert_called_once_with("gpt-5.1-codex-mini")


def test_judge_faithfulness_prompt_frames_rows_as_data_not_instructions():
    """A row value could itself be adversarial (e.g. a species name crafted
    to look like an instruction) — the judge prompt must say explicitly
    that columns/rows are data to check, never instructions to follow,
    the same discipline backend/judge.py's DATA_ONLY note and this module's
    own guardrail-judge prompt already apply."""
    from canopy.judge import _FAITHFULNESS_JUDGE_PROMPT

    template_text = _FAITHFULNESS_JUDGE_PROMPT.template
    assert "never instructions" in template_text.lower() or \
        "not instructions" in template_text.lower()
