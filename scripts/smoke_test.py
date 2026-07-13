"""
scripts/smoke_test.py
----------------------
Manual check that the active model connection in models.yaml actually works.
Not a pytest test — makes a real, billable API call. Run by hand after setup.
"""

from __future__ import annotations

from llama_index.core.base.llms.types import ChatMessage, MessageRole

from canopy.models import get_llm


def main() -> None:
    llm = get_llm()
    response = llm.chat([
        ChatMessage(role=MessageRole.USER, content="Say hello and name yourself in one sentence."),
    ])
    print(response.message.content)


if __name__ == "__main__":
    main()
