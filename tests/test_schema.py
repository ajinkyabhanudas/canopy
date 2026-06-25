"""Tests for src/canopy/schema.py — no external dependencies required."""

import canopy.schema as schema
from canopy.schema import SCHEMA_CONTEXT, build_system_prompt


class TestSchemaContext:
    def test_contains_all_table_names(self):
        for table in ("users", "species", "sites", "ingestion_logs",
                      "assignment_packages", "detections"):
            assert table in SCHEMA_CONTEXT, f"SCHEMA_CONTEXT missing table: {table}"

    def test_contains_all_validation_statuses(self):
        for status in ("validated_true", "validated_false", "unvalidated"):
            assert status in SCHEMA_CONTEXT, (
                f"SCHEMA_CONTEXT missing validation status: {status}"
            )

    def test_contains_core_detection_columns(self):
        for col in ("confidence", "recorded_at", "management_unit",
                    "validation_status", "latitude", "longitude"):
            assert col in SCHEMA_CONTEXT, (
                f"SCHEMA_CONTEXT missing detections column: {col}"
            )

    def test_contains_canonical_join_pattern(self):
        assert "JOIN species" in SCHEMA_CONTEXT
        assert "JOIN sites" in SCHEMA_CONTEXT

    def test_documents_what_is_not_in_db(self):
        assert "IUCN" in SCHEMA_CONTEXT
        assert "patrol" in SCHEMA_CONTEXT.lower() or "EarthRanger" in SCHEMA_CONTEXT


class TestBuildSystemPrompt:
    def test_contains_schema_context(self):
        prompt = build_system_prompt()
        assert SCHEMA_CONTEXT in prompt

    def test_contains_select_only_instruction(self):
        prompt = build_system_prompt()
        lower = prompt.lower()
        assert "select" in lower
        assert any(word in lower for word in ("only", "read-only", "no insert",
                                               "never generate insert"))

    def test_contains_guardrail_against_trend_inference(self):
        prompt = build_system_prompt()
        lower = prompt.lower()
        assert any(phrase in lower for phrase in (
            "trend", "conservation status", "population"
        ))
        assert any(phrase in lower for phrase in (
            "never", "do not", "no trend"
        ))

    def test_contains_execute_sql_tool_instruction(self):
        prompt = build_system_prompt()
        assert "execute_sql" in prompt

    def test_contains_hallucination_guard(self):
        prompt = build_system_prompt()
        lower = prompt.lower()
        assert any(phrase in lower for phrase in (
            "hallucinate", "never guess", "do not guess", "invent"
        ))

    def test_returns_non_empty_string(self):
        prompt = build_system_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 500

    def test_is_pure_function(self):
        """build_system_prompt() must return the same value on repeated calls."""
        assert build_system_prompt() == build_system_prompt()

    def test_schema_context_is_module_level_constant(self):
        """SCHEMA_CONTEXT must be a string constant, not a callable."""
        assert isinstance(schema.SCHEMA_CONTEXT, str)
        assert not callable(schema.SCHEMA_CONTEXT)
