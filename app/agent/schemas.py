"""The structured contract between the LLM and our code.

The final answer is constrained with the API's JSON-schema structured output AND re-validated with
Pydantic here, so a malformed answer can never reach the UI.
"""
from typing import Literal

from pydantic import BaseModel, Field

QuestionType = Literal["ranking", "comparison", "explanation", "weather_statistic",
                       "methodology", "out_of_scope", "other"]


class HubMention(BaseModel):
    hub_id: str = Field(description="Hub id exactly as returned by the tools, e.g. 'dallas'")
    score: float | None = Field(description="The score being discussed (overall or hazard), copied from tool output")
    score_type: str = Field(description="'overall' or the hazard key the score refers to, e.g. 'winter'")
    tier: str | None = Field(description="Tier label from tool output, e.g. 'High'")


class AgentAnswer(BaseModel):
    answer: str = Field(description="The answer for the analyst, in concise Markdown. Lead with the conclusion.")
    question_type: QuestionType
    hubs: list[HubMention] = Field(description="Hubs discussed, in the order they appear in the answer")
    key_drivers: list[str] = Field(description="The specific data points that drive the conclusion")
    data_sources: list[str] = Field(description="Data sources actually used for this answer")
    assumptions: list[str] = Field(description="Interpretations made, e.g. how 'last year' or 'Midwest' was read")
    caveats: list[str] = Field(description="Limitations and uncertainty the analyst should know about")
    confidence: Literal["low", "medium", "high"]
    confidence_reason: str
    suggested_followups: list[str] = Field(description="Up to 3 useful follow-up questions")


class ChatResponse(AgentAnswer):
    """What the API returns: the LLM answer plus server-side metadata."""
    session_id: str
    tool_trace: list[dict]
    validation_warnings: list[str]
    latency_ms: int
    model: str
    usage: dict


def strict_json_schema(model: type[BaseModel]) -> dict:
    """Pydantic schema -> schema accepted by structured outputs (every object closed, every field required)."""
    schema = model.model_json_schema()

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            node.pop("title", None)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return schema
