"""English locale — canonical source of truth for all user-facing strings."""

STRINGS: dict[str, str] = {
    # UI static labels
    "app_subtitle": (
        "Ask questions about Jocotoco's species monitoring data "
        "in plain English or Spanish."
    ),
    "question_label": "Ask a question",
    "placeholder":    "e.g. How many confirmed species were detected at each reserve in 2023?",
    "run_btn":        "Run Query",
    "recent_queries": "### Recent queries",
    "clear_btn":      "Clear history",
    "tab_answer":     "Answer",
    "tab_data":       "Full data table",
    "tab_sql":        "Database query",
    "idle_prompt": (
        "Ask a question about species monitoring data.\n\n"
        "→ How many confirmed species were detected at each reserve in 2023?  \n"
        "→ Which sites had the most activity last year?  \n"
        "→ Show me all Jocotoco Antpitta detections since 2022.  \n"
        "→ How many detections are awaiting human review at each site?\n\n"
        "---\n\n"
        "**This tool cannot:**  \n"
        "✗ Assess population trends or conservation status"
        " — that requires formal scientific review  \n"
        "✗ Answer questions in languages other than English or Spanish  \n"
        "✗ Look up IUCN Red List categories — not stored in this database  \n"
        "✗ Identify species by common name (e.g. \"birds\") — use scientific names"
    ),
    # Streaming status messages
    "status_reading":       "Reading your question…",
    "status_cache_hit":     "Loading your previous result…",
    "status_understood":    "**I understood:** {intent}\n\nSearching the database…",
    "status_searching_db":  "Searching the monitoring database…",
    "status_understanding": "Understanding your question…",
    "status_refining":      "Refining the search…",
    # Detection count (loop.py — user-visible, not tool-result content)
    "found_detections_singular": "Found {n} detection — writing your answer…",
    "found_detections_plural":   "Found {n} detections — writing your answer…",
    # Result row count (app.py)
    "count_row_singular": "**{n} row returned**",
    "count_row_plural":   "**{n} rows returned**",
    # Interpretation block labels (app.py)
    "interpretation_heading":  "Interpretation",
    "interpretation_source":   "Data source",
    "interpretation_gaps":     "Gaps",
    "interpretation_gaps_none": "none",
    "interpretation_research": "Research questions",
    # Timing footer
    "timing_cached": "⚡ From your recent queries · instant",
    "timing_live":   "Answer ready in {total:.0f}s",
    # Error messages
    "error_empty_question": "Please enter a question.",
    "error_guard_response": (
        "I wasn't able to run that query safely.\n\n"
        "This sometimes happens with unusual question phrasing — "
        "try asking what's in the data rather than asking to change it.\n\n"
        "The generated query is shown in the **Database query tab** for reference."
    ),
    "error_guard_status":     "⚠ Could not complete that query — see the Answer tab",
    "error_guard_readonly": (
        "**{operation} is not permitted** — this tool can only read from the database, "
        "not modify it.\n\n"
        "Canopy is designed to retrieve and analyse species monitoring data. "
        "If you're trying to understand what's in the data, try rephrasing as a question "
        "(e.g. *'How many pending detections are there?'* rather than asking to change them).\n\n"
        "The blocked query is shown in the **Database query** tab."
    ),
    "error_guard_readonly_status": "⚠ {operation} blocked — this tool is read-only",
    "error_timeout": (
        "The database query ran for too long and was stopped.\n\n"
        "Try asking about a smaller date range, a specific site, or a single species "
        "rather than the full dataset."
    ),
    "error_iterations": (
        "This question needed too many steps to answer automatically.\n\n"
        "Try breaking it into smaller questions — for example, ask about one site or "
        "one species at a time."
    ),
    "error_db_connection": (
        "Couldn't reach the database. Please try again in a moment.\n\n"
        "If the problem persists, check that the database connection is active."
    ),
    "error_generic_response": (
        "Something went wrong while searching. "
        "Please try again, or rephrase your question."
    ),
    "error_generic_status": "⚠ Could not complete that query",
    "error_unsupported_language": (
        "Canopy currently supports questions in **English or Spanish**.\n\n"
        "Please rephrase your question in either language — the tool will respond "
        "in whichever one you use.\n\n"
        "Support for additional languages may be added in the future."
    ),
    "error_unsupported_language_status": "⚠ Language not yet supported — try English or Spanish",
    # Fuzzy name suggestions (app.py) — shown when a query returns 0 rows and
    # a close match was found for a mistyped species/site name. {label} is
    # the translated column name, looked up via t(f"fuzzy_column_{label_key}")
    # for whichever FuzzyMatch.label_key the backend returned (see
    # fuzzy_column_* keys below) — a question can have typos in more than
    # one column at once, each getting its own labeled prompt.
    "fuzzy_suggestion_prompt": "{label}: no exact match found. Did you mean one of these?",
    "fuzzy_column_species": "Species",
    "fuzzy_column_site": "Site",
}
