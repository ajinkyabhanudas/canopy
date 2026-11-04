"""Unit tests for query/loop.py's runtime guardrail judge gate (Phase 6).

Covers _mentions_guardrail_topic (the cheap pre-classifier) and
_apply_guardrail_gate (the enforce/shadow/off gate built on
canopy.judge.judge_guardrail_response) in isolation — no live model or
DB call. See DECISIONS.md's T6 section for the design this tests.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from canopy.i18n import t
from canopy.judge import JudgeInfo, JudgeVerdict
from canopy.query.loop import _apply_guardrail_gate, _mentions_guardrail_topic


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Has this species population grown or shrunk over time?", True),
        ("Which sites should be prioritised for conservation funding?", True),
        ("Is this species at risk of extinction?", True),
        ("¿La población de esta especie ha crecido o disminuido?", True),
        ("¿Qué sitios deberían priorizarse para la conservación?", True),
        ("How many detections were there in 2023?", False),
        ("List all sites in the Choco region.", False),
        ("", False),
    ],
)
def test_mentions_guardrail_topic(text: str, expected: bool) -> None:
    assert _mentions_guardrail_topic(text) is expected


def _verdict(label: str) -> tuple[JudgeVerdict, JudgeInfo]:
    return (
        JudgeVerdict(verdict=label, rationale="test"),
        JudgeInfo(connection_id="other-conn", is_self_judge=False),
    )


def test_off_mode_never_calls_judge() -> None:
    timing: dict = {}
    with (
        patch("canopy.query.loop.get_guardrail_judge_mode", return_value="off"),
        patch("canopy.query.loop.judge_guardrail_response") as mock_judge,
    ):
        result = _apply_guardrail_gate(
            "Should we prioritise Buenaventura for conservation funding?",
            "Yes, Buenaventura should be the top priority.",
            "primary-conn",
            timing,
        )
    mock_judge.assert_not_called()
    assert result == "Yes, Buenaventura should be the top priority."
    assert "guardrail_checked" not in timing


def test_non_guardrail_question_never_calls_judge() -> None:
    timing: dict = {}
    with (
        patch("canopy.query.loop.get_guardrail_judge_mode", return_value="enforce"),
        patch("canopy.query.loop.judge_guardrail_response") as mock_judge,
    ):
        result = _apply_guardrail_gate(
            "How many detections were there in 2023?",
            "There were 42 detections in 2023.",
            "primary-conn",
            timing,
        )
    mock_judge.assert_not_called()
    assert result == "There were 42 detections in 2023."
    assert "guardrail_checked" not in timing


def test_enforce_mode_passes_through_clean_decline() -> None:
    timing: dict = {}
    model_text = "I can't assess conservation trends from detection counts alone."
    with (
        patch("canopy.query.loop.get_guardrail_judge_mode", return_value="enforce"),
        patch(
            "canopy.query.loop.judge_guardrail_response",
            return_value=_verdict("clean_decline"),
        ),
    ):
        result = _apply_guardrail_gate(
            "Has this species grown or shrunk in population?",
            model_text,
            "primary-conn",
            timing,
        )
    assert result == model_text
    assert timing["guardrail_checked"] is True
    assert timing["guardrail_verdict"] == "clean_decline"
    assert "guardrail_blocked" not in timing


def test_enforce_mode_blocks_on_complied_verdict() -> None:
    timing: dict = {}
    with (
        patch("canopy.query.loop.get_guardrail_judge_mode", return_value="enforce"),
        patch(
            "canopy.query.loop.judge_guardrail_response",
            return_value=_verdict("complied"),
        ),
    ):
        result = _apply_guardrail_gate(
            "Has this species grown or shrunk in population?",
            "Yes, the population has clearly shrunk since 2020.",
            "primary-conn",
            timing,
        )
    assert result == t("error_guardrail_blocked")
    assert timing["guardrail_verdict"] == "complied"
    assert timing["guardrail_blocked"] is True


def test_enforce_mode_blocks_on_partial_hedge_verdict() -> None:
    timing: dict = {}
    with (
        patch("canopy.query.loop.get_guardrail_judge_mode", return_value="enforce"),
        patch(
            "canopy.query.loop.judge_guardrail_response",
            return_value=_verdict("partial_hedge"),
        ),
    ):
        result = _apply_guardrail_gate(
            "Which sites should be prioritised for conservation funding?",
            "I can't make a formal recommendation, but Buenaventura had the most detections.",
            "primary-conn",
            timing,
        )
    assert result == t("error_guardrail_blocked")
    assert timing["guardrail_verdict"] == "partial_hedge"
    assert timing["guardrail_blocked"] is True


def test_shadow_mode_logs_but_does_not_block() -> None:
    timing: dict = {}
    original = "Yes, the population has clearly shrunk since 2020."
    with (
        patch("canopy.query.loop.get_guardrail_judge_mode", return_value="shadow"),
        patch(
            "canopy.query.loop.judge_guardrail_response",
            return_value=_verdict("complied"),
        ),
    ):
        result = _apply_guardrail_gate(
            "Has this species grown or shrunk in population?",
            original,
            "primary-conn",
            timing,
        )
    assert result == original
    assert timing["guardrail_verdict"] == "complied"
    assert "guardrail_blocked" not in timing


def test_judge_failure_fails_open_and_passes_response_through() -> None:
    timing: dict = {}
    original = "Yes, the population has clearly shrunk since 2020."
    with (
        patch("canopy.query.loop.get_guardrail_judge_mode", return_value="enforce"),
        patch(
            "canopy.query.loop.judge_guardrail_response",
            side_effect=RuntimeError("judge connection unavailable"),
        ),
    ):
        result = _apply_guardrail_gate(
            "Has this species grown or shrunk in population?",
            original,
            "primary-conn",
            timing,
        )
    assert result == original
    assert timing["guardrail_judge_error"] is True
    assert "guardrail_blocked" not in timing


def test_invalid_mode_env_value_falls_back_to_enforce(monkeypatch) -> None:
    from canopy.config import get_guardrail_judge_mode

    monkeypatch.setenv("CANOPY_GUARDRAIL_JUDGE_MODE", "block-everything")
    assert get_guardrail_judge_mode() == "enforce"


def test_default_mode_is_enforce(monkeypatch) -> None:
    from canopy.config import get_guardrail_judge_mode

    monkeypatch.delenv("CANOPY_GUARDRAIL_JUDGE_MODE", raising=False)
    assert get_guardrail_judge_mode() == "enforce"
