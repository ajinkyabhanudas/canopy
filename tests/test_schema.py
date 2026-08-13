"""Tests for src/canopy/schema.py — no external dependencies required."""

import canopy.schema as schema
from canopy.schema import SCHEMA_CONTEXT, build_system_prompt


class TestSchemaContext:
    def test_contains_all_table_names(self):
        for table in ("users", "species", "sites", "ingestion_logs",
                      "assignment_packages", "detections"):
            assert table in SCHEMA_CONTEXT, f"SCHEMA_CONTEXT missing table: {table}"

    def test_contains_all_validation_statuses(self):
        for status in ("approved", "pending"):
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

    def test_language_instruction_present(self):
        prompt = build_system_prompt()
        assert "LANGUAGE" in prompt

    def test_sql_stays_english_stated(self):
        prompt = build_system_prompt()
        assert "SQL" in prompt and "English" in prompt

    def test_language_section_covers_spanish(self):
        prompt = build_system_prompt()
        assert "Spanish" in prompt

    def test_language_instruction_is_unconditional(self):
        """build_system_prompt() takes no lang parameter — instruction is always present."""
        import inspect
        sig = inspect.signature(build_system_prompt)
        assert len(sig.parameters) == 0

    def test_forbids_markdown_tables_in_response_regardless_of_row_count(self):
        """A live run rendered a 637-row markdown table directly in the
        Response despite the pre-existing "Do NOT include the raw data
        table" instruction — that instruction wasn't explicit that this
        applies at every row count, not just large ones, and never named
        the markdown table syntax directly. Strengthened; this test pins
        the strengthened language so it can't silently regress."""
        prompt = build_system_prompt()
        assert "Never render a markdown" in prompt
        assert "Full data table" in prompt
        assert "even a 6-row" in prompt

    def test_forbids_trailing_empty_bullets_in_key_findings(self):
        """A live run rendered a trailing empty bullet ("- " with nothing
        after it) as the 4th "Key findings" list item — no instruction
        told the model every bullet must contain real content."""
        prompt = build_system_prompt()
        assert "never emit a" in prompt.lower()
        assert "empty bullet" in prompt.lower()
