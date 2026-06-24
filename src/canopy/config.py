"""
config.py
---------
Single place that reads environment variables. Nothing else in this
package should call os.getenv directly, so credential handling stays
auditable in one file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class ModelConfig:
    backend: str
    api_key: str
    model: str


def get_model_config() -> ModelConfig:
    return ModelConfig(
        backend=os.environ.get("MODEL_BACKEND", "anthropic"),
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
    )
