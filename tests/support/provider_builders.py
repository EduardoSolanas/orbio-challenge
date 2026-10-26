"""Provider-side builders shared by the unit and integration provider tests."""

from __future__ import annotations

import httpx

from gateway.schemas import KnowledgeContext, KnowledgeRecord

SOURCE = KnowledgeRecord(
    id="faq-returns",
    title="Returns and refunds",
    keywords=["return", "refund"],
    body="An unused item can be requested for return within 30 calendar days.",
)


def knowledge_context() -> KnowledgeContext:
    return KnowledgeContext(
        question="Can I return an unused item?",
        kb_version="2026-10-22",
        sources=[SOURCE],
    )


def unexpected(role: str):
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        raise AssertionError(f"unexpected {role} wire call")

    return responder
