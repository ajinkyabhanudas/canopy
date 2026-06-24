"""
scripts/smoke_test.py
----------------------
Manual check that the Anthropic API key and model name in .env actually
work. Not a pytest test, it makes a real, billable API call. Run by hand
after setup, not in CI.
"""

from __future__ import annotations

from canopy.models import get_model_client


def main() -> None:
    client = get_model_client()
    response = client.generate(
        system_prompt="Reply with exactly one short sentence.",
        messages=[{"role": "user", "content": "Say hello and name yourself."}],
    )
    print(response.text)


if __name__ == "__main__":
    main()
