"""Unit tests for canopy.ui.app handler functions. No browser or server needed."""


import canopy.ui.app as ui_mod
from canopy.query.loop import LoopResult


def _make_result(**overrides) -> LoopResult:
    defaults = dict(
        question="How many detections?",
        sql="SELECT COUNT(*) FROM detections",
        columns=["count"],
        rows=[(5,)],
        row_count=5,
        model_text="There are 5 detections.",
    )
    return LoopResult(**{**defaults, **overrides})


# ---------------------------------------------------------------------------
# _history_choices
# ---------------------------------------------------------------------------


def test_history_choices_empty(monkeypatch):
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [])
    assert ui_mod._history_choices() == []


def test_history_choices_reversed(monkeypatch):
    entries = [
        {"question": "first"},
        {"question": "second"},
        {"question": "third"},
    ]
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: entries)
    choices = ui_mod._history_choices()
    assert choices == ["third", "second", "first"]


# ---------------------------------------------------------------------------
# _empty_result
# ---------------------------------------------------------------------------


def test_empty_result_structure():
    result = ui_mod._empty_result("some message")
    assert len(result) == 5
    sql, df, response, count_md, radio = result
    assert sql == ""
    assert count_md == ""
    assert response == "some message"


# ---------------------------------------------------------------------------
# _run_query_handler — happy path
# ---------------------------------------------------------------------------


def test_handler_empty_question(monkeypatch):
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [])
    sql, df, response, count_md, radio = ui_mod._run_query_handler("   ")
    assert sql == ""
    assert count_md == ""
    assert "Please enter a question" in response


def test_handler_valid_question(monkeypatch):
    monkeypatch.setattr(ui_mod, "run_query", lambda q: _make_result())
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [])
    sql, df, response, count_md, radio = ui_mod._run_query_handler("How many detections?")
    assert sql == "SELECT COUNT(*) FROM detections"
    assert response == "There are 5 detections."
    assert "5" in count_md


def test_handler_singular_row_count(monkeypatch):
    monkeypatch.setattr(ui_mod, "run_query", lambda q: _make_result(row_count=1, rows=[(1,)]))
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [])
    _, _, _, count_md, _ = ui_mod._run_query_handler("q")
    assert "1 row returned" in count_md
    assert "rows" not in count_md


def test_handler_plural_row_count(monkeypatch):
    monkeypatch.setattr(
        ui_mod, "run_query", lambda q: _make_result(row_count=3, rows=[(1,), (2,), (3,)])
    )
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [])
    _, _, _, count_md, _ = ui_mod._run_query_handler("q")
    assert "3 rows returned" in count_md


def test_handler_rows_converted_to_lists(monkeypatch):
    result = _make_result(rows=[(1, "a"), (2, "b")], columns=["id", "name"], row_count=2)
    monkeypatch.setattr(ui_mod, "run_query", lambda q: result)
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [])
    _, df, _, _, _ = ui_mod._run_query_handler("q")
    # Dataframe component wraps the data — check it's a Gradio component
    import gradio as gr
    assert isinstance(df, gr.Dataframe)


def test_handler_null_sql(monkeypatch):
    monkeypatch.setattr(ui_mod, "run_query", lambda q: _make_result(sql=None, rows=[], row_count=0))
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [])
    sql, _, _, _, _ = ui_mod._run_query_handler("q")
    assert sql == ""


def test_handler_updates_history(monkeypatch):
    monkeypatch.setattr(ui_mod, "run_query", lambda q: _make_result())
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [{"question": "prev q"}])
    _, _, _, _, radio = ui_mod._run_query_handler("q")
    import gradio as gr
    assert isinstance(radio, gr.Radio)


# ---------------------------------------------------------------------------
# _run_query_handler — error path
# ---------------------------------------------------------------------------


def test_handler_run_query_raises(monkeypatch):
    def _boom(q):
        raise RuntimeError("DB is down")

    monkeypatch.setattr(ui_mod, "run_query", _boom)
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [])
    sql, df, response, count_md, _ = ui_mod._run_query_handler("anything")
    assert sql == ""
    assert count_md == ""
    assert "Sorry" in response
    assert "DB is down" in response


# ---------------------------------------------------------------------------
# _clear_handler
# ---------------------------------------------------------------------------


def test_clear_handler_calls_clear_history(monkeypatch):
    called = []
    monkeypatch.setattr(ui_mod, "clear_history", lambda: called.append(True))
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [])
    ui_mod._clear_handler()
    assert called == [True]


def test_clear_handler_empties_question(monkeypatch):
    monkeypatch.setattr(ui_mod, "clear_history", lambda: None)
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [])
    radio, question = ui_mod._clear_handler()
    assert question == ""
