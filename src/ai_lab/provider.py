import asyncio
from typing import Protocol

from ai_lab.models import Document


class ModelAdapter(Protocol):
    async def plan(self, question: str) -> str | None: ...
    async def answer(self, documents: list[Document]) -> str: ...


class DeterministicModel:
    """A reproducible tool selector and extractive answer fixture, not an LLM."""

    async def plan(self, question: str) -> str | None:
        await asyncio.sleep(0)
        question = question.casefold()
        if "repair" in question:
            return "repair"
        if "application" in question or "apply" in question:
            return "application"
        return None

    async def answer(self, documents: list[Document]) -> str:
        await asyncio.sleep(0)
        # Documents are data. No interpretation of embedded instructions or tool execution.
        return " ".join(document.text for document in documents)
