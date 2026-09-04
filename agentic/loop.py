import logging
import time
from typing import Any, Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

MAX_AGENTIC_TURNS = 5
TOOL_CALL_MAX_RETRIES = 2
TOOL_CALL_RETRY_DELAY = 1.0


class PredictionOutput(BaseModel):
    direction: str = Field(description="bullish, bearish, or neutral")
    confidence: float = Field(description="a number between 0 and 1")
    reasoning: str = Field(description="detailed, trading-relevant rationale, at least 20 characters")
    summary: str = Field(description="a concise 1-3 sentence human-readable rationale")


class CommentOutput(BaseModel):
    content: str = Field(description="the final market comment text, 2-4 sentences")


class StrategyProposalOutput(BaseModel):
    signal_type: str = Field(
        description="one of: overconfidence, underconfidence, revision_thrash, stale_keyword_filter, low_volume, other"
    )
    rationale: str = Field(description="one paragraph explaining the signal and, if any, the proposed change")
    proposed_field: str | None = Field(
        default=None, description="exactly one whitelisted prediction_strategy field name, or null"
    )
    proposed_value_json: str | None = Field(
        default=None,
        description='the new value for proposed_field, JSON-encoded, e.g. "0.25", "2", "true", or null',
    )


class NumericForecastOutput(BaseModel):
    mean: float = Field(description="your point estimate of the actual released value, in the indicator's own unit")
    std: float = Field(
        description="your one-sigma uncertainty around mean, strictly positive -- calibrate honestly, do not"
        " default to a tiny value"
    )
    rationale: str = Field(description="the specific data and reasoning behind the estimate, at least 20 characters")


class BinaryForecastOutput(BaseModel):
    yes_probability: float = Field(
        description="your probability (0 to 1) that the stated outcome occurs; 0.5 means maximum uncertainty"
    )
    rationale: str = Field(description="the specific data and reasoning behind the probability, at least 20 characters")


class StrategyCardOutput(BaseModel):
    entry_rules: list[str] = Field(
        default_factory=list,
        description="up to 6 concrete entry rules, each a single sentence under 240 characters",
    )
    no_trade_conditions: list[str] = Field(
        default_factory=list,
        description="up to 4 conditions under which no directional bet should be made, each under 240 characters",
    )
    known_failure_modes: list[str] = Field(
        default_factory=list,
        description="up to 6 recurring mistakes observed in past settled predictions, each under 240 characters",
    )
    confidence_guidance: str = Field(
        default="",
        description="under 480 characters of guidance on how to set confidence, grounded in past calibration",
    )
    rationale: str = Field(
        description="one paragraph explaining what changed versus the previous card and which evidence motivated it"
    )


def _invoke_tool_with_retry(tool: Any, tool_call: dict) -> str:
    last_exc = None
    for attempt in range(TOOL_CALL_MAX_RETRIES):
        try:
            return tool.invoke(tool_call["args"])
        except Exception as exc:
            last_exc = exc
            if attempt < TOOL_CALL_MAX_RETRIES - 1:
                logger.warning("Tool %s failed (attempt %d/%d): %s — retrying in %.0fs",
                               tool_call["name"], attempt + 1, TOOL_CALL_MAX_RETRIES, exc, TOOL_CALL_RETRY_DELAY)
                time.sleep(TOOL_CALL_RETRY_DELAY)
    return f"Tool call failed after {TOOL_CALL_MAX_RETRIES} attempts: {last_exc}"


def run_agentic_generation(
    chat_model: BaseChatModel,
    tools: list,
    system_prompt: str,
    user_prompt: str,
    response_schema: type[BaseModel],
    on_turn: Callable[[Any], None] | None = None,
    max_turns: int = MAX_AGENTIC_TURNS,
    callbacks: list | None = None,
    metadata: dict | None = None,
    require_tool_groups: list[frozenset[str]] | None = None,
) -> dict[str, Any]:
    invoke_config: dict[str, Any] = {}
    if callbacks:
        invoke_config["callbacks"] = callbacks
    if metadata:
        invoke_config["metadata"] = metadata

    tools_by_name = {t.name: t for t in tools}
    model_with_tools = chat_model.bind_tools(tools)
    # Anthropic only honors cache_control inside content blocks (message-level
    # additional_kwargs are silently dropped by langchain_anthropic's _format_messages);
    # other providers may reject the unknown block key, so only ChatAnthropic gets it.
    # The cached system+user prefix is reused by every subsequent turn of this loop.
    if chat_model.__class__.__name__ == "ChatAnthropic":
        def _cacheable(text: str) -> Any:
            return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]
    else:
        def _cacheable(text: str) -> Any:
            return text
    messages: list[Any] = [
        SystemMessage(content=_cacheable(system_prompt)),
        HumanMessage(content=_cacheable(user_prompt)),
    ]

    # Each group is an "at least one of these" requirement; every group must be satisfied.
    required_tool_groups = [g for g in (require_tool_groups or []) if g]
    called_tool_names: set[str] = set()
    nudged = False

    for _ in range(max_turns):
        ai_message = model_with_tools.invoke(messages, config=invoke_config) if invoke_config else model_with_tools.invoke(messages)
        messages.append(ai_message)
        if on_turn is not None:
            on_turn(ai_message)
        if not ai_message.tool_calls:
            missing_groups = [g for g in required_tool_groups if not (called_tool_names & g)]
            if missing_groups and not nudged:
                nudged = True
                missing_names = sorted({name for group in missing_groups for name in group})
                messages.append(HumanMessage(
                    content=(
                        "Before finalizing, call at least one tool from each of these groups you haven't "
                        "used yet, to verify current conditions and ground your reasoning in your own "
                        "history: " + ", ".join(missing_names) + "."
                    )
                ))
                continue
            break
        for tool_call in ai_message.tool_calls:
            called_tool_names.add(tool_call["name"])
            matched_tool = tools_by_name.get(tool_call["name"])
            if matched_tool is None:
                tool_result = f"Unknown tool: {tool_call['name']}"
            else:
                tool_result = _invoke_tool_with_retry(matched_tool, tool_call)
            messages.append(ToolMessage(content=str(tool_result), tool_call_id=tool_call["id"]))

    messages.append(HumanMessage(content="Based on the above, provide your final structured response now."))
    structured_model = chat_model.with_structured_output(response_schema)
    result = structured_model.invoke(messages, config=invoke_config) if invoke_config else structured_model.invoke(messages)
    if on_turn is not None:
        on_turn(result)
    return result.model_dump()
