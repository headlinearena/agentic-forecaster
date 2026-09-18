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


def _parse_jsonstat_time_series(data: dict[str, Any]) -> list[tuple[str, Any]] | None:
    """Flatten a Eurostat JSON-stat response into [(period, value)] -- only when every
    non-time dimension is pinned to a single category, so the flat value index maps 1:1
    onto the time axis. Returns None when the query was not filtered down to one series."""
    ids = data.get("id") or []
    sizes = data.get("size") or []
    if "time" not in ids or len(ids) != len(sizes):
        return None
    if any(size != 1 for dim_id, size in zip(ids, sizes) if dim_id != "time"):
        return None
    time_index = ((data.get("dimension") or {}).get("time") or {}).get("category", {}).get("index") or {}
    position_to_period = {int(pos): str(period) for period, pos in time_index.items()}
    observations = []
    for flat_index, value in (data.get("value") or {}).items():
        period = position_to_period.get(int(flat_index))
        if period is not None:
            observations.append((period, value))
    return sorted(observations)


def build_data_lookup_tools(
    fred_fetch: Callable[[str, dict[str, Any]], dict[str, Any]] | None,
    fred_search: Callable[[str, int], dict[str, Any]] | None,
    eurostat_fetch: Callable[[str, str], dict[str, Any]] | None,
) -> list:
    """Parameterized statistical-series lookup tools, so the agentic loop can ground a
    forecast in the target indicator's OWN recent history instead of only the fixed
    snapshot series the skill configs enumerate. Tool errors are returned as text so the
    model can correct the series id / filters and retry."""
    tools = []

    if fred_fetch is not None:

        @tool
        def fred_series_observations(series_id: str, units: str = "", limit: int = 12) -> str:
            """Fetch the most recent observations of any FRED series by id (e.g. RSAFS retail sales, HOUST housing starts, ICSA initial claims, GASREGW gasoline price), newest first. Optional units transform: pch = percent change from prior period, pc1 = percent change from a year ago."""
            params: dict[str, Any] = {"sort_order": "desc", "limit": str(max(1, min(int(limit), 60)))}
            if units.strip():
                params["units"] = units.strip()
            try:
                data = fred_fetch(series_id.strip().upper(), params)
            except Exception as exc:
                return f"FRED fetch failed for {series_id!r}: {exc}. Try fred_series_search to find a valid series id."
            lines = [
                f"- {obs.get('date')}: {obs.get('value')}"
                for obs in data.get("observations") or []
                if obs.get("value") not in (None, ".")
            ]
            if not lines:
                return f"No observations returned for {series_id!r} -- the series may be discontinued; try fred_series_search."
            header = f"{series_id.strip().upper()} latest observations (newest first"
            header += f", units={params['units']}):" if "units" in params else "):"
            return "\n".join([header, *lines])

        tools.append(fred_series_observations)

    if fred_search is not None:

        @tool
        def fred_series_search(search_text: str, limit: int = 8) -> str:
            """Search FRED by free text for series matching an economic indicator; returns series ids with title, frequency, and last observation date so you can pick a CURRENT series and fetch it with fred_series_observations."""
            try:
                data = fred_search(search_text, max(1, min(int(limit), 20)))
            except Exception as exc:
                return f"FRED search failed: {exc}"
            lines = [
                f"- {s.get('id')}: {s.get('title')} ({s.get('frequency_short')}, {s.get('units_short')},"
                f" ends {s.get('observation_end')})"
                for s in data.get("seriess") or []
            ]
            return "\n".join(lines) if lines else f"No FRED series found for {search_text!r}."

        tools.append(fred_series_search)

    if eurostat_fetch is not None:

        @tool
        def eurostat_series_observations(dataset: str, filters: str, since_period: str = "") -> str:
            """Fetch recent observations of one Eurostat series. dataset is the Eurostat dataset code (e.g. une_rt_m for monthly unemployment rates); filters is a query string pinning every non-time dimension to exactly one category (e.g. geo=EA21&age=TOTAL&sex=T&s_adj=SA&unit=PC_ACT; use age=Y_LT25 for under-25). Optional since_period like 2026-01."""
            query = filters.strip().strip("&")
            if since_period.strip():
                query += f"&sinceTimePeriod={since_period.strip()}"
            try:
                data = eurostat_fetch(dataset.strip(), query)
            except Exception as exc:
                return f"Eurostat fetch failed for {dataset!r}: {exc}"
            observations = _parse_jsonstat_time_series(data)
            if observations is None:
                return (
                    "Eurostat query matched more than one series -- pin every non-time dimension"
                    " to one category in filters (check spelling of geo/age/sex/s_adj/unit values)."
                )
            if not observations:
                return f"No observations returned for {dataset!r} with filters {filters!r}."
            lines = [f"- {period}: {value}" for period, value in observations[-24:]]
            return "\n".join([f"{dataset.strip()} observations (oldest to newest):", *lines])

        tools.append(eurostat_series_observations)

    return tools
