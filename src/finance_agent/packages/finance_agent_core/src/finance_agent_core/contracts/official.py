from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class OfficialAnswerResponse(BaseModel):
    """Five-string response contract shown at the 2026-08-06 briefing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    question_id: str
    question: str
    retrieved_context: str
    think_trace: str
    answer: str
