from typing import Any, Callable

from langchain_core.tools import tool

from storage import retrieval as storage_retrieval


def build_knowledge_tools(persona_id: str) -> list:
    @tool
    def get_similar_predictions(query_text: str, asset_key: str | None = None, k: int = 5) -> list:
        """Look up this persona's own past predictions that are semantically similar to query_text, including their direction, confidence, outcome, and reasoning."""
        return storage_retrieval.get_similar_predictions(query_text, persona_id=persona_id, asset_key=asset_key, k=k)

    @tool
    def get_relevant_lessons(query_text: str, asset_key: str | None = None, k: int = 3) -> list:
        """Look up distilled lessons relevant to query_text, learned from this persona's own settled prediction history."""
        return storage_retrieval.get_relevant_lessons(query_text, persona_id=persona_id, asset_key=asset_key, k=k)

    @tool
    def get_recent_market_trend(asset_key: str | None = None, lookback_hours: int = 24) -> list:
        """Look up recently fetched market data snapshots for an asset (or across all assets if asset_key is omitted)."""
        return storage_retrieval.get_recent_market_trend(asset_key=asset_key, lookback_hours=lookback_hours)

    @tool
    def get_latest_backtest_report() -> dict:
        """Look up this persona's most recent trailing-window performance report (win rate, per-asset breakdown, confidence calibration)."""
        return storage_retrieval.get_latest_backtest_report(persona_id) or {}

    return [get_similar_predictions, get_relevant_lessons, get_recent_market_trend, get_latest_backtest_report]


def build_skill_tools(
    skill_registry: list[dict[str, Any]],
    enabled_skills_config: dict[str, Any],
    fetch_skill: Callable[[str, str], dict[str, Any]],
    language: str,
) -> list:
    tools = []
    for entry in skill_registry:
        skill_config = enabled_skills_config.get(entry["key"])
        if not (isinstance(skill_config, dict) and skill_config.get("enabled", False)):
            continue

        def make_tool(entry: dict[str, Any] = entry):
            @tool(entry["key"], description=f"Fetch current {entry['key']} market data for this prediction.")
            def skill_tool() -> str:
                snapshot = fetch_skill(entry["fetch"], "prediction")
                lines = entry["format_lines"](snapshot, language)
                return "\n".join(lines)

            return skill_tool

        tools.append(make_tool())
    return tools
