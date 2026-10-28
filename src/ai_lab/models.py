from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Scenario = Literal[
    "healthy", "empty", "timeout", "rate_limit", "server_error", "malformed", "model_timeout"
]
Outcome = Literal[
    "success", "no_evidence", "unsupported", "tool_error", "budget_exhausted", "deadline_exceeded"
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AskRequest(StrictModel):
    question: str = Field(min_length=1, max_length=512)
    scenario: Scenario = "healthy"
    max_steps: int = Field(default=3, ge=1, le=3)
    max_tool_calls: int = Field(default=2, ge=0, le=2)
    timeout_ms: int = Field(default=2000, ge=50, le=5000)


class Citation(StrictModel):
    document_id: str
    title: str


class Usage(StrictModel):
    input_tokens: None = None
    output_tokens: None = None


class AskResponse(StrictModel):
    outcome: Outcome
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    model_mode: Literal["deterministic-mock"] = "deterministic-mock"
    usage: Usage = Field(default_factory=Usage)
    trace_id: str
    steps: int
    tool_calls: int


class RetrievalRequest(StrictModel):
    topic: Literal["repair", "application"]
    scenario: Literal["healthy", "empty", "timeout", "rate_limit", "server_error", "malformed"] = (
        "healthy"
    )


class Document(StrictModel):
    document_id: str = Field(max_length=40)
    title: str = Field(max_length=100)
    text: str = Field(max_length=1000)


class RetrievalResponse(StrictModel):
    documents: list[Document] = Field(max_length=2)
