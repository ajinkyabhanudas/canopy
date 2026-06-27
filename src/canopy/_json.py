"""Shared JSON encoder for psycopg2 types that stdlib json cannot handle."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal


class Encoder(json.JSONEncoder):
    def default(self, obj: object) -> object:
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, date):
            return obj.isoformat()
        return super().default(obj)
