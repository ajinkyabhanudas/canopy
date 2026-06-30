"""
Unit tests for the shared JSON encoder in canopy/_json.py.

Encoder.default() handles two psycopg2 types that stdlib json cannot
serialize: Decimal (from numeric DB columns) and date. Silent failures
here corrupt cache.json and history.jsonl entries.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from canopy._json import Encoder


def _encode(obj: object) -> object:
    return json.loads(json.dumps(obj, cls=Encoder))


def test_decimal_converts_to_float():
    assert _encode(Decimal("3.14")) == pytest.approx(3.14)


def test_decimal_zero_converts_to_float():
    assert _encode(Decimal("0")) == 0.0


def test_decimal_large_integer():
    assert _encode(Decimal("35741")) == pytest.approx(35741.0)


def test_date_converts_to_iso_string():
    assert _encode(date(2024, 1, 15)) == "2024-01-15"


def test_date_year_boundary():
    assert _encode(date(2000, 12, 31)) == "2000-12-31"


def test_regular_types_pass_through():
    payload = {"n": 42, "s": "hello", "lst": [1, 2], "flag": True}
    assert _encode(payload) == payload


def test_nested_decimal_and_date_in_dict():
    result = _encode({"count": Decimal("100"), "recorded": date(2024, 6, 1)})
    assert result == {"count": pytest.approx(100.0), "recorded": "2024-06-01"}


def test_unknown_type_raises_type_error():
    with pytest.raises(TypeError):
        json.dumps(object(), cls=Encoder)
