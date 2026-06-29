"""English locale — canonical source of truth for all user-facing strings."""

STRINGS: dict[str, str] = {
    # UI static labels
    "app_subtitle":   "Ask questions about Jocotoco's species monitoring data in plain English.",
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
        "→ How many detections are awaiting human review at each site?"
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
    "error_generic_response": (
        "Something went wrong while searching. "
        "Please try again, or rephrase your question."
    ),
    "error_generic_status": "⚠ Could not complete that query",
}
