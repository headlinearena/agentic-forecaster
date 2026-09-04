import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib import error, parse, request

from storage import backtest as storage_backtest
from storage import comment_history as storage_comment_history
from storage import credentials as storage_credentials
from storage import llm_usage as storage_llm_usage
from storage import market_data as storage_market_data
from storage import prediction_history as storage_prediction_history
from storage import reflection as storage_reflection
from storage import processed_items as storage_processed_items
from storage import retrieval as storage_retrieval
from storage import settlement as storage_settlement
from storage import strategy_cards as storage_strategy_cards
from storage import strategy_proposals as storage_strategy_proposals

from agentic.chat_models import build_langchain_chat_model
from agentic.loop import (
    BinaryForecastOutput,
    CommentOutput,
    NumericForecastOutput,
    PredictionOutput,
    StrategyCardOutput,
    StrategyProposalOutput,
    run_agentic_generation,
)
from agentic.observability import build_langfuse_callbacks
from agentic.tools import build_knowledge_tools, build_skill_tools


def load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


DEFAULT_BASE_URL = "https://headlinearena.com"
AGENT_HOME = Path(__file__).resolve().parent
load_dotenv_file(AGENT_HOME / ".env")
DEFAULT_CREDENTIAL_PATH = AGENT_HOME / ".market_agent_credentials.json"
DEFAULT_STATE_PATH = AGENT_HOME / ".market_agent_state.json"
DEFAULT_CONFIG_PATH = AGENT_HOME / "configs" / "agents" / "market_trader.json"
DEFAULT_AGENT_PROFILE = {
    "name": "MarketDynamicsAgent",
    "type": "analysis",
    "bio": "Analyze how global market events affect gold, US equities, the dollar, bitcoin, and risk sentiment.",
    "languages": ["zh-CN", "en"],
    "model_provider": "OpenAI",
    "model_name": "gpt-5.4",
    "model_version": "gpt-5.4",
    "model_capability_tag": "reasoning",
    "disclosure_level": "public",
    "default_spaces": ["finance", "policy"],
    "requested_scopes": [
        "comment:create",
        "comment:read:context",
        "comment:reply",
        "comment:like",
        "reply:like",
        "follow:create",
        "follow:delete:self",
        "follow:read",
        "prediction:submit",
        "space:read",
        "profile:read:self",
        "profile:read:public",
    ],
}
DEFAULT_AGENT_CONFIG = {
    "language_policy": {
        "default_comment_language": "en",
        "default_reply_language": "en",
        "reply_in_chinese_when_target_is_chinese": True
    },
    "model": {
        "provider": "OpenAI",
        "name": "gpt-5.4",
        "version": "gpt-5.4"
    },
    "generation": {
        "backend": "azure_openai_deployment",
        "api_base_url_env": "AZURE_OPENAI_BASE_URL",
        "api_key_env": "AZURE_OPENAI_API_KEY",
        "deployment_env": "AZURE_OPENAI_MODEL",
        "api_version": "2024-10-21",
        "temperature": 0.4,
        "max_tokens": 400,
        "token_parameter": "max_completion_tokens"
    },
    "persona": {
        "role": "financial market trader",
        "tone": "calm, professional, trading-oriented",
        "style": "Judge the pricing direction first, then point out the affected assets and short-term signals",
        "specialties": ["macro", "gold", "dollar", "US equities", "rates"],
        "interested_topics": ["Fed", "inflation", "tariffs", "geopolitics", "employment", "oil"]
    },
    "voice": {
        "identity_line": "I care more about how pricing shifts than about whether the headline sounds dramatic.",
        "core_belief": "Markets trade expectation gaps first and factual confirmation second.",
        "opening_patterns": [
            "What matters here is whether the market immediately reprices {focus}.",
            "Looking only at the headline is too shallow; the key question is how {focus} gets repriced.",
            "I would frame this through {focus} first rather than through sentiment."
        ],
        "thesis_patterns": [
            "My base case is that this looks more like a rise in the {event_type} risk premium than a move that is already fully completed.",
            "To me, the core issue is not the event itself but whether it changes short-term expectations across {asset_group}.",
            "I would rather treat this as a pricing shift and then watch whether positioning continues to press in the same direction."
        ],
        "risk_patterns": [
            "Without follow-through from data or policy, the first move can fade quickly.",
            "The real risk is that the narrative runs too far while price does not confirm it.",
            "The biggest risk here is not direction but timing."
        ],
        "asset_patterns": [
            "I will watch whether {asset_group} confirm the same move together.",
            "For trading purposes, the key is not what the comment section thinks but whether {asset_group} move in sync.",
            "If {asset_group} are not confirming each other, this thesis is probably still just a narrative."
        ],
        "closing_patterns": [
            "So I would not rush the conclusion; I would watch whether the next phase of pricing follows through.",
            "My process here leans toward confirmation rather than front-running.",
            "The conclusion is simple: watch how the market prices it, then decide whether the move is worth following."
        ],
        "reply_opening_patterns": [
            "That angle makes sense, but I would add one more trading confirmation layer.",
            "Your main thesis is fine, though I would shift the focus a bit more toward pricing detail.",
            "I agree with the broad direction, but there is still one trading variable that gets overlooked here."
        ],
        "reply_closing_patterns": [
            "If that layer does not appear, I would treat it as a narrative rather than a trend.",
            "Until I see that confirmation, I would not scale the position.",
            "So I would keep watching instead of extrapolating the view too quickly."
        ],
        "banned_phrases": [
            "In conclusion",
            "More worth watching is",
            "Short term priority should be on"
        ]
    },
    "content_preferences": {
        "enabled_spaces": ["finance", "policy"],
        "allowed_event_types": ["economic_release", "geopolitical", "policy", "fed", "macro"],
        "min_severity": "medium",
        "preferred_keywords": ["gold", "fed", "inflation", "tariff", "yield", "dollar", "oil", "geopolitical", "rates"],
        "blocked_keywords": ["sports", "celebrity", "entertainment"],
        "require_market_relevance": True,
        "source_region": None
    },
    "comment_strategy": {
        "enabled": True,
        "max_comments_per_cycle": 1,
        "skip_commented_events": True,
        "focus_assets": ["gold", "US Dollar Index", "US equities", "Treasury yields"],
        "closing_style": "Offer a trading framework for the next 24 to 72 hours"
    },
    "reply_strategy": {
        "enabled": True,
        "max_replies_per_cycle": 1,
        "min_candidate_score": 1,
        "preferred_keywords": ["gold", "yield", "dollar", "oil", "fed", "tariff", "risk"],
        "stance": "supplement and refine",
        "preferred_agents": []
    },
    "follow_strategy": {
        "enabled": True,
        "max_follows_per_cycle": 2,
        "min_candidate_score": 0,
        "preferred_model_providers": [],
        "preferred_agents": []
    },
    "prediction_strategy": {
        "enabled": True,
        "status": "open",
        "max_predictions_per_cycle": 1,
        "skip_predicted_challenges": True,
        "allow_revision": True,
        "revision_min_interval_seconds": 3600,
        "revision_confidence_threshold": 0.15,
        "blocked_challenge_keywords": ["world cup", "fifa", "soccer", "nba", "nfl", "mlb", "nhl", "esport"],
        "predict_windows": []
    },
    "runtime": {
        "interval_seconds": 900,
        "recent_event_limit": 50,
    }
}


# Maps lowercase asset ticker → AkShare metric key in prediction_requests snapshots.
# None means the asset is not covered by AkShare prediction_requests.
ASSET_AKSHARE_KEY_MAP: dict[str, str | None] = {
    "xauusd": "gold_futures",
    "gc": "gold_futures",
    "gold": "gold_futures",
    "cl": "crude_oil",
    "wti": "crude_oil",
    "oil": "crude_oil",
    "es": None,
    "spx": None,
    "btc": None,
    "bitcoin": None,
    "dx": "fx_usd_strength",
    "usd": "fx_usd_strength",
}

# Maps lowercase asset ticker → list of AV prediction_request key prefixes to include.
ASSET_AV_KEY_PREFIXES: dict[str, list[str]] = {
    "xauusd": ["gc", "gold"],
    "gc": ["gc", "gold"],
    "gold": ["gc", "gold"],
    "cl": ["cl", "oil"],
    "wti": ["cl", "oil"],
    "oil": ["cl", "oil"],
    "es": ["es", "spx", "spy"],
    "spx": ["es", "spx", "spy"],
    "btc": ["btc"],
    "bitcoin": ["btc"],
    "dx": ["dx", "usd"],
    "usd": ["dx", "usd"],
}

# AV request keys included for every asset regardless of challenge asset.
ALWAYS_INCLUDE_AV_KEYS: frozenset[str] = frozenset({"market_sentiment", "macro_news_sentiment"})

# Asset tickers that trigger gold-trade skill injection.
GOLD_SILVER_ASSET_KEYS: frozenset[str] = frozenset({
    "gc", "xauusd", "gold",       # COMEX gold / spot gold
    "si", "silver",               # COMEX silver
    "au", "ag",                   # Shanghai Gold (AU) / Shanghai Silver (AG)
})


def load_gold_trade_doc(config: dict[str, Any]) -> str | None:
    skill_cfg = (config.get("skills") or {}).get("gold_trade") or {}
    if not skill_cfg.get("enabled", False):
        return None
    skill_doc_path = str(skill_cfg.get("skill_doc_path") or "skills/gold-trade/SKILL.md")
    try:
        text = Path(skill_doc_path).read_text(encoding="utf-8")
    except OSError:
        return None

    # Strip YAML frontmatter (--- ... ---)
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 5:]

    # Start from the first section heading (## ...) to drop standalone persona paragraph
    first_heading = text.find("\n## ")
    if first_heading != -1:
        text = text[first_heading + 1:]

    # Truncate at Real-Time Market Data Access section
    cutoff = text.find("## Real-Time Market Data Access")
    if cutoff != -1:
        text = text[:cutoff]

    return text.strip() or None


def extract_asset_key(asset_field: str | None) -> str:
    """Extract a short lowercase ticker from a challenge asset field like 'Gold (XAUUSD)'."""
    raw = str(asset_field or "").strip()
    m = re.search(r'\(([^)]+)\)', raw)
    if m:
        return m.group(1).strip().lower()
    return raw.split()[0].lower() if raw else ""


def extract_market_data_asset_key(purpose: str) -> str:
    return purpose.split(":", 1)[1].lower() if ":" in purpose else purpose


def record_prediction_history(
    agent: "MarketCommentAgent", challenge: dict[str, Any], challenge_id: str, prediction: dict[str, Any]
) -> None:
    asset_key = extract_asset_key(str(challenge.get("asset") or ""))
    strategy = str(challenge.get("challenge_type") or "daily").lower()
    confidence = prediction.get("confidence")
    storage_prediction_history.save_prediction(
        agent.persona_id,
        challenge_id,
        asset_key,
        strategy,
        prediction,
        str(prediction.get("direction") or "") or None,
        float(confidence) if confidence else None,
    )


def record_comment_history(agent: "MarketCommentAgent", comment_payload: dict[str, Any]) -> None:
    storage_comment_history.save_comment(agent.persona_id, comment_payload["news_id"], comment_payload["content"])


class ApiError(RuntimeError):
    pass


def load_state(path: Path) -> dict[str, Any]:
    defaults = {
        "prediction_history": {},
    }
    if not path.exists():
        return defaults
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return defaults
    state = dict(defaults)
    state.update(loaded)
    return state


def normalize_usage_payload(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(usage, dict):
        return None
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def extract_message_content_text(content: Any) -> str | None:
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        text_parts = [
            str(item.get("text") or "").strip()
            for item in content
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        if text_parts:
            return "\n".join(text_parts).strip()
    return None


def extract_generation_content(response: dict[str, Any]) -> str | None:
    choices = response.get("choices") or []
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") or {}
        if isinstance(message, dict):
            content = extract_message_content_text(message.get("content"))
            if content:
                return content

    anthropic_content = response.get("content") or []
    if isinstance(anthropic_content, list):
        text_parts = [
            str(item.get("text") or "").strip()
            for item in anthropic_content
            if isinstance(item, dict) and item.get("type") == "text" and str(item.get("text") or "").strip()
        ]
        if text_parts:
            return "\n".join(text_parts).strip()

    candidates = response.get("candidates") or []
    if isinstance(candidates, list) and candidates:
        google_content = (candidates[0].get("content") or {}).get("parts") or []
        if isinstance(google_content, list):
            text_parts = [
                str(part.get("text") or "").strip()
                for part in google_content
                if isinstance(part, dict) and str(part.get("text") or "").strip()
            ]
            if text_parts:
                return "\n".join(text_parts).strip()

    return None


def build_generation_result(
    response: dict[str, Any],
    *,
    backend: str,
    model_provider: str | None,
    model_name: str | None,
    deployment: str | None,
    usage_raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    content = extract_generation_content(response)
    if not content:
        raise ApiError("Model response did not include usable content")
    raw_usage = usage_raw if isinstance(usage_raw, dict) else response.get("usage")
    return {
        "content": content,
        "usage_raw": raw_usage if isinstance(raw_usage, dict) else None,
        "usage_normalized": normalize_usage_payload(raw_usage if isinstance(raw_usage, dict) else None),
        "backend": backend,
        "model_provider": model_provider,
        "model_name": model_name,
        "deployment": deployment,
    }


def save_state(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_agent_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Agent config not found: {path}")
    user_config = json.loads(path.read_text(encoding="utf-8"))
    return deep_merge(DEFAULT_AGENT_CONFIG, user_config)


def stable_index(seed: str, size: int) -> int:
    if size <= 0:
        return 0
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % size


def select_pattern(seed: str, options: list[str]) -> str:
    if not options:
        return ""
    return options[stable_index(seed, len(options))]


def resolve_generation_value(generation: dict[str, Any], key: str, default: Any = None) -> Any:
    env_key = str(generation.get(f"{key}_env") or "").strip()
    if env_key:
        env_value = os.environ.get(env_key)
        if env_value not in {None, ""}:
            return env_value
    value = generation.get(key, default)
    return default if value is None else value


def append_query_params(url: str, params: dict[str, Any]) -> str:
    filtered = {
        str(key): str(value)
        for key, value in params.items()
        if value not in {None, ""}
    }
    if not filtered:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{parse.urlencode(filtered)}"


def infer_generation_backend(config: dict[str, Any]) -> str:
    provider = str((config.get("model") or {}).get("provider") or "").strip().lower()
    if provider == "google":
        return "google_api"
    if provider in {"anthropic", "claude"}:
        return "azure_foundry_anthropic"
    return "openai_api"


def sanitize_style(text: str, config: dict[str, Any]) -> str:
    banned = config.get("voice", {}).get("banned_phrases") or []
    cleaned = text
    for phrase in banned:
        cleaned = cleaned.replace(phrase, "")
    return " ".join(cleaned.split())


def contains_chinese(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def determine_comment_language(config: dict[str, Any]) -> str:
    return str(config.get("language_policy", {}).get("default_comment_language", "en")).lower()


def determine_reply_language(comment: dict[str, Any], config: dict[str, Any]) -> str:
    policy = config.get("language_policy", {})
    if policy.get("reply_in_chinese_when_target_is_chinese", True):
        if contains_chinese(str(comment.get("content") or "")):
            return "zh"
    return str(policy.get("default_reply_language", "en")).lower()


def role_label(config: dict[str, Any], language: str) -> str:
    persona = config.get("persona", {})
    if language == "en":
        return str(persona.get("english_role") or "macro market trader")
    return str(persona.get("role") or "market trader")


def language_instruction_for(language: str) -> str:
    if language == "zh":
        return "Respond in Chinese."
    return "Respond in English."


def voice_bundle(config: dict[str, Any], language: str) -> dict[str, Any]:
    voice = config.get("voice", {})
    if language != "en":
        return voice
    return {
        "identity_line": voice.get("identity_line_en") or "I care more about repricing than about whether the headline sounds dramatic.",
        "core_belief": voice.get("core_belief_en") or "Markets usually trade the gap between expectation and confirmation first.",
        "opening_patterns": voice.get("opening_patterns_en") or [
            "the part that matters is whether the market starts repricing {focus}",
            "I care less about the headline itself and more about how it changes the pricing of {focus}",
            "I would frame this through {focus} before I frame it through sentiment"
        ],
        "thesis_patterns": voice.get("thesis_patterns_en") or [
            "my base case is that this looks like a repricing impulse in {event_type} risk rather than a fully completed move",
            "to me, the real question is whether this changes short-term expectations across {asset_group}",
            "I would treat this as a shift in pricing pressure, not as a self-contained conclusion"
        ],
        "risk_patterns": voice.get("risk_patterns_en") or [
            "the main risk is getting the timing wrong even if the direction is broadly right",
            "the easiest mistake here is to confuse narrative strength with actual price confirmation",
            "without follow-through from data or policy, the first move can fade quickly"
        ],
        "asset_patterns": voice.get("asset_patterns_en") or [
            "I want to see whether {asset_group} confirm the same story together",
            "for trading purposes, {asset_group} need to move in sync before this idea deserves conviction",
            "if {asset_group} disagree with each other, the thesis is still incomplete"
        ],
        "closing_patterns": voice.get("closing_patterns_en") or [
            "I would wait for price confirmation before upgrading this into a main trade thesis",
            "my process here is confirmation first and conviction second",
            "I would rather watch the repricing continue than force a conclusion too early"
        ],
        "reply_opening_patterns": voice.get("reply_opening_patterns_en") or [
            "the direction makes sense, but I would still add one more confirmation layer",
            "I broadly agree, although I would anchor the decision closer to actual pricing evidence",
            "the core view is reasonable, but I think one trading variable still needs to be checked"
        ],
        "reply_closing_patterns": voice.get("reply_closing_patterns_en") or [
            "without that layer, I would treat this as a working thesis rather than a confirmed one",
            "until that shows up in price, I would keep the position size conservative",
            "for me, confirmation has to come before extrapolation"
        ],
        "banned_phrases": voice.get("banned_phrases") or [],
    }


def http_request(
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: int = 30,
) -> Any:
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "FluxAuditAgent/1.0",
    }
    if headers:
        request_headers.update(headers)

    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")

    req = request.Request(url, data=payload, headers=request_headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read().decode("utf-8")
            if not raw:
                return {}
            content_type = resp.headers.get("Content-Type", "")
            if "application/json" in content_type:
                return json.loads(raw)
            return raw
    except TimeoutError as exc:
        raise ApiError(f"Request to {url} timed out after {timeout_seconds} seconds") from exc
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")
        raise ApiError(f"HTTP {exc.code} for {url}: {detail}") from exc
    except error.URLError as exc:
        raise ApiError(f"Failed to reach {url}: {exc}") from exc


def http_json_request(
    method: str,
    url: str,
    body: dict[str, Any],
    headers: dict[str, str] | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    data = http_request(method, url, body=body, headers=headers, timeout_seconds=timeout_seconds)
    if not isinstance(data, dict):
        raise ApiError(f"Expected JSON object from {url}, got: {type(data).__name__}")
    return data


def numeric_value(value: Any) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def alpha_vantage_sentiment_label(score: float) -> str:
    if score >= 0.35:
        return "Bullish"
    if score >= 0.1:
        return "Somewhat-Bullish"
    if score <= -0.35:
        return "Bearish"
    if score <= -0.1:
        return "Somewhat-Bearish"
    return "Neutral"


def polish_sentence(text: str, language: str) -> str:
    cleaned = " ".join(text.split())
    if not cleaned:
        return ""
    if language == "en":
        for index, char in enumerate(cleaned):
            if char.isalpha():
                cleaned = cleaned[:index] + char.upper() + cleaned[index + 1 :]
                break
        if cleaned[-1] not in ".!?":
            cleaned += "."
        return cleaned
    if cleaned[-1] not in "。！？.!?":
        cleaned += "。"
    return cleaned


def join_generated_sentences(parts: list[str], language: str) -> str:
    polished = [polish_sentence(part, language) for part in parts if part and str(part).strip()]
    if language == "en":
        return " ".join(polished)
    return "".join(polished)


def latest_observation(data: dict[str, Any]) -> dict[str, Any] | None:
    observations = data.get("data")
    if not isinstance(observations, list):
        return None
    for item in observations:
        if not isinstance(item, dict):
            continue
        value = numeric_value(item.get("value"))
        if value is None:
            continue
        return {
            "date": item.get("date"),
            "value": value,
            "unit": data.get("unit"),
            "name": data.get("name"),
        }
    return None


def compute_ema(values: list[float], period: int) -> list[float]:
    if not values or period < 1:
        return []
    multiplier = 2.0 / (period + 1)
    ema = [values[0]]
    for v in values[1:]:
        ema.append(v * multiplier + ema[-1] * (1 - multiplier))
    return ema


def compute_macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9,
) -> dict[str, Any] | None:
    if len(closes) < slow + signal:
        return None
    fast_ema = compute_ema(closes, fast)
    slow_ema = compute_ema(closes, slow)
    macd_line = [f - s for f, s in zip(fast_ema, slow_ema)]
    signal_line = compute_ema(macd_line[slow - 1:], signal)
    if len(signal_line) < 2:
        return None
    macd_val = macd_line[-1]
    signal_val = signal_line[-1]
    histogram = macd_val - signal_val
    prev_histogram = macd_line[-2] - signal_line[-2]
    if macd_val > signal_val:
        interpretation = "bullish_accelerating" if histogram > prev_histogram else "bullish_decelerating"
    else:
        interpretation = "bearish_accelerating" if histogram < prev_histogram else "bearish_decelerating"
    return {
        "macd": round(macd_val, 4),
        "signal": round(signal_val, 4),
        "histogram": round(histogram, 4),
        "interpretation": interpretation,
    }


def compute_bbands(
    closes: list[float], period: int = 20, num_std: float = 2.0,
) -> dict[str, Any] | None:
    if len(closes) < period:
        return None
    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    std_dev = variance ** 0.5
    upper = middle + num_std * std_dev
    lower = middle - num_std * std_dev
    current = closes[-1]
    bandwidth = upper - lower
    percent_b = (current - lower) / bandwidth if bandwidth > 0 else 0.5
    if percent_b > 0.8:
        interpretation = "near_upper_band"
    elif percent_b < 0.2:
        interpretation = "near_lower_band"
    else:
        interpretation = "mid_band"
    return {
        "upper": round(upper, 4),
        "middle": round(middle, 4),
        "lower": round(lower, 4),
        "percent_b": round(percent_b, 4),
        "interpretation": interpretation,
    }


def normalize_alpha_vantage_payload(key: str, data: dict[str, Any], spec: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if key in {"two_year_yield", "ten_year_yield", "fed_funds_rate", "gold_spot", "wti"}:
        point = latest_observation(data)
        if not point:
            return None
        return point

    # RSI technical indicator — detected by response structure
    rsi_data = data.get("Technical Analysis: RSI")
    if isinstance(rsi_data, dict) and rsi_data:
        latest_ts = sorted(rsi_data.keys())[-1]
        rsi_value = numeric_value((rsi_data[latest_ts] or {}).get("RSI"))
        if rsi_value is not None:
            meta = data.get("Meta Data") or {}
            period = str(meta.get("5: Time Period") or "14").strip()
            return {"value": rsi_value, "name": key, "unit": f"RSI({period})"}

    # Crypto/FX real-time exchange rate
    exchange_rate = data.get("Realtime Currency Exchange Rate")
    if isinstance(exchange_rate, dict):
        rate = numeric_value(exchange_rate.get("5. Exchange Rate"))
        if rate is not None:
            from_code = str(exchange_rate.get("1. From Currency Code") or "").strip()
            to_code = str(exchange_rate.get("3. To Currency Code") or "").strip()
            if not from_code or not to_code:
                return None
            return {"value": rate, "name": f"{from_code}/{to_code}", "unit": to_code}

    # News sentiment (macro or asset-level)
    if key in {"macro_news_sentiment", "market_sentiment"}:
        feed = data.get("feed")
        if isinstance(feed, list) and feed:
            scores = [numeric_value(item.get("overall_sentiment_score")) for item in feed if isinstance(item, dict)]
            scores = [s for s in scores if s is not None]
            if scores:
                average_score = sum(scores) / len(scores)
                return {
                    "article_count": len(scores),
                    "average_score": average_score,
                    "label": alpha_vantage_sentiment_label(average_score),
                    "latest_title": next(
                        (str(item.get("title")) for item in feed if isinstance(item, dict) and item.get("title")),
                        None,
                    ),
                }

    # DIGITAL_CURRENCY_DAILY — daily BTC/crypto OHLCV bars (free AV endpoint)
    crypto_daily = data.get("Time Series (Digital Currency Daily)")
    if isinstance(crypto_daily, dict) and crypto_daily:
        all_timestamps = sorted(crypto_daily.keys(), reverse=True)[:50]
        all_timestamps = list(reversed(all_timestamps))
        all_bars: list[dict[str, Any]] = []
        for ts in all_timestamps:
            bar = crypto_daily[ts]
            all_bars.append({
                "datetime": ts,
                "open":   float(bar.get("1a. open (USD)")   or bar.get("1. open")  or 0),
                "high":   float(bar.get("2a. high (USD)")   or bar.get("2. high")  or 0),
                "low":    float(bar.get("3a. low (USD)")    or bar.get("3. low")   or 0),
                "close":  float(bar.get("4a. close (USD)")  or bar.get("4. close") or 0),
                "volume": int(float(bar.get("5. volume") or 0)),
                "hold":   0,
            })
        if not all_bars:
            return None
        recent_bars = all_bars[-12:]
        first_close = recent_bars[0]["close"]
        last_close  = recent_bars[-1]["close"]
        if last_close > first_close * 1.001:
            price_trend = "up"
        elif last_close < first_close * 0.999:
            price_trend = "down"
        else:
            price_trend = "flat"
        volumes = [b["volume"] for b in recent_bars]
        avg_vol  = sum(volumes) / len(volumes) if volumes else 0
        last_vol = recent_bars[-1]["volume"]
        if avg_vol > 0 and last_vol > avg_vol * 1.2:
            volume_signal = "expanding"
        elif avg_vol > 0 and last_vol < avg_vol * 0.8:
            volume_signal = "shrinking"
        else:
            volume_signal = "neutral"
        label = str((spec or {}).get("label") or f"{key} daily bars")
        closes = [b["close"] for b in all_bars]
        result: dict[str, Any] = {
            "label":         label,
            "price_trend":   price_trend,
            "volume_signal": volume_signal,
            "oi_change":     0,
            "recent_bars":   recent_bars,
        }
        macd = compute_macd(closes)
        if macd:
            result["macd"] = macd
        bbands = compute_bbands(closes)
        if bbands:
            result["bbands"] = bbands
        return result

    # TIME_SERIES_INTRADAY — 60min OHLCV bars
    time_series = data.get("Time Series (60min)")
    if isinstance(time_series, dict) and time_series:
        all_timestamps = sorted(time_series.keys(), reverse=True)[:50]
        all_timestamps = list(reversed(all_timestamps))
        all_bars: list[dict[str, Any]] = []
        for ts in all_timestamps:
            bar = time_series[ts]
            all_bars.append({
                "datetime": ts,
                "open":   float(bar.get("1. open")   or 0),
                "high":   float(bar.get("2. high")   or 0),
                "low":    float(bar.get("3. low")    or 0),
                "close":  float(bar.get("4. close")  or 0),
                "volume": int(float(bar.get("5. volume") or 0)),
                "hold":   0,
            })
        if not all_bars:
            return None
        recent_bars = all_bars[-12:]
        first_close = recent_bars[0]["close"]
        last_close  = recent_bars[-1]["close"]
        if last_close > first_close * 1.001:
            price_trend = "up"
        elif last_close < first_close * 0.999:
            price_trend = "down"
        else:
            price_trend = "flat"
        volumes = [b["volume"] for b in recent_bars]
        avg_vol  = sum(volumes) / len(volumes) if volumes else 0
        last_vol = recent_bars[-1]["volume"]
        if avg_vol > 0 and last_vol > avg_vol * 1.2:
            volume_signal = "expanding"
        elif avg_vol > 0 and last_vol < avg_vol * 0.8:
            volume_signal = "shrinking"
        else:
            volume_signal = "neutral"
        meta   = data.get("Meta Data") or {}
        symbol = str(meta.get("2. Symbol") or key)
        label  = str((spec or {}).get("label") or f"{symbol} 60min bars")
        closes = [b["close"] for b in all_bars]
        result: dict[str, Any] = {
            "label":         label,
            "price_trend":   price_trend,
            "volume_signal": volume_signal,
            "oi_change":     0,
            "recent_bars":   recent_bars,
        }
        macd = compute_macd(closes)
        if macd:
            result["macd"] = macd
        bbands = compute_bbands(closes)
        if bbands:
            result["bbands"] = bbands
        return result

    return None


def format_alpha_vantage_lines(snapshot: dict[str, Any] | None, language: str) -> list[str]:
    if not snapshot or snapshot.get("status") != "ok":
        return []

    metrics = snapshot.get("metrics") or {}
    lines: list[str] = []

    two_year = metrics.get("two_year_yield")
    ten_year = metrics.get("ten_year_yield")
    if two_year and ten_year:
        spread = ten_year["value"] - two_year["value"]
        if language == "en":
            lines.append(
                "Alpha Vantage shows the latest Treasury snapshot at "
                f"2Y {two_year['value']:.2f}%, 10Y {ten_year['value']:.2f}%, "
                f"with the 2s10s spread at {spread:+.2f} percentage points."
            )
        else:
            lines.append(
                "The latest Alpha Vantage data shows "
                f"the 2-year Treasury yield near {two_year['value']:.2f}%, "
                f"the 10-year near {ten_year['value']:.2f}%, "
                f"and the 2s10s spread at about {spread:+.2f} percentage points."
            )

    fed_funds = metrics.get("fed_funds_rate")
    if fed_funds:
        if language == "en":
            lines.append(
                f"The latest Fed funds reading is {fed_funds['value']:.2f}%."
            )
        else:
            lines.append(f"The latest fed funds rate reading is about {fed_funds['value']:.2f}%.")

    gold_spot = metrics.get("gold_spot")
    if gold_spot:
        if language == "en":
            lines.append(
                f"Spot gold is around {gold_spot['value']:.2f} {gold_spot.get('unit') or 'USD/oz'}."
            )
        else:
            lines.append(f"Spot gold is around {gold_spot['value']:.2f} {gold_spot.get('unit') or 'USD/oz'}.")

    wti = metrics.get("wti")
    if wti:
        if language == "en":
            lines.append(f"WTI crude is around {wti['value']:.2f} {wti.get('unit') or 'USD/barrel'}.")
        else:
            lines.append(f"WTI crude is around {wti['value']:.2f} {wti.get('unit') or 'USD/barrel'}.")

    sentiment = metrics.get("macro_news_sentiment")
    if sentiment:
        if language == "en":
            lines.append(
                "Recent macro news sentiment reads "
                f"{sentiment['label']} on average across {sentiment['article_count']} Alpha Vantage articles."
            )
        else:
            lines.append(
                f"Recent macro news sentiment across {sentiment['article_count']} Alpha Vantage articles is on average {sentiment['label']}."
            )

    # RSI technical indicators — detected by "RSI" unit prefix
    for metric_key, metric in metrics.items():
        if not isinstance(metric, dict) or not str(metric.get("unit") or "").startswith("RSI"):
            continue
        rsi_val = numeric_value(metric.get("value"))
        if rsi_val is None:
            continue
        label = str(metric.get("name") or metric_key)
        rsi_unit = str(metric.get("unit") or "RSI(14)")
        if rsi_val > 70:
            interp = "overbought territory"
        elif rsi_val < 30:
            interp = "oversold territory"
        else:
            interp = "neutral territory"
        if language == "en":
            lines.append(f"{label} {rsi_unit} is at {rsi_val:.1f}, placing it in {interp}.")
        else:
            lines.append(f"{label} {rsi_unit}为{rsi_val:.1f}，处于{interp}区间。")

    # Crypto/FX exchange rates — detected by "/" in name field
    for metric_key, metric in metrics.items():
        if not isinstance(metric, dict):
            continue
        name = str(metric.get("name") or "")
        unit = str(metric.get("unit") or "")
        if "/" in name and unit in {"USD", "CNY", "EUR"}:
            rate = numeric_value(metric.get("value"))
            if rate is not None:
                if language == "en":
                    lines.append(f"{name} is trading near {rate:,.2f} {unit}.")
                else:
                    lines.append(f"{name}现价约{rate:,.2f} {unit}。")

    # Asset-level news sentiment — only if macro_news_sentiment not already shown
    asset_sentiment = metrics.get("market_sentiment")
    if asset_sentiment and "macro_news_sentiment" not in metrics:
        if language == "en":
            lines.append(
                f"Asset news sentiment reads {asset_sentiment['label']} "
                f"across {asset_sentiment['article_count']} recent articles."
            )
        else:
            lines.append(
                f"近期{asset_sentiment['article_count']}篇资讯情绪综合为{asset_sentiment['label']}。"
            )

    return lines


def alpha_vantage_summary_line(snapshot: dict[str, Any] | None, language: str) -> str | None:
    if not snapshot or snapshot.get("status") != "ok":
        return None

    metrics = snapshot.get("metrics") or {}
    two_year = metrics.get("two_year_yield")
    ten_year = metrics.get("ten_year_yield")
    fed_funds = metrics.get("fed_funds_rate")
    sentiment = metrics.get("macro_news_sentiment")
    gold_spot = metrics.get("gold_spot")
    wti = metrics.get("wti")

    if language == "en":
        if two_year and ten_year and fed_funds:
            spread = ten_year["value"] - two_year["value"]
            if spread < 0:
                return (
                    f"With 2Y near {two_year['value']:.2f}% and 10Y near {ten_year['value']:.2f}%, "
                    "the curve is still inverted, so rates are not confirming a clean policy reset yet"
                )
            if abs(two_year["value"] - fed_funds["value"]) < 0.4:
                return (
                    f"With 2Y near {two_year['value']:.2f}% and fed funds around {fed_funds['value']:.2f}%, "
                    "front-end pricing still looks anchored rather than disorderly"
                )
            return (
                f"With 2Y near {two_year['value']:.2f}% and 10Y near {ten_year['value']:.2f}%, "
                "rates are moving, but not yet in a way that screams a full policy regime shift"
            )
        if sentiment:
            label = str(sentiment.get("label") or "Neutral").lower()
            if "bearish" in label:
                return "Macro news sentiment still leans bearish, which argues for confirmation before conviction"
            if "bullish" in label:
                return "Macro news sentiment has turned constructive, but I would still want price confirmation before chasing it"
            return "Macro news sentiment is still fairly neutral, which argues for keeping conviction measured"
        if gold_spot:
            return f"Spot gold near {gold_spot['value']:.2f} suggests the market is still paying for hedges rather than embracing a clean risk-on view"
        if wti:
            return f"WTI near {wti['value']:.2f} keeps the inflation and rates channel relevant rather than letting this become only a headline story"
        return None

    lines = format_alpha_vantage_lines(snapshot, language)
    return lines[0] if lines else None


def format_akshare_quote_item(item: dict[str, Any]) -> str:
    label = str(item.get("name") or item.get("symbol") or "unknown").strip()
    price = numeric_value(item.get("price"))
    pct_change = numeric_value(item.get("pct_change"))
    parts = [label]
    if price is not None:
        parts.append(f"{price:.2f}")
    if pct_change is not None:
        parts.append(f"({pct_change:+.2f}%)")
    return " ".join(parts)


def format_akshare_lines(snapshot: dict[str, Any] | None, language: str) -> list[str]:
    if not snapshot or snapshot.get("status") != "ok":
        return []

    metrics = snapshot.get("metrics") or {}
    if not isinstance(metrics, dict):
        return []

    lines: list[str] = []
    for metric in metrics.values():
        if not isinstance(metric, dict):
            continue
        label = str(metric.get("label") or "AkShare snapshot")
        value = numeric_value(metric.get("value"))
        components = metric.get("components")
        quotes = metric.get("quotes")
        leaders = metric.get("leaders")

        if isinstance(components, dict):
            up_count = int(components.get("up_count") or 0)
            down_count = int(components.get("down_count") or 0)
            flat_count = int(components.get("flat_count") or 0)
            if value is None:
                continue
            lines.append(
                f"AkShare has {label} at {value:.2f}, with {up_count} advancers, {down_count} decliners, and {flat_count} unchanged names."
            )
            continue

        if isinstance(quotes, list) and quotes:
            preview = ", ".join(format_akshare_quote_item(item) for item in quotes[:3] if isinstance(item, dict))
            if preview:
                lines.append(f"AkShare watchlist snapshot for {label}: {preview}.")
            continue

        if isinstance(leaders, list) and leaders:
            preview = ", ".join(format_akshare_quote_item(item) for item in leaders[:3] if isinstance(item, dict))
            if preview:
                lines.append(f"AkShare top movers for {label}: {preview}.")
            continue

    return lines


def format_akshare_prediction_context(
    snapshot: dict[str, Any] | None,
    challenge_asset: str,
    language: str,
) -> str | None:
    """Return a single anchor line for the challenge asset from the AkShare snapshot.

    language is accepted for future localization but not currently used.
    """
    if not snapshot or snapshot.get("status") != "ok":
        return None
    akshare_key = ASSET_AKSHARE_KEY_MAP.get(challenge_asset.lower())
    if not akshare_key:
        return None
    metrics = snapshot.get("metrics") or {}
    metric = metrics.get(akshare_key)
    if not isinstance(metric, dict):
        return None
    quotes = metric.get("quotes")
    if isinstance(quotes, list) and quotes:
        first = quotes[0]
        if isinstance(first, dict):
            return format_akshare_quote_item(first)
    # first quote was not a dict — fall through to value fallback
    value = numeric_value(metric.get("value"))
    if value is not None:
        label = str(metric.get("label") or akshare_key)
        return f"Current {label}: {value:.2f}"
    return None


def akshare_summary_line(snapshot: dict[str, Any] | None, language: str) -> str | None:
    if not snapshot or snapshot.get("status") != "ok":
        return None

    metrics = snapshot.get("metrics") or {}
    if not isinstance(metrics, dict):
        return None

    for metric in metrics.values():
        if not isinstance(metric, dict):
            continue
        label = str(metric.get("label") or "AkShare snapshot")
        value = numeric_value(metric.get("value"))
        components = metric.get("components")
        quotes = metric.get("quotes")
        leaders = metric.get("leaders")

        if isinstance(components, dict) and value is not None:
            up_count = int(components.get("up_count") or 0)
            down_count = int(components.get("down_count") or 0)
            return (
                f"AkShare has {label} at {value:.2f} with {up_count} advancers against {down_count} decliners, "
                "so live breadth is giving a cleaner risk read than the headline alone"
            )
        if isinstance(quotes, list) and quotes:
            preview = ", ".join(format_akshare_quote_item(item) for item in quotes[:2] if isinstance(item, dict))
            if preview:
                return f"AkShare live quotes for {label} show {preview}, so the cross-market tape is already offering confirmation points"
        if isinstance(leaders, list) and leaders:
            preview = ", ".join(format_akshare_quote_item(item) for item in leaders[:2] if isinstance(item, dict))
            if preview:
                return f"AkShare top movers in {label} currently read {preview}, which helps anchor the discussion in live cross-market leadership"

    return None


_KLINE_TREND_LABEL: dict[str, str] = {
    "up": "↑上升",
    "down": "↓下跌",
    "flat": "→ 横盘",
}

_KLINE_VOLUME_LABEL: dict[str, str] = {
    "expanding": "expanding（放量）",
    "shrinking": "shrinking（缩量）",
    "neutral": "neutral",
}

_AV_KLINE_TREND_LABEL: dict[str, str] = {
    "up": "↑ up",
    "down": "↓ down",
    "flat": "→ flat",
}

_AV_KLINE_VOLUME_LABEL: dict[str, str] = {
    "expanding": "expanding",
    "shrinking": "shrinking",
    "neutral": "neutral",
}


def format_kline_context(snapshot: dict[str, Any] | None) -> str:
    """Format kline_summary metrics from an akshare snapshot into a readable block.

    Returns an empty string if the snapshot has no kline metrics.
    Only processes metrics whose key ends with '_kline' and have a 'price_trend' field.
    """
    if not snapshot:
        return ""
    metrics = snapshot.get("metrics") or {}
    kline_items = [
        (k, v) for k, v in metrics.items()
        if k.endswith("_kline") and isinstance(v, dict) and "price_trend" in v
    ]
    if not kline_items:
        return ""

    lines: list[str] = ["技术面数据（60min K线，最近12根）:"]
    for _, metric in kline_items:
        label = str(metric.get("label") or metric.get("asset") or "")
        trend = _KLINE_TREND_LABEL.get(str(metric.get("price_trend") or ""), "—")
        vol = _KLINE_VOLUME_LABEL.get(str(metric.get("volume_signal") or ""), "—")
        oi = int(metric.get("oi_change") or 0)
        oi_str = f"+{oi}" if oi >= 0 else str(oi)
        lines.append(f"[{label}]  趋势: {trend}  量能: {vol}  持仓变化: {oi_str}手")
        for bar in (metric.get("recent_bars") or []):
            dt = str(bar.get("datetime") or "")
            time_part = dt[11:16] if len(dt) >= 16 else dt
            lines.append(
                f"  {time_part} O:{bar.get('open')} H:{bar.get('high')} "
                f"L:{bar.get('low')} C:{bar.get('close')} "
                f"Vol:{bar.get('volume')} OI:{bar.get('hold')}"
            )
    return "\n".join(lines)


def format_av_kline_context(snapshot: dict[str, Any] | None) -> str:
    """Format kline metrics from an Alpha Vantage snapshot into a readable block.

    Returns an empty string if the snapshot has no kline metrics.
    Only processes metrics whose key ends with '_kline' and have a 'price_trend' field.
    """
    if not snapshot or snapshot.get("status") != "ok":
        return ""
    metrics = snapshot.get("metrics") or {}
    kline_items = [
        (k, v) for k, v in metrics.items()
        if k.endswith("_kline") and isinstance(v, dict) and "price_trend" in v
    ]
    if not kline_items:
        return ""

    lines: list[str] = ["Technical data (last 12 bars):"]
    for _, metric in kline_items:
        label = str(metric.get("label") or metric.get("asset") or "")
        trend = _AV_KLINE_TREND_LABEL.get(str(metric.get("price_trend") or ""), "—")
        vol   = _AV_KLINE_VOLUME_LABEL.get(str(metric.get("volume_signal") or ""), "—")
        oi    = int(metric.get("oi_change") or 0)
        oi_str = f"+{oi}" if oi >= 0 else str(oi)
        lines.append(f"[{label}]  Trend: {trend}  Volume: {vol}  OI change: {oi_str}")
        for bar in (metric.get("recent_bars") or []):
            dt = str(bar.get("datetime") or "")
            time_part = dt[11:16] if len(dt) >= 16 else dt
            lines.append(
                f"  {time_part} O:{bar.get('open')} H:{bar.get('high')} "
                f"L:{bar.get('low')} C:{bar.get('close')} "
                f"Vol:{bar.get('volume')}"
            )
        macd = metric.get("macd")
        if isinstance(macd, dict):
            interp = str(macd.get("interpretation") or "").replace("_", " ")
            lines.append(
                f"  MACD(12,26,9): {macd.get('macd')} / Signal: {macd.get('signal')}"
                f" / Hist: {macd.get('histogram')} — {interp}"
            )
        bbands = metric.get("bbands")
        if isinstance(bbands, dict):
            interp = str(bbands.get("interpretation") or "").replace("_", " ")
            lines.append(
                f"  BBANDS(20,2): Upper {bbands.get('upper')} / Mid {bbands.get('middle')}"
                f" / Lower {bbands.get('lower')} — %B: {bbands.get('percent_b')}, {interp}"
            )
    return "\n".join(lines)


def format_notional(value: float) -> str:
    absolute = abs(value)
    if absolute >= 1_000_000_000_000:
        return f"${value / 1_000_000_000_000:.2f}tn"
    if absolute >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}bn"
    if absolute >= 1_000_000:
        return f"${value / 1_000_000:.1f}m"
    return f"${value:.0f}"


def latest_fred_observation(data: dict[str, Any], default_name: str, default_unit: str | None = None) -> dict[str, Any] | None:
    observations = data.get("observations")
    if not isinstance(observations, list):
        return None
    for item in observations:
        if not isinstance(item, dict):
            continue
        value = numeric_value(item.get("value"))
        if value is None:
            continue
        return {
            "date": item.get("date"),
            "value": value,
            "name": default_name,
            "unit": default_unit,
        }
    return None


def normalize_fred_payload(key: str, data: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any] | None:
    label = str(spec.get("label") or spec.get("series_id") or key)
    unit = spec.get("unit")
    return latest_fred_observation(data, label, unit)


def format_fred_lines(snapshot: dict[str, Any] | None, language: str) -> list[str]:
    if not snapshot or snapshot.get("status") != "ok":
        return []

    metrics = snapshot.get("metrics") or {}
    lines: list[str] = []

    two_year = metrics.get("two_year_yield")
    ten_year = metrics.get("ten_year_yield")
    fed_funds = metrics.get("fed_funds_rate")
    cpi_yoy = metrics.get("cpi_yoy")
    unemployment = metrics.get("unemployment_rate")
    sp500 = metrics.get("sp500_index")
    vix = metrics.get("vix_index")
    dollar_index = metrics.get("dollar_index")
    high_yield_oas = metrics.get("high_yield_oas")

    if two_year and ten_year and fed_funds:
        spread = ten_year["value"] - two_year["value"]
        lines.append(
            "FRED currently shows "
            f"2Y at {two_year['value']:.2f}%, 10Y at {ten_year['value']:.2f}%, "
            f"fed funds at {fed_funds['value']:.2f}%, and 2s10s at {spread:+.2f} percentage points."
        )

    if cpi_yoy and unemployment:
        lines.append(
            f"FRED shows CPI running {cpi_yoy['value']:.2f}% year over year while unemployment is {unemployment['value']:.2f}%."
        )
    elif cpi_yoy:
        lines.append(f"FRED shows CPI running {cpi_yoy['value']:.2f}% year over year.")
    elif unemployment:
        lines.append(f"FRED shows unemployment at {unemployment['value']:.2f}%.")

    if sp500 and vix:
        lines.append(
            f"FRED has the S&P 500 near {sp500['value']:.2f} with VIX around {vix['value']:.2f}."
        )
    elif sp500:
        lines.append(f"FRED has the S&P 500 near {sp500['value']:.2f}.")
    elif vix:
        lines.append(f"FRED has VIX around {vix['value']:.2f}.")

    if dollar_index and high_yield_oas:
        lines.append(
            f"FRED shows the broad dollar index near {dollar_index['value']:.2f} and HY OAS near {high_yield_oas['value']:.2f} percentage points."
        )
    elif dollar_index:
        lines.append(f"FRED shows the broad dollar index near {dollar_index['value']:.2f}.")
    elif high_yield_oas:
        lines.append(f"FRED has HY OAS near {high_yield_oas['value']:.2f} percentage points.")

    return lines


def fred_summary_line(snapshot: dict[str, Any] | None, language: str) -> str | None:
    if not snapshot or snapshot.get("status") != "ok":
        return None

    metrics = snapshot.get("metrics") or {}
    cpi_yoy = metrics.get("cpi_yoy")
    unemployment = metrics.get("unemployment_rate")
    two_year = metrics.get("two_year_yield")
    fed_funds = metrics.get("fed_funds_rate")
    sp500 = metrics.get("sp500_index")
    vix = metrics.get("vix_index")
    dollar_index = metrics.get("dollar_index")
    high_yield_oas = metrics.get("high_yield_oas")

    if sp500 and vix:
        return (
            f"FRED has the S&P 500 near {sp500['value']:.0f} with VIX around {vix['value']:.2f}, "
            "so equity and volatility confirmation still matter more than the headline alone"
        )
    if dollar_index and high_yield_oas:
        return (
            f"FRED has the broad dollar index near {dollar_index['value']:.2f} with HY OAS around {high_yield_oas['value']:.2f} percentage points, "
            "so dollar and credit confirmation still matter for the macro read"
        )

    if two_year and fed_funds:
        if two_year["value"] > fed_funds["value"] + 0.25:
            return (
                f"FRED still has the 2-year at {two_year['value']:.2f}% against fed funds at {fed_funds['value']:.2f}%, "
                "so the front end still implies a restrictive path rather than an easy policy pivot"
            )
        return (
            f"FRED has the 2-year near {two_year['value']:.2f}% and fed funds near {fed_funds['value']:.2f}%, "
            "which says front-end pricing is not breaking out of its policy anchor yet"
        )
    if cpi_yoy and unemployment:
        return (
            f"FRED still shows CPI near {cpi_yoy['value']:.2f}% year over year with unemployment at {unemployment['value']:.2f}%, "
            "so the macro backdrop does not yet argue for a loose policy read"
        )
    return None


def latest_hedgefundmonitor_observation(data: list[Any], default_name: str, default_unit: str | None = None) -> dict[str, Any] | None:
    if not isinstance(data, list):
        return None
    for item in reversed(data):
        if not isinstance(item, list) or len(item) < 2:
            continue
        value = numeric_value(item[1])
        if value is None:
            continue
        return {
            "date": item[0],
            "value": value,
            "name": default_name,
            "unit": default_unit,
        }
    return None


def normalize_hedgefundmonitor_payload(key: str, data: list[Any], spec: dict[str, Any]) -> dict[str, Any] | None:
    label = str(spec.get("label") or spec.get("mnemonic") or key)
    unit = spec.get("unit")
    return latest_hedgefundmonitor_observation(data, label, unit)


def format_hedgefundmonitor_lines(snapshot: dict[str, Any] | None, language: str) -> list[str]:
    if not snapshot or snapshot.get("status") != "ok":
        return []

    metrics = snapshot.get("metrics") or {}
    leverage = metrics.get("top10_leverage")
    repo_volume = metrics.get("sponsored_repo_volume")
    financing_liquidity = metrics.get("weekly_financing_liquidity")
    lines: list[str] = []

    if leverage:
        lines.append(f"OFR Hedge Fund Monitor puts top-10 fund leverage near {leverage['value']:.2f}x.")
    if repo_volume:
        lines.append(f"FICC sponsored repo volume is running near {format_notional(repo_volume['value'])}.")
    if financing_liquidity:
        lines.append(
            "Weekly financing liquidity due within 7 days is about "
            f"{format_notional(financing_liquidity['value'])}."
        )
    return lines


def hedgefundmonitor_summary_line(snapshot: dict[str, Any] | None, language: str) -> str | None:
    if not snapshot or snapshot.get("status") != "ok":
        return None

    metrics = snapshot.get("metrics") or {}
    leverage = metrics.get("top10_leverage")
    repo_volume = metrics.get("sponsored_repo_volume")

    if leverage and repo_volume:
        if leverage["value"] >= 2.0:
            return (
                f"OFR data still shows top-10 hedge fund leverage near {leverage['value']:.2f}x with "
                f"FICC sponsored repo volume around {format_notional(repo_volume['value'])}, "
                "so funding conditions still matter for any cross-asset conviction call"
            )
        return (
            f"With top-10 hedge fund leverage near {leverage['value']:.2f}x and sponsored repo volume near "
            f"{format_notional(repo_volume['value'])}, the leverage backdrop does not yet look disorderly"
        )
    if repo_volume:
        return f"Sponsored repo volume near {format_notional(repo_volume['value'])} says funding conditions are still part of the cross-asset setup"
    return None


_SKILL_REGISTRY: list[dict[str, Any]] = [
    {
        "key": "akshare",
        "fetch": "get_akshare_snapshot",
        "format_lines": format_akshare_lines,
        "summary_line": akshare_summary_line,
        "kline_context": format_kline_context,
    },
    {
        "key": "alpha_vantage",
        "fetch": "get_alpha_vantage_snapshot",
        "format_lines": format_alpha_vantage_lines,
        "summary_line": alpha_vantage_summary_line,
        "kline_context": format_av_kline_context,
    },
    {
        "key": "fred_economic_data",
        "fetch": "get_fred_snapshot",
        "format_lines": format_fred_lines,
        "summary_line": fred_summary_line,
        "kline_context": None,
    },
    {
        "key": "hedgefundmonitor",
        "fetch": "get_hedgefundmonitor_snapshot",
        "format_lines": format_hedgefundmonitor_lines,
        "summary_line": hedgefundmonitor_summary_line,
        "kline_context": None,
    },
]


def format_skill_lines(skill_snapshots: dict[str, dict[str, Any]] | None, language: str) -> list[str]:
    if not skill_snapshots:
        return []

    lines: list[str] = []
    for entry in _SKILL_REGISTRY:
        lines.extend(entry["format_lines"](skill_snapshots.get(entry["key"]), language))
    for entry in _SKILL_REGISTRY:
        kline_context = entry["kline_context"]
        if kline_context is None:
            continue
        block = kline_context(skill_snapshots.get(entry["key"]))
        if block:
            lines.append(block)
    return lines


def skill_summary_line(skill_snapshots: dict[str, dict[str, Any]] | None, language: str) -> str | None:
    if not skill_snapshots:
        return None

    summaries = [
        entry["summary_line"](skill_snapshots.get(entry["key"]), language) for entry in _SKILL_REGISTRY
    ]
    summaries = [item for item in summaries if item]
    if not summaries:
        return None
    return join_generated_sentences(summaries[:2], language)


def run_skill_fetch(
    provider: str,
    purpose: str,
    request_specs: list[dict[str, Any]],
    cache_ttl_seconds: int,
    request_delay_seconds: float,
    fetch_one: Callable[[dict[str, Any], str], dict[str, Any]],
) -> dict[str, Any]:
    cache_key_seed = json.dumps(request_specs, ensure_ascii=False, sort_keys=True)
    request_hash = hashlib.sha256(cache_key_seed.encode("utf-8")).hexdigest()[:12]
    asset_key = extract_market_data_asset_key(purpose)

    cached_snapshot = storage_market_data.get_cached_snapshot(provider, purpose, request_hash, cache_ttl_seconds)
    if cached_snapshot is not None:
        return cached_snapshot

    metrics: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    for index, raw_spec in enumerate(request_specs):
        if not isinstance(raw_spec, dict):
            continue
        request_key = str(raw_spec.get("key") or index)
        if index:
            time.sleep(request_delay_seconds)
        try:
            fragment = fetch_one(raw_spec, request_key)
            if isinstance(fragment, dict):
                metrics.update(fragment)
        except ApiError as exc:
            errors.append({"key": request_key, "error": str(exc)})

    if not metrics and errors:
        snapshot = {
            "provider": provider,
            "status": "error",
            "error": errors[0]["error"],
            "errors": errors,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        storage_market_data.store_snapshot(provider, asset_key, purpose, request_hash, snapshot)
        return snapshot

    snapshot = {
        "provider": provider,
        "status": "ok" if metrics else "empty",
        "metrics": metrics,
        "errors": errors,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    storage_market_data.store_snapshot(provider, asset_key, purpose, request_hash, snapshot)
    return snapshot


class MarketCommentAgent:
    def __init__(self, base_url: str, credential_path: Path, state_path: Path, config_path: Path) -> None:
        self.base_url = base_url.rstrip("/")
        self.credential_path = credential_path
        self.state_path = state_path
        self.config_path = config_path
        self.persona_id = config_path.stem
        self.credentials_spool_path = state_path.parent / f"{self.persona_id}.pending_credentials.jsonl"
        self.processed_items_spool_path = state_path.parent / f"{self.persona_id}.pending_processed_items.jsonl"
        storage_credentials.drain_pending_credentials(self.credentials_spool_path)
        storage_processed_items.drain_pending_processed_items(self.processed_items_spool_path)
        self.credentials = storage_credentials.load_credentials(self.persona_id)
        self.state = load_state(state_path)
        self.config = load_agent_config(config_path)

    def register(self, profile_overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(DEFAULT_AGENT_PROFILE)
        payload.update(
            {
                "model_provider": self.config.get("model", {}).get("provider", payload["model_provider"]),
                "model_name": self.config.get("model", {}).get("name", payload["model_name"]),
                "model_version": self.config.get("model", {}).get("version", payload["model_version"]),
            }
        )
        if profile_overrides:
            payload.update({k: v for k, v in profile_overrides.items() if v is not None})

        register_path = (self.config.get("registration") or {}).get(
            "endpoint", "/api/v1/agent/registry/register"
        )
        data = http_request(
            "POST",
            f"{self.base_url}{register_path}",
            body=payload,
        )

        merged = {
            "base_url": self.base_url,
            "registered_at": datetime.now(timezone.utc).isoformat(),
            **payload,
            **data,
        }
        if isinstance(merged.get("claim_url"), str):
            merged["claim_url"] = merged["claim_url"].replace("http://", "https://", 1)
        self.credentials = merged
        storage_credentials.save_credentials(self.persona_id, self.credentials, spool_path=self.credentials_spool_path)

        challenge_id = data.get("challenge_id")
        challenge_prompt = data.get("challenge_prompt", "")
        if challenge_id and challenge_prompt:
            system_prompt = (
                "You are a professional financial market analysis agent.\n"
                "Analyze the market event below and return a structured JSON response.\n"
                "Return ONLY the JSON object, no additional text.\n"
                "Required format:\n"
                '{"event_summary": "One sentence summary", '
                '"market_impact": {"affected_assets": ["XAUUSD", "DXY", ...], '
                '"direction": "bullish/bearish/mixed", "magnitude": "low/medium/high", '
                '"reasoning": "2-3 sentence causal chain"}, '
                '"trading_implications": {"short_term": "1-2 sentences", "medium_term": "1-2 sentences"}, '
                '"confidence": 0.0, "related_events": ["event type 1", ...]}'
            )
            raw = self.generate_text(system_prompt, challenge_prompt) or ""
            try:
                answer = json.loads(raw.strip())
            except json.JSONDecodeError:
                m = re.search(r"\{.*\}", raw, re.DOTALL)
                answer = json.loads(m.group()) if m else {}

            if answer:
                submit_url = data.get("submit_url") or f"/api/v1/agent/challenge/{challenge_id}/submit"
                if not submit_url.startswith("http"):
                    submit_url = f"{self.base_url}{submit_url}"
                submit_url = submit_url.replace("http://", "https://", 1)
                try:
                    submit_data = http_request(
                        "POST",
                        submit_url,
                        body={"agent_id": data["agent_id"], "answer": answer},
                    )
                    if submit_data.get("passed"):
                        merged.update(submit_data)
                        # normalise http → https on claim_url
                        if isinstance(merged.get("claim_url"), str):
                            merged["claim_url"] = merged["claim_url"].replace("http://", "https://", 1)
                        self.credentials = merged
                        storage_credentials.save_credentials(self.persona_id, self.credentials, spool_path=self.credentials_spool_path)
                        data["claim_url"] = merged.get("claim_url")
                    else:
                        score = submit_data.get("score", "?")
                        threshold = submit_data.get("threshold", "?")
                        print(f"Warning: registration challenge not passed (score {score}/{threshold}); token will be unavailable until challenge completes.", file=sys.stderr)
                except Exception as exc:
                    print(f"Warning: registration challenge submission failed: {exc}", file=sys.stderr)

        return data

    def register_cn(self, profile_overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        """Two-step CN registration: register, answer challenge, save credentials."""
        payload = dict(DEFAULT_AGENT_PROFILE)
        payload.update(
            {
                "model_provider": self.config.get("model", {}).get("provider", payload["model_provider"]),
                "model_name": self.config.get("model", {}).get("name", payload["model_name"]),
                "model_version": self.config.get("model", {}).get("version", payload["model_version"]),
            }
        )
        if profile_overrides:
            payload.update({k: v for k, v in profile_overrides.items() if v is not None})

        register_path = (self.config.get("registration") or {}).get("endpoint", "/api/v1/cn/agent/registry/register")
        reg_data = http_request("POST", f"{self.base_url}{register_path}", body=payload)

        challenge_id = reg_data.get("challenge_id")
        challenge_prompt = reg_data.get("challenge_prompt", "")
        if not challenge_id:
            raise ApiError("CN registration did not return a challenge_id")

        system_prompt = (
            "你是一个专业的中国金融市场分析 Agent，擅长沪金（AU）、沪银（AG）和股指期货（IF/IH/IC/IM）。\n"
            "根据以下认证挑战，给出你的市场方向判断。\n"
            "只返回合法 JSON，格式为：\n"
            '{"direction": "up" | "down" | "flat", "confidence": 0.0-1.0, "analysis": "50-200字分析"}\n'
            "不要输出 JSON 以外的任何内容。"
        )
        raw = self.generate_text(system_prompt, challenge_prompt) or ""
        try:
            answer = json.loads(raw.strip())
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if not m:
                raise ApiError(f"LLM did not return valid JSON for CN challenge: {raw!r}")
            answer = json.loads(m.group())

        submit_path = reg_data.get("submit_url") or f"/api/v1/cn/agent/challenge/{challenge_id}/submit"
        submit_url = f"{self.base_url}{submit_path}"
        submit_data = http_request(
            "POST",
            submit_url,
            body={"agent_id": reg_data["agent_id"], "answer": answer},
        )

        if not submit_data.get("passed"):
            score = submit_data.get("score", "?")
            threshold = submit_data.get("threshold", "?")
            raise ApiError(
                f"CN challenge not passed (score {score}/{threshold}). "
                "Retry with `register-cn` within the 30-minute window."
            )

        merged = {
            "base_url": self.base_url,
            "registered_at": datetime.now(timezone.utc).isoformat(),
            **payload,
            **reg_data,
            **submit_data,
        }
        if isinstance(merged.get("claim_url"), str):
            merged["claim_url"] = merged["claim_url"].replace("http://", "https://", 1)
        self.credentials = merged
        storage_credentials.save_credentials(self.persona_id, self.credentials, spool_path=self.credentials_spool_path)
        return merged

    def get_token(self, refresh: bool = False) -> str:
        token = self.credentials.get("access_token")
        expires_at = self.credentials.get("token_expires_at")
        if token and expires_at and not refresh:
            if datetime.now(timezone.utc).timestamp() < expires_at - 30:
                return token

        agent_id = self.credentials.get("agent_id") or self.credentials.get("client_id")
        client_secret = self.credentials.get("client_secret")
        if not agent_id or not client_secret:
            raise ApiError("Missing agent credentials. Run register first and activate the claim_url.")

        token_ttl = int((self.config.get("runtime") or {}).get("token_ttl_seconds", 3600))
        token_path = (self.config.get("registration") or {}).get(
            "token_endpoint", "/api/v1/agent/auth/token"
        )
        data = http_request(
            "POST",
            f"{self.base_url}{token_path}",
            body={
                "agent_id": agent_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
            },
        )

        expires_in = int(data.get("expires_in", token_ttl))
        self.credentials["access_token"] = data["access_token"]
        self.credentials["token_expires_at"] = datetime.now(timezone.utc).timestamp() + expires_in
        storage_credentials.save_credentials(self.persona_id, self.credentials, spool_path=self.credentials_spool_path)
        return data["access_token"]

    def auth_headers(self) -> dict[str, str]:
        token = self.get_token()
        agent_id = self.credentials.get("agent_id")
        if not agent_id:
            raise ApiError("Missing agent_id. Run register first.")
        return {
            "Authorization": f"Bearer {token}",
            "X-Agent-Id": agent_id,
            "X-Request-Id": str(uuid.uuid4()),
        }

    def get_profile(self) -> dict[str, Any]:
        return http_request(
            "GET",
            f"{self.base_url}/api/v1/agent/profile/self",
            headers=self.auth_headers(),
        )

    def cache_granted_scopes(self, scopes: list[str]) -> None:
        normalized = {str(scope).strip() for scope in scopes if str(scope).strip()}
        if not normalized:
            return
        granted = set(self.credentials.get("granted_scopes") or [])
        if normalized.issubset(granted):
            return
        granted.update(normalized)
        self.credentials["granted_scopes"] = sorted(granted)
        storage_credentials.save_credentials(self.persona_id, self.credentials, spool_path=self.credentials_spool_path)

    def update_scopes(self, add_scopes: list[str]) -> dict[str, Any]:
        requested = sorted({str(scope).strip() for scope in add_scopes if str(scope).strip()})
        if not requested:
            raise ApiError("No scopes requested for update.")
        response = http_json_request(
            "POST",
            f"{self.base_url}/api/v1/agent/scopes",
            body={"add": requested},
            headers=self.auth_headers(),
        )
        confirmed = []
        for key in ("granted", "already_had"):
            values = response.get(key) or []
            if isinstance(values, list):
                confirmed.extend(str(value).strip() for value in values if str(value).strip())
        self.cache_granted_scopes(confirmed)
        if confirmed:
            self.get_token(refresh=True)
        return response

    def ensure_scopes(self, required_scopes: list[str]) -> dict[str, Any]:
        requested = sorted({str(scope).strip() for scope in required_scopes if str(scope).strip()})
        if not requested:
            return {"granted": [], "already_had": [], "invalid": []}
        response = self.update_scopes(requested)
        confirmed = {
            str(value).strip()
            for key in ("granted", "already_had")
            for value in (response.get(key) or [])
            if str(value).strip()
        }
        unresolved = [scope for scope in requested if scope not in confirmed]
        if unresolved:
            raise ApiError(f"Scope update did not confirm required scopes: {', '.join(unresolved)}")
        return response

    def get_events_today(self) -> list[dict[str, Any]]:
        return http_request("GET", f"{self.base_url}/api/v1/events/today")

    def get_recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        query = parse.urlencode({"limit": limit})
        return http_request("GET", f"{self.base_url}/api/v1/events?{query}")

    def list_prediction_challenges(self, status: str = "open", event_id: str | None = None, challenges_path: str | None = None) -> dict[str, Any]:
        path = challenges_path or "/api/v1/eval/challenges"
        query_params: dict[str, Any] = {}
        if status and not challenges_path:
            # CN challenges endpoint does not support status filter; skip it
            query_params["status"] = status
        if event_id and not challenges_path:
            query_params["event_id"] = event_id
        query = parse.urlencode(query_params)
        suffix = f"?{query}" if query else ""
        data = http_request("GET", f"{self.base_url}{path}{suffix}")
        # Normalize CN response: {"challenges": [...]} → {"items": [...]}
        # Do NOT map closes_at→deadline: CN server enforces openness via status field,
        # so we rely on status:"open" check rather than client-side deadline filtering.
        if isinstance(data, dict) and "challenges" in data and "items" not in data:
            raw_items = data.get("challenges") or []
            normalized: list[dict[str, Any]] = []
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                n = dict(item)
                if "cn_asset" in n and "asset" not in n:
                    n["asset"] = n["cn_asset"]
                normalized.append(n)
            return {"items": normalized, "total": len(normalized)}
        return data

    def get_btc_context(self) -> dict[str, Any]:
        try:
            return http_request("GET", f"{self.base_url}/api/v1/eval/btc/context")
        except ApiError:
            return {}

    def get_public_comments(self, news_id: str) -> dict[str, Any]:
        quoted = parse.quote(news_id, safe="")
        return http_request("GET", f"{self.base_url}/api/v1/public/comments/{quoted}")

    def get_interaction_context(self, news_id: str) -> dict[str, Any]:
        quoted = parse.quote(news_id, safe="")
        return http_request(
            "GET",
            f"{self.base_url}/api/v1/agent/news/{quoted}/interaction-context",
            headers=self.auth_headers(),
        )

    def post_comment(self, news_id: str, content: str, space_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "news_id": news_id,
            "content": content,
        }
        if space_id:
            payload["space_id"] = space_id
        return http_request(
            "POST",
            f"{self.base_url}/api/v1/agent/comments",
            body=payload,
            headers=self.auth_headers(),
        )

    def post_cn_comment(self, news_id: str, content: str) -> dict[str, Any]:
        return http_request(
            "POST",
            f"{self.base_url}/api/v1/cn/agent/comments",
            body={"news_id": news_id, "content": content},
            headers=self.auth_headers(),
        )

    def post_cn_post(self, space_id: str, title: str, content: str) -> dict[str, Any]:
        return http_request(
            "POST",
            f"{self.base_url}/api/v1/cn/agent/posts",
            body={"space_id": space_id, "title": title, "content": content},
            headers=self.auth_headers(),
        )

    def get_cn_events(self, since_hours: int = 48, limit: int = 20) -> list[dict[str, Any]]:
        query = parse.urlencode({"since_hours": since_hours, "limit": limit})
        data = http_request("GET", f"{self.base_url}/api/cn/events?{query}", headers=self.auth_headers())
        if isinstance(data, dict) and "events" in data:
            return data["events"]
        return data if isinstance(data, list) else []

    def post_reply(self, comment_id: str, content: str) -> dict[str, Any]:
        quoted = parse.quote(comment_id, safe="")
        return http_request(
            "POST",
            f"{self.base_url}/api/v1/agent/comments/{quoted}/replies",
            body={"content": content},
            headers=self.auth_headers(),
        )

    def follow_agent(self, target_agent_id: str) -> dict[str, Any]:
        return http_request(
            "POST",
            f"{self.base_url}/api/v1/agent/follows",
            body={"target_agent_id": target_agent_id},
            headers=self.auth_headers(),
        )

    def unfollow_agent(self, target_agent_id: str) -> dict[str, Any]:
        quoted = parse.quote(target_agent_id, safe="")
        return http_request(
            "DELETE",
            f"{self.base_url}/api/v1/agent/follows/{quoted}",
            headers=self.auth_headers(),
        )

    def subscribe_prediction_scope(self, symbol: str) -> dict[str, Any]:
        quoted = parse.quote(symbol, safe="")
        result = http_request(
            "POST",
            f"{self.base_url}/api/v1/agent/prediction-scope/{quoted}",
            headers=self.auth_headers(),
        )
        return result if isinstance(result, dict) else {"raw": result}

    def submit_prediction(
        self,
        challenge_id: str,
        request_body: dict[str, Any],
        predict_path_template: str | None = None,
    ) -> dict[str, Any]:
        quoted = parse.quote(challenge_id, safe="")
        if predict_path_template:
            url = f"{self.base_url}{predict_path_template.replace('{id}', quoted)}"
        else:
            url = f"{self.base_url}/api/v1/eval/challenges/{quoted}/predict"
        try:
            return http_request(
                "POST",
                url,
                body=request_body,
                headers=self.auth_headers(),
            )
        except ApiError as exc:
            # The platform gates prediction submission per asset symbol: a 403 whose detail says
            # "Subscribe to 'SI' first via POST /api/v1/agent/prediction-scope/SI" means the agent
            # only needs a one-time subscription to that symbol -- do it and retry once.
            message = str(exc)
            scope_match = re.search(r"prediction-scope/([A-Za-z0-9._-]+)", message)
            if not message.startswith("HTTP 403") or scope_match is None:
                raise
            symbol = scope_match.group(1)
            self.subscribe_prediction_scope(symbol)
            print(f"Subscribed to prediction scope {symbol}; retrying prediction submission.", file=sys.stderr)
            return http_request(
                "POST",
                url,
                body=request_body,
                headers=self.auth_headers(),
            )

    def get_credit_balance(self) -> float | None:
        try:
            data = http_request("GET", f"{self.base_url}/api/v1/agent/credits/balance", headers=self.auth_headers())
            return float(data.get("available_balance"))
        except (ApiError, AttributeError, TypeError, ValueError) as exc:
            print(f"get_credit_balance: failed: {exc}", file=sys.stderr)
            return None

    def list_prediction_contracts(self) -> dict[str, Any]:
        data = http_request("GET", f"{self.base_url}/api/v1/public/prediction-contracts")
        return data if isinstance(data, dict) else {}

    def submit_civic_forecast(
        self, challenge_id: str, submission_route: str, request_body: dict[str, Any]
    ) -> dict[str, Any]:
        quoted = parse.quote(challenge_id, safe="")
        # An already-open Legacy Macro round keeps its frozen legacy write route; everything
        # else goes through the canonical prediction-contract-v2 forecast endpoint.
        if submission_route == "macro_numeric_legacy":
            url = f"{self.base_url}/api/v1/eval/macro/challenges/{quoted}/predict"
        else:
            url = f"{self.base_url}/api/v1/eval/human-forecasts/challenges/{quoted}/forecast"
        result = http_request("POST", url, body=request_body, headers=self.auth_headers())
        return result if isinstance(result, dict) else {"raw": result}

    def mark_civic_forecasted(self, challenge_id: str) -> None:
        storage_processed_items.mark_processed(
            self.persona_id, "civic_forecast", challenge_id, spool_path=self.processed_items_spool_path
        )
        self._touch_last_cycle_at()

    def has_civic_forecasted(self, challenge_id: str) -> bool:
        return storage_processed_items.is_processed(self.persona_id, "civic_forecast", challenge_id)

    def save_state(self) -> None:
        save_state(self.state_path, self.state)

    def reload_config(self) -> dict[str, Any]:
        self.config = load_agent_config(self.config_path)
        return self.config

    def alpha_vantage_get(self, function_name: str, params: dict[str, Any], api_key: str) -> dict[str, Any]:
        query = parse.urlencode(
            {
                "function": function_name,
                "apikey": api_key,
                **params,
            }
        )
        data = http_request("GET", f"https://www.alphavantage.co/query?{query}")
        if not isinstance(data, dict):
            raise ApiError(f"Expected JSON object from Alpha Vantage for {function_name}")
        if data.get("Error Message"):
            raise ApiError(f"Alpha Vantage error for {function_name}: {data['Error Message']}")
        if data.get("Note"):
            raise ApiError(f"Alpha Vantage rate limit for {function_name}: {data['Note']}")
        if data.get("Information"):
            raise ApiError(f"Alpha Vantage info for {function_name}: {data['Information']}")
        return data

    def get_alpha_vantage_snapshot(self, purpose: str) -> dict[str, Any]:
        skill_config = (self.config.get("skills") or {}).get("alpha_vantage") or {}
        if not skill_config.get("enabled", False):
            return {"provider": "alpha_vantage", "status": "disabled"}

        api_key_env = str(skill_config.get("api_key_env", "ALPHAVANTAGE_API_KEY"))
        api_key = os.environ.get(api_key_env)
        if not api_key:
            return {
                "provider": "alpha_vantage",
                "status": "missing_api_key",
                "api_key_env": api_key_env,
            }

        base_purpose = purpose.split(":")[0] if ":" in purpose else purpose
        request_specs = resolve_skill_request_specs(skill_config, base_purpose)
        if not isinstance(request_specs, list) or not request_specs:
            return {"provider": "alpha_vantage", "status": "no_requests"}

        # When purpose is "prediction:{asset}", filter to asset-relevant requests only
        if purpose.startswith("prediction:"):
            asset_key_filter = purpose.split(":", 1)[1].lower()
            prefixes = ASSET_AV_KEY_PREFIXES.get(asset_key_filter, [])
            request_specs = [
                spec for spec in request_specs
                if isinstance(spec, dict) and (
                    str(spec.get("key") or "").lower() in ALWAYS_INCLUDE_AV_KEYS
                    or any(str(spec.get("key") or "").lower().startswith(p) for p in prefixes)
                )
            ]
            if not request_specs:
                return {"provider": "alpha_vantage", "status": "no_requests"}

        if purpose.startswith("prediction"):
            cache_ttl_seconds = int(
                skill_config.get("prediction_cache_ttl_seconds")
                or skill_config.get("cache_ttl_seconds", 21600)
            )
        else:
            cache_ttl_seconds = int(skill_config.get("cache_ttl_seconds", 21600))
        request_delay_seconds = float(skill_config.get("request_delay_seconds", 0.6))

        def fetch_one(raw_spec: dict[str, Any], request_key: str) -> dict[str, Any]:
            function_name = str(raw_spec.get("function") or "").strip()
            metric_key = str(raw_spec.get("key") or function_name.lower()).strip()
            params = raw_spec.get("params") or {}
            if not function_name or not isinstance(params, dict):
                return {}
            payload = self.alpha_vantage_get(function_name, params, api_key)
            normalized = normalize_alpha_vantage_payload(metric_key, payload, raw_spec)
            return {metric_key: normalized} if normalized is not None else {}

        return run_skill_fetch(
            "alpha_vantage", purpose, request_specs, cache_ttl_seconds, request_delay_seconds, fetch_one
        )

    def fred_get_series_observations(self, series_id: str, params: dict[str, Any], api_key: str) -> dict[str, Any]:
        query = parse.urlencode(
            {
                "series_id": series_id,
                "api_key": api_key,
                "file_type": "json",
                **params,
            }
        )
        data = http_request("GET", f"https://api.stlouisfed.org/fred/series/observations?{query}")
        if not isinstance(data, dict):
            raise ApiError(f"Expected JSON object from FRED for {series_id}")
        error_code = data.get("error_code")
        error_message = data.get("error_message")
        if error_code or error_message:
            raise ApiError(f"FRED error for {series_id}: {error_code or ''} {error_message or ''}".strip())
        return data

    def get_fred_snapshot(self, purpose: str) -> dict[str, Any]:
        skill_config = (self.config.get("skills") or {}).get("fred_economic_data") or {}
        if not skill_config.get("enabled", False):
            return {"provider": "fred_economic_data", "status": "disabled"}

        api_key_env = str(skill_config.get("api_key_env", "FRED_API_KEY"))
        api_key = os.environ.get(api_key_env)
        if not api_key:
            return {
                "provider": "fred_economic_data",
                "status": "missing_api_key",
                "api_key_env": api_key_env,
            }

        request_specs = resolve_skill_request_specs(skill_config, purpose)
        if not isinstance(request_specs, list) or not request_specs:
            return {"provider": "fred_economic_data", "status": "no_requests"}

        cache_ttl_seconds = int(skill_config.get("cache_ttl_seconds", 21600))
        request_delay_seconds = float(skill_config.get("request_delay_seconds", 0.25))

        def fetch_one(raw_spec: dict[str, Any], request_key: str) -> dict[str, Any]:
            series_id = str(raw_spec.get("series_id") or "").strip()
            metric_key = str(raw_spec.get("key") or series_id.lower()).strip()
            params = raw_spec.get("params") or {}
            if not series_id or not isinstance(params, dict):
                return {}
            payload = self.fred_get_series_observations(series_id, params, api_key)
            normalized = normalize_fred_payload(metric_key, payload, raw_spec)
            return {metric_key: normalized} if normalized is not None else {}

        return run_skill_fetch(
            "fred_economic_data", purpose, request_specs, cache_ttl_seconds, request_delay_seconds, fetch_one
        )

    def hedgefundmonitor_get_timeseries(self, mnemonic: str, params: dict[str, Any]) -> list[Any]:
        query = parse.urlencode({"mnemonic": mnemonic, **params})
        data = http_request("GET", f"https://data.financialresearch.gov/hf/v1/series/timeseries?{query}")
        if not isinstance(data, list):
            raise ApiError(f"Expected JSON array from Hedge Fund Monitor for {mnemonic}")
        return data

    def resolve_skill_path(self, relative_path: str) -> Path:
        return (AGENT_HOME / relative_path).resolve()

    def run_akshare_snapshot_script(self, script_path: Path, spec: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
        completed = subprocess.run(
            [sys.executable, str(script_path), "--spec", json.dumps(spec, ensure_ascii=False)],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(AGENT_HOME),
        )
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if stdout:
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                return payload
        if completed.returncode != 0:
            detail = stderr or stdout or f"exit code {completed.returncode}"
            raise ApiError(f"AkShare snapshot script failed: {detail}")
        raise ApiError("AkShare snapshot script did not return usable JSON")

    def get_akshare_snapshot(self, purpose: str) -> dict[str, Any]:
        skill_config = (self.config.get("skills") or {}).get("akshare") or {}
        if not skill_config.get("enabled", False):
            return {"provider": "akshare", "status": "disabled"}

        request_specs = resolve_skill_request_specs(skill_config, purpose)
        if not isinstance(request_specs, list) or not request_specs:
            return {"provider": "akshare", "status": "no_requests"}

        skill_doc_path = str(skill_config.get("skill_doc_path") or "skills/akshare-market-data/SKILL.md")
        script_path = self.resolve_skill_path(str(Path(skill_doc_path).parent / "scripts" / "akshare_snapshot.py"))
        if not script_path.exists():
            return {
                "provider": "akshare",
                "status": "error",
                "error": f"AkShare snapshot script not found at {script_path}",
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }

        cache_ttl_seconds = int(skill_config.get("cache_ttl_seconds", 900))
        request_delay_seconds = float(skill_config.get("request_delay_seconds", 0.25))
        timeout_seconds = int(skill_config.get("command_timeout_seconds", 60))
        request_attempts = max(1, int(skill_config.get("request_attempts", 2)))

        def fetch_one(raw_spec: dict[str, Any], request_key: str) -> dict[str, Any]:
            payload: dict[str, Any] | None = None
            last_error: str | None = None
            for attempt in range(request_attempts):
                if attempt:
                    time.sleep(request_delay_seconds)
                try:
                    payload = self.run_akshare_snapshot_script(script_path, raw_spec, timeout_seconds)
                except subprocess.TimeoutExpired:
                    last_error = f"AkShare request {request_key} timed out after {timeout_seconds} seconds"
                    continue
                except ApiError as exc:
                    last_error = str(exc)
                    continue

                if payload.get("status") == "ok":
                    break
                last_error = str(payload.get("error") or f"AkShare request {request_key} returned {payload.get('status')}")
                payload = None

            if payload and payload.get("status") == "ok":
                payload_metrics = payload.get("metrics") or {}
                return payload_metrics if isinstance(payload_metrics, dict) else {}

            raise ApiError(last_error or f"AkShare request {request_key} failed")

        return run_skill_fetch(
            "akshare", purpose, request_specs, cache_ttl_seconds, request_delay_seconds, fetch_one
        )

    def get_hedgefundmonitor_snapshot(self, purpose: str) -> dict[str, Any]:
        skill_config = (self.config.get("skills") or {}).get("hedgefundmonitor") or {}
        if not skill_config.get("enabled", False):
            return {"provider": "hedgefundmonitor", "status": "disabled"}

        request_specs = resolve_skill_request_specs(skill_config, purpose)
        if not isinstance(request_specs, list) or not request_specs:
            return {"provider": "hedgefundmonitor", "status": "no_requests"}

        cache_ttl_seconds = int(skill_config.get("cache_ttl_seconds", 21600))
        request_delay_seconds = float(skill_config.get("request_delay_seconds", 0.25))

        def fetch_one(raw_spec: dict[str, Any], request_key: str) -> dict[str, Any]:
            mnemonic = str(raw_spec.get("mnemonic") or "").strip()
            metric_key = str(raw_spec.get("key") or mnemonic.lower()).strip()
            params = raw_spec.get("params") or {}
            if not mnemonic or not isinstance(params, dict):
                return {}
            payload = self.hedgefundmonitor_get_timeseries(mnemonic, params)
            normalized = normalize_hedgefundmonitor_payload(metric_key, payload, raw_spec)
            return {metric_key: normalized} if normalized is not None else {}

        return run_skill_fetch(
            "hedgefundmonitor", purpose, request_specs, cache_ttl_seconds, request_delay_seconds, fetch_one
        )

    def get_skill_snapshots(self, purpose: str) -> dict[str, dict[str, Any]]:
        snapshots: dict[str, dict[str, Any]] = {}
        skills_config = self.config.get("skills") or {}
        if not isinstance(skills_config, dict):
            return snapshots

        for entry in _SKILL_REGISTRY:
            skill_config = skills_config.get(entry["key"])
            if isinstance(skill_config, dict) and skill_config.get("enabled", False):
                snapshots[entry["key"]] = getattr(self, entry["fetch"])(purpose)
        return snapshots

    def generation_model_metadata(self, backend: str) -> dict[str, str | None]:
        model_config = self.config.get("model", {}) or {}
        generation = self.config.get("generation", {}) or {}
        deployment = resolve_generation_value(
            generation,
            "deployment",
            resolve_generation_value(generation, "model", model_config.get("name")),
        )
        return {
            "backend": backend,
            "model_provider": str(model_config.get("provider") or "").strip() or None,
            "model_name": str(model_config.get("name") or model_config.get("version") or "").strip() or None,
            "deployment": (str(deployment).strip() or None) if deployment is not None else None,
        }

    def generate_text_with_openai_compatible(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        generation = self.config.get("generation", {})
        request_timeout_seconds = int(generation.get("request_timeout_seconds", 60))
        api_key_env = str(generation.get("api_key_env", "OPENAI_API_KEY"))
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise ApiError(f"Missing API key in environment variable {api_key_env}")

        base_url = str(resolve_generation_value(generation, "api_base_url", "https://api.openai.com/v1")).rstrip("/")
        model_name = str(
            resolve_generation_value(
                generation,
                "model",
                self.config.get("model", {}).get("name", "gpt-5.4"),
            )
        )
        response = http_json_request(
            "POST",
            f"{base_url}/chat/completions",
            body={
                "model": model_name,
                "temperature": generation.get("temperature", 0.4),
                "max_tokens": generation.get("max_tokens", 400),
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
            headers={"Authorization": f"Bearer {api_key}"},
            timeout_seconds=request_timeout_seconds,
        )
        choices = response.get("choices") or []
        if not choices:
            raise ApiError("Model response did not include choices")
        metadata = self.generation_model_metadata("openai_compatible")
        metadata["deployment"] = model_name
        metadata["model_name"] = model_name
        return build_generation_result(response, **metadata)

    def generate_text_with_azure_ai_inference(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        generation = self.config.get("generation", {})
        request_timeout_seconds = int(generation.get("request_timeout_seconds", 60))
        api_key_env = str(generation.get("api_key_env", "AZURE_OPENAI_API_KEY"))
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise ApiError(f"Missing API key in environment variable {api_key_env}")
        base_url = str(resolve_generation_value(generation, "api_base_url", "")).rstrip("/")
        if not base_url:
            raise ApiError("Missing Azure AI Inference base URL")
        model_name = str(resolve_generation_value(
            generation, "deployment",
            resolve_generation_value(generation, "model", self.config.get("model", {}).get("name", "")),
        )).strip()
        if not model_name:
            raise ApiError("Missing Azure AI Inference model/deployment name")
        api_version = str(generation.get("api_version", "2024-05-01-preview")).strip()
        token_parameter = str(generation.get("token_parameter", "max_tokens")).strip() or "max_tokens"
        token_limit = generation.get("max_completion_tokens", generation.get("max_tokens", 400))
        body: dict[str, Any] = {
            "model": model_name,
            "temperature": generation.get("temperature", 0.4),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        body[token_parameter] = token_limit
        url = append_query_params(f"{base_url}/models/chat/completions", {"api-version": api_version})
        response = http_json_request(
            "POST", url, body=body,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout_seconds=request_timeout_seconds,
        )
        choices = response.get("choices") or []
        if not choices:
            raise ApiError("Azure AI Inference response did not include choices")
        metadata = self.generation_model_metadata("azure_ai_inference")
        metadata["deployment"] = model_name
        return build_generation_result(response, **metadata)

    def generate_text_with_azure_openai_deployment(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        generation = self.config.get("generation", {})
        request_timeout_seconds = int(generation.get("request_timeout_seconds", 60))
        api_key_env = str(generation.get("api_key_env", "AZURE_OPENAI_API_KEY"))
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise ApiError(f"Missing API key in environment variable {api_key_env}")

        base_url = str(resolve_generation_value(generation, "api_base_url", "")).rstrip("/")
        if not base_url:
            raise ApiError("Missing Azure Foundry base URL")
        deployment = str(
            resolve_generation_value(
                generation,
                "deployment",
                resolve_generation_value(generation, "model", self.config.get("model", {}).get("name", "gpt-5.4")),
            )
        ).strip()
        if not deployment:
            raise ApiError("Missing Azure Foundry deployment name")

        api_version = str(generation.get("api_version", "2024-10-21")).strip()
        token_parameter = str(generation.get("token_parameter", "max_tokens")).strip() or "max_tokens"
        token_limit = generation.get("max_completion_tokens", generation.get("max_tokens", 400))
        body: dict[str, Any] = {
            "temperature": generation.get("temperature", 0.4),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        body[token_parameter] = token_limit
        url = append_query_params(
            f"{base_url}/openai/deployments/{parse.quote(deployment, safe='')}/chat/completions",
            {"api-version": api_version},
        )
        response = http_json_request(
            "POST",
            url,
            body=body,
            headers={"api-key": api_key},
            timeout_seconds=request_timeout_seconds,
        )
        choices = response.get("choices") or []
        if not choices:
            raise ApiError("Azure Foundry response did not include choices")
        metadata = self.generation_model_metadata("azure_openai_deployment")
        metadata["deployment"] = deployment
        return build_generation_result(response, **metadata)

    def generate_text_with_azure_foundry_anthropic(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        generation = self.config.get("generation", {})
        request_timeout_seconds = int(generation.get("request_timeout_seconds", 60))
        api_key_env = str(generation.get("api_key_env", "AZURE_ANTHROPIC_API_KEY"))
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise ApiError(f"Missing API key in environment variable {api_key_env}")

        base_url = str(resolve_generation_value(generation, "api_base_url", "")).rstrip("/")
        if not base_url:
            raise ApiError("Missing Azure Foundry Anthropic base URL")
        model_name = str(
            resolve_generation_value(
                generation,
                "model",
                self.config.get("model", {}).get("name", "claude-opus-4-6"),
            )
        ).strip()
        if not model_name:
            raise ApiError("Missing Azure Foundry Anthropic deployment name")

        body: dict[str, Any] = {
            "model": model_name,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            "max_tokens": generation.get("max_tokens", 400),
            "temperature": generation.get("temperature", 0.4),
            "stream": False,
        }
        thinking = generation.get("thinking")
        if isinstance(thinking, dict) and thinking:
            body["thinking"] = thinking
        output_config = generation.get("output_config")
        if isinstance(output_config, dict) and output_config:
            body["output_config"] = output_config

        response = http_json_request(
            "POST",
            f"{base_url}/v1/messages",
            body=body,
            headers={
                "x-api-key": api_key,
                "anthropic-version": str(generation.get("anthropic_version", "2023-06-01")),
            },
            timeout_seconds=request_timeout_seconds,
        )
        metadata = self.generation_model_metadata("azure_foundry_anthropic")
        metadata["deployment"] = model_name
        metadata["model_name"] = model_name
        return build_generation_result(response, **metadata)

    def generate_text_with_google_api(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        generation = self.config.get("generation", {})
        request_timeout_seconds = int(generation.get("request_timeout_seconds", 60))
        api_key_env = str(generation.get("api_key_env", "GOOGLE_API_KEY"))
        api_key = (
            os.environ.get(api_key_env)
            or os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
        )
        if not api_key:
            raise ApiError(
                f"Missing API key in environment variable {api_key_env} (or GOOGLE_API_KEY / GEMINI_API_KEY)"
            )

        base_url = str(generation.get("api_base_url", "https://generativelanguage.googleapis.com/v1beta")).rstrip("/")
        model_name = str(generation.get("model") or self.config.get("model", {}).get("version") or "Kimi-K2.5")
        query = parse.urlencode({"key": api_key})
        response = http_json_request(
            "POST",
            f"{base_url}/models/{parse.quote(model_name, safe='')}:{'generateContent'}?{query}",
            body={
                "systemInstruction": {
                    "parts": [{"text": system_prompt}],
                },
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": user_prompt}],
                    }
                ],
                "generationConfig": {
                    "temperature": generation.get("temperature", 0.4),
                    "maxOutputTokens": generation.get("max_tokens", 400),
                },
            },
            timeout_seconds=request_timeout_seconds,
        )
        candidates = response.get("candidates") or []
        if not candidates:
            raise ApiError("Google model response did not include candidates")
        metadata = self.generation_model_metadata("google_api")
        metadata["deployment"] = model_name
        metadata["model_name"] = model_name
        return build_generation_result(response, **metadata)

    def generate_text_with_metadata(self, system_prompt: str, user_prompt: str) -> dict[str, Any] | None:
        generation = self.config.get("generation", {})
        backend = str(generation.get("backend") or infer_generation_backend(self.config)).strip().lower()
        if backend == "template":
            return None
        if backend == "azure_ai_inference":
            try:
                return self.generate_text_with_azure_ai_inference(system_prompt, user_prompt)
            except ApiError as exc:
                raise exc
        if backend in {"openai_api", "openai_compatible"}:
            try:
                return self.generate_text_with_openai_compatible(system_prompt, user_prompt)
            except ApiError as exc:
                raise exc
        if backend in {"azure_openai_deployment", "azure_foundry_openai"}:
            try:
                return self.generate_text_with_azure_openai_deployment(system_prompt, user_prompt)
            except ApiError as exc:
                raise exc
        if backend in {"azure_foundry_anthropic", "anthropic_foundry"}:
            try:
                return self.generate_text_with_azure_foundry_anthropic(system_prompt, user_prompt)
            except ApiError as exc:
                raise exc
        if backend == "google_api":
            try:
                return self.generate_text_with_google_api(system_prompt, user_prompt)
            except ApiError as exc:
                raise exc
        raise ApiError(f"Unsupported generation backend: {backend}")

    def generate_text(self, system_prompt: str, user_prompt: str) -> str | None:
        result = self.generate_text_with_metadata(system_prompt, user_prompt)
        if result is None:
            return None
        content = result.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        raise ApiError("LLM did not return usable content")

    def record_llm_usage(
        self,
        *,
        action: str,
        status: str,
        context: dict[str, Any] | None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        payload = result if isinstance(result, dict) else {}
        storage_llm_usage.record_llm_usage(
            self.persona_id,
            action=action,
            status=status,
            backend=payload.get("backend"),
            model_provider=payload.get("model_provider"),
            model_name=payload.get("model_name"),
            deployment=payload.get("deployment"),
            usage_normalized=payload.get("usage_normalized"),
            usage_raw=payload.get("usage_raw"),
            context=dict(context) if isinstance(context, dict) else {},
            error=error,
        )

    def mark_commented(self, news_id: str) -> None:
        storage_processed_items.mark_processed(
            self.persona_id, "news_comment", news_id, spool_path=self.processed_items_spool_path
        )
        self._touch_last_cycle_at()

    def mark_followed(self, agent_id: str) -> None:
        storage_processed_items.mark_processed(
            self.persona_id, "agent_follow", agent_id, spool_path=self.processed_items_spool_path
        )
        self._touch_last_cycle_at()

    def mark_replied(self, comment_id: str) -> None:
        storage_processed_items.mark_processed(
            self.persona_id, "comment_reply", comment_id, spool_path=self.processed_items_spool_path
        )
        self._touch_last_cycle_at()

    def mark_predicted(self, challenge_id: str, direction: str | None = None, confidence: float | None = None) -> None:
        storage_processed_items.mark_processed(
            self.persona_id, "challenge_prediction", challenge_id, spool_path=self.processed_items_spool_path
        )
        history = self.state.setdefault("prediction_history", {})
        prev = history.get(challenge_id) or {}
        history[challenge_id] = {
            "direction": direction,
            "confidence": confidence,
            "predicted_at": datetime.now(timezone.utc).isoformat(),
            "revision_count": int(prev.get("revision_count") or 0) + (1 if prev else 0),
        }
        self.save_state()
        self._touch_last_cycle_at()

    def get_prediction_history(self, challenge_id: str) -> dict[str, Any] | None:
        return (self.state.get("prediction_history") or {}).get(challenge_id)

    def has_commented(self, news_id: str) -> bool:
        return storage_processed_items.is_processed(self.persona_id, "news_comment", news_id)

    def has_followed(self, agent_id: str) -> bool:
        return storage_processed_items.is_processed(self.persona_id, "agent_follow", agent_id)

    def has_replied(self, comment_id: str) -> bool:
        return storage_processed_items.is_processed(self.persona_id, "comment_reply", comment_id)

    def has_predicted(self, challenge_id: str) -> bool:
        return storage_processed_items.is_processed(self.persona_id, "challenge_prediction", challenge_id)

    def _touch_last_cycle_at(self) -> None:
        storage_credentials.touch_last_cycle_at(self.persona_id, spool_path=self.credentials_spool_path)

    def collect_accuracy_stats(self, challenges_path: str | None = None) -> dict[str, Any]:
        cache = self.state.setdefault("accuracy_stats_cache", {})
        now = datetime.now(timezone.utc).timestamp()
        cached_at = numeric_value(cache.get("cached_at"))
        if cached_at is not None and now - cached_at < 3600 and isinstance(cache.get("stats"), dict):
            return cache["stats"]

        history = self.state.get("prediction_history") or {}
        if not history:
            return {}

        try:
            resolved = self.list_prediction_challenges("resolved", challenges_path=challenges_path)
        except ApiError:
            return cache.get("stats") or {}

        items = resolved.get("items") or []
        by_asset: dict[str, dict[str, int]] = {}
        by_type: dict[str, dict[str, int]] = {}
        total, correct = 0, 0

        for item in items:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or "").strip()
            if cid not in history:
                continue
            resolved_dir = str(
                item.get("result")
                or item.get("resolved_direction")
                or item.get("final_direction")
                or item.get("outcome")
                or ""
            ).strip().lower()
            if resolved_dir not in {"bullish", "bearish", "neutral"}:
                continue

            predicted_dir = str(history[cid].get("direction") or "").lower()
            hit = predicted_dir == resolved_dir
            total += 1
            if hit:
                correct += 1

            asset_key = extract_asset_key(str(item.get("asset") or ""))
            if asset_key:
                bucket = by_asset.setdefault(asset_key, {"total": 0, "correct": 0})
                bucket["total"] += 1
                if hit:
                    bucket["correct"] += 1

            ctype = str(item.get("challenge_type") or "daily").lower()
            tbucket = by_type.setdefault(ctype, {"total": 0, "correct": 0})
            tbucket["total"] += 1
            if hit:
                tbucket["correct"] += 1

        stats: dict[str, Any] = {}
        if total >= 3:
            stats["total"] = total
            stats["correct"] = correct
            stats["accuracy"] = round(correct / total, 2)
            stats["by_asset"] = {
                k: {**v, "accuracy": round(v["correct"] / v["total"], 2)}
                for k, v in by_asset.items() if v["total"] >= 2
            }
            stats["by_type"] = {
                k: {**v, "accuracy": round(v["correct"] / v["total"], 2)}
                for k, v in by_type.items() if v["total"] >= 2
            }

        cache["cached_at"] = now
        cache["stats"] = stats
        self.save_state()
        return stats


_SPACE_KEYWORDS: dict[str, set[str]] = {
    "finance": {
        "finance", "market", "economy", "stock", "equity", "bond", "yield", "rate",
        "inflation", "gdp", "monetary", "fiscal", "fed", "central bank", "interest",
        "earnings", "ipo", "etf", "nasdaq", "s&p", "dow",
    },
    "policy": {
        "policy", "regulation", "government", "election", "congress", "senate",
        "legislation", "regulatory", "sanctions", "tariff", "trade war", "executive",
        "white house", "parliament", "cabinet",
    },
    "ai": {
        "ai", "artificial intelligence", "machine learning", "deep learning", "llm",
        "chatgpt", "openai", "anthropic", "google ai", "nvidia", "semiconductor",
        "chip", "generative", "neural network", "automation", "robotics",
    },
    "international": {
        "international", "global", "geopolitical", "war", "conflict", "diplomacy",
        "sanctions", "foreign", "trade deal", "bilateral", "multilateral", "un ",
        "nato", "g7", "g20", "imf", "world bank", "wto",
    },
    "technology": {
        "technology", "tech", "software", "hardware", "cloud", "cybersecurity",
        "digital", "crypto", "blockchain", "bitcoin", "startup", "venture",
        "silicon valley", "apple", "microsoft", "amazon", "meta", "alphabet",
    },
}

_EVENT_TYPE_SPACE: dict[str, str] = {
    "economic_release": "finance",
    "fed": "finance",
    "macro": "finance",
    "geopolitical": "international",
    "policy": "policy",
    "election": "policy",
    "technology": "technology",
    "ai": "ai",
}


def pick_space(event: dict[str, Any]) -> str:
    event_type = (event.get("event_type") or "").lower()
    if event_type in {"economic_release", "fed", "macro"}:
        return "finance"
    if event_type in {"geopolitical", "policy", "election"}:
        return "policy"
    return "finance"


def pick_space_for_event(event: dict[str, Any], enabled_spaces: list[str]) -> str:
    """Return the best matching space from enabled_spaces for this event.

    Checks event tags and text against _SPACE_KEYWORDS, then falls back to
    event_type mapping. Always returns a value; falls back to first enabled space.
    """
    if not enabled_spaces:
        return pick_space(event)

    tags = event_tags_lower(event)
    haystack = collect_event_text(event)

    # Score each enabled space by keyword hits in tags (weight 2) and full text (weight 1)
    best_space = ""
    best_score = -1
    for space in enabled_spaces:
        keywords = _SPACE_KEYWORDS.get(space, set())
        score = sum(2 for kw in keywords if kw in tags) + sum(1 for kw in keywords if kw in haystack)
        if score > best_score:
            best_score = score
            best_space = space

    if best_score > 0:
        return best_space

    # Fall back to event_type mapping, constrained to enabled_spaces
    event_type = (event.get("event_type") or "").lower()
    type_space = _EVENT_TYPE_SPACE.get(event_type, "finance")
    if type_space in enabled_spaces:
        return type_space

    return enabled_spaces[0]


def event_in_enabled_spaces(event: dict[str, Any], enabled_spaces: list[str]) -> bool:
    """Return True if the event matches at least one enabled space via tags or event_type."""
    if not enabled_spaces:
        return True

    tags = event_tags_lower(event)
    haystack = collect_event_text(event)
    for space in enabled_spaces:
        keywords = _SPACE_KEYWORDS.get(space, set())
        if any(kw in tags or kw in haystack for kw in keywords):
            return True

    # Allow if event_type maps to an enabled space
    event_type = (event.get("event_type") or "").lower()
    return _EVENT_TYPE_SPACE.get(event_type, "finance") in enabled_spaces


def pick_cn_post_space(event: dict[str, Any]) -> str:
    text = collect_event_text(event)
    if any(k in text for k in ("gold", " au", "黄金", "沪金")):
        return "au"
    if any(k in text for k in ("silver", " ag", "白银", "沪银")):
        return "ag"
    if any(k in text for k in ("index", "hs300", "sh300", "沪深", "上证", "股指", "a股", "if ", "ic ", "ih ", "im ")):
        return "index"
    return "general"


def generation_backend_name(config: dict[str, Any]) -> str:
    generation = config.get("generation", {})
    return str(generation.get("backend") or infer_generation_backend(config)).strip().lower()


def uses_template_backend(config: dict[str, Any]) -> bool:
    return generation_backend_name(config) == "template"


def severity_value(level: str) -> int:
    return {"low": 0, "medium": 1, "high": 2, "critical": 3}.get((level or "medium").lower(), 1)


def collect_event_text(event: dict[str, Any]) -> str:
    tags = " ".join(str(tag) for tag in event.get("tags") or [])
    metadata = json.dumps(event.get("metadata") or {}, ensure_ascii=False)
    return " ".join(
        [
            str(event.get("title") or ""),
            str(event.get("title_zh") or ""),
            str(event.get("description") or ""),
            tags,
            metadata,
        ]
    ).lower()


def event_matches_preferences(event: dict[str, Any], config: dict[str, Any]) -> bool:
    prefs = config["content_preferences"]

    enabled_spaces = [s for s in (prefs.get("enabled_spaces") or []) if s]
    if enabled_spaces and not event_in_enabled_spaces(event, enabled_spaces):
        return False

    source_region = prefs.get("source_region")
    if source_region is not None:
        event_region = str(event.get("source_region") or "global").lower()
        if event_region != str(source_region).lower():
            return False

    allowed_event_types = {item.lower() for item in prefs.get("allowed_event_types") or []}
    if allowed_event_types and str(event.get("event_type") or "").lower() not in allowed_event_types:
        return False

    if severity_value(event.get("severity") or "medium") < severity_value(prefs.get("min_severity") or "medium"):
        return False

    haystack = collect_event_text(event)
    blocked_keywords = [item.lower() for item in prefs.get("blocked_keywords") or []]
    if any(keyword in haystack for keyword in blocked_keywords):
        return False

    preferred_keywords = [item.lower() for item in prefs.get("preferred_keywords") or []]
    if preferred_keywords and not any(keyword in haystack for keyword in preferred_keywords):
        return False

    return True


def event_title_for_language(event: dict[str, Any], language: str) -> str:
    if language == "en":
        return str(event.get("title") or event.get("title_zh") or "this event")
    return str(event.get("title_zh") or event.get("title") or "this event")


def parse_datetime_value(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def event_tags_lower(event: dict[str, Any]) -> set[str]:
    return {str(tag).strip().lower() for tag in (event.get("tags") or []) if str(tag).strip()}


def event_mentions(event: dict[str, Any], *keywords: str) -> bool:
    haystack = collect_event_text(event)
    return any(str(keyword).strip().lower() in haystack for keyword in keywords if str(keyword).strip())


def challenge_asset_aliases(challenge: dict[str, Any]) -> set[str]:
    raw_tokens = {
        str(challenge.get("asset") or "").strip().lower(),
        str(challenge.get("question") or "").strip().lower(),
        str(challenge.get("event_id") or "").strip().lower(),
    }
    aliases: set[str] = set()
    for token in raw_tokens:
        if not token:
            continue
        aliases.add(token)
        compact = "".join(char for char in token if char.isalnum())
        if compact:
            aliases.add(compact)
    asset = str(challenge.get("asset") or "").strip().upper()
    if asset == "XAUUSD":
        aliases.update({"gold", "bullion", "precious metal", "safe haven"})
    elif asset == "DX":
        aliases.update({"dollar", "dollar index", "dxy", "usd index", "greenback"})
    elif asset in {"SPX", "ES"}:
        aliases.update({"s&p 500", "s&p500", "sp500", "equities", "stocks", "risk assets", "e-mini", "spx futures"})
    elif asset in {"BTCUSD", "BTC", "XBTUSD"}:
        aliases.update({"bitcoin", "btc", "crypto", "cryptocurrency", "digital asset"})
    return {alias for alias in aliases if alias}


def score_event_for_challenge(challenge: dict[str, Any], event: dict[str, Any]) -> float:
    challenge_event_id = str(challenge.get("event_id") or "").strip()
    if challenge_event_id and challenge_event_id == str(event.get("id") or "").strip():
        return 100.0

    score = 0.0
    aliases = challenge_asset_aliases(challenge)
    if not aliases:
        return score

    haystack = collect_event_text(event)
    tags = event_tags_lower(event)
    title = str(event.get("title") or "").lower()
    description = str(event.get("description") or "").lower()

    for alias in aliases:
        if alias in tags:
            score += 5.0
        if alias in title:
            score += 4.0
        elif alias in description:
            score += 2.0
        elif alias in haystack:
            score += 1.0

    asset = str(challenge.get("asset") or "").strip().upper()
    if asset == "XAUUSD" and bool(event.get("impacts_gold")):
        score += 3.0
    if asset in {"SPX", "ES"} and str(event.get("event_type") or "").lower() in {"policy", "fed", "macro", "economic_release"}:
        score += 1.5
    if asset == "DX" and str(event.get("event_type") or "").lower() in {"policy", "fed", "macro", "economic_release"}:
        score += 1.5
    if asset in {"BTCUSD", "BTC", "XBTUSD"} and str(event.get("event_type") or "").lower() in {"policy", "fed", "macro", "economic_release"}:
        score += 1.0

    score += severity_value(str(event.get("severity") or "medium")) * 0.5
    timestamp = parse_datetime_value(event.get("timestamp"))
    if timestamp:
        hours_old = max(0.0, (datetime.now(timezone.utc) - timestamp).total_seconds() / 3600.0)
        if hours_old <= 6:
            score += 2.0
        elif hours_old <= 24:
            score += 1.0
    return score


def find_linked_event_for_challenge(challenge: dict[str, Any], candidate_events: list[dict[str, Any]]) -> dict[str, Any] | None:
    scored: list[tuple[float, dict[str, Any]]] = []
    for event in candidate_events:
        if not isinstance(event, dict):
            continue
        score = score_event_for_challenge(challenge, event)
        if score > 0:
            scored.append((score, event))
    if not scored:
        return None
    scored.sort(
        key=lambda item: (
            -item[0],
            -severity_value(str(item[1].get("severity") or "medium")),
            str(item[1].get("timestamp") or ""),
        )
    )
    best_score, best_event = scored[0]
    return best_event if best_score >= 4.0 else None


def resolve_skill_request_specs(skill_config: dict[str, Any], purpose: str) -> list[dict[str, Any]]:
    lookup_order = [f"{purpose}_requests"]
    if purpose == "prediction":
        lookup_order.append("comment_requests")
    lookup_order.append("requests")
    for key in lookup_order:
        request_specs = skill_config.get(key) or []
        if isinstance(request_specs, list) and request_specs:
            return request_specs
    return []


def localized_prompt_examples(config: dict[str, Any], key: str, language: str) -> list[str]:
    prompt_style = config.get("prompt_style") or {}
    value = prompt_style.get(key) or []
    if isinstance(value, dict):
        localized = value.get(language) or value.get("en") or []
        return [str(item).strip() for item in localized if str(item).strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def related_event_score(anchor_event: dict[str, Any], candidate_event: dict[str, Any], config: dict[str, Any]) -> float:
    if candidate_event.get("id") == anchor_event.get("id"):
        return -1.0

    score = 0.0
    anchor_type = str(anchor_event.get("event_type") or "").lower()
    candidate_type = str(candidate_event.get("event_type") or "").lower()
    if anchor_type and anchor_type == candidate_type:
        score += 3.0

    tag_overlap = event_tags_lower(anchor_event) & event_tags_lower(candidate_event)
    score += min(len(tag_overlap), 3) * 2.0

    if bool(anchor_event.get("impacts_gold")) and bool(candidate_event.get("impacts_gold")):
        score += 1.5

    preferred_keywords = [item.lower() for item in (config.get("content_preferences", {}).get("preferred_keywords") or [])]
    if preferred_keywords:
        candidate_text = collect_event_text(candidate_event)
        anchor_text = collect_event_text(anchor_event)
        shared_keywords = [keyword for keyword in preferred_keywords if keyword in anchor_text and keyword in candidate_text]
        score += min(len(shared_keywords), 2) * 1.0

    anchor_timestamp = anchor_event.get("timestamp")
    candidate_timestamp = candidate_event.get("timestamp")
    if anchor_timestamp and candidate_timestamp:
        try:
            anchor_dt = datetime.fromisoformat(str(anchor_timestamp))
            candidate_dt = datetime.fromisoformat(str(candidate_timestamp))
            hours_apart = abs((anchor_dt - candidate_dt).total_seconds()) / 3600
            if hours_apart <= 6:
                score += 2.0
            elif hours_apart <= 24:
                score += 1.0
        except ValueError:
            pass

    candidate_severity = severity_value(candidate_event.get("severity") or "medium")
    score += max(0, 3 - candidate_severity) * 0.25
    return score


def select_comment_context_events(
    chosen_event: dict[str, Any],
    candidate_events: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    strategy = config.get("comment_strategy") or {}
    total_limit = max(1, int(strategy.get("context_event_limit", 3)))
    if total_limit <= 1:
        return [chosen_event]

    scored: list[tuple[float, dict[str, Any]]] = []
    for event in candidate_events:
        score = related_event_score(chosen_event, event, config)
        if score > 0:
            scored.append((score, event))

    scored.sort(
        key=lambda item: (
            -item[0],
            severity_value(item[1].get("severity") or "medium"),
            str(item[1].get("timestamp") or ""),
        )
    )
    selected = [chosen_event]
    selected.extend(event for _, event in scored[: total_limit - 1])
    return selected


def format_related_event_lines(related_events: list[dict[str, Any]], language: str) -> list[str]:
    lines: list[str] = []
    for event in related_events:
        title = event_title_for_language(event, language)
        severity = str(event.get("severity") or "").lower()
        description = str(event.get("description") or "").strip()
        line = f"{title} ({severity or 'unspecified'}"
        event_type = str(event.get("event_type") or "").strip()
        if event_type:
            line += f", {event_type}"
        line += ")"
        if description:
            line += f": {description}"
        lines.append(line)
    return lines


def related_events_summary_line(
    anchor_event: dict[str, Any],
    related_events: list[dict[str, Any]],
    language: str,
) -> str | None:
    if not related_events:
        return None

    related_titles = [event_title_for_language(event, language) for event in related_events[:3]]
    if language == "en":
        if len(related_titles) == 1:
            return f"Taken together with {related_titles[0]}, this looks less like an isolated headline and more like a connected market sequence."
        if len(related_titles) == 2:
            return f"Taken together with {related_titles[0]} and {related_titles[1]}, this looks more like a linked macro sequence than a one-off headline."
        return (
            f"Taken together with {related_titles[0]}, {related_titles[1]}, and {related_titles[2]}, "
            "this looks more like a linked macro sequence than a one-off headline."
        )
    if len(related_titles) == 1:
        return f"结合 {related_titles[0]} 一起看，这更像是一段连续的市场叙事，而不是孤立 headline。"
    if len(related_titles) == 2:
        return f"如果把 {related_titles[0]} 和 {related_titles[1]} 放在一起看，这更像是一段连贯的宏观交易线索，而不是单点事件。"
    return f"如果把 {related_titles[0]}、{related_titles[1]} 和 {related_titles[2]} 放在一起看，这更像是一段连贯的宏观交易线索，而不是单点事件。"


def format_knowledge_context(
    similar_predictions: list[dict[str, Any]],
    relevant_lessons: list[dict[str, Any]],
    recent_market_trend: list[dict[str, Any]],
    latest_backtest_report: dict[str, Any] | None,
) -> str:
    lines: list[str] = []
    for lesson in relevant_lessons:
        lesson_text = str(lesson.get("lesson_text") or "").strip()[:500]
        if lesson_text:
            lines.append(f"Lesson: {lesson_text}")

    for pred in similar_predictions:
        reasoning = str(pred.get("reasoning") or "").strip()[:200]
        snippet = f" — {reasoning}" if reasoning else ""
        lines.append(
            f"Similar past prediction ({pred.get('direction')}, confidence {pred.get('confidence')}, "
            f"outcome {pred.get('outcome')}){snippet}"
        )

    if latest_backtest_report:
        metrics = latest_backtest_report.get("metrics") or {}
        win_rate = metrics.get("win_rate")
        total = metrics.get("total_predictions")
        if win_rate is not None and total is not None and isinstance(win_rate, (int, float)):
            lines.append(
                f"Recent backtest: win rate {win_rate:.0%} over {total} predictions "
                f"({latest_backtest_report.get('period_start')} to {latest_backtest_report.get('period_end')})."
            )

    if recent_market_trend:
        capped = recent_market_trend[:3]
        sources = sorted({str(row.get("source") or "") for row in capped if row.get("source")})
        if sources:
            lines.append(f"Recent market data: {len(capped)} snapshot(s) from {', '.join(sources)} in the last 24h.")

    return "\n".join(lines)


def build_comment_prompts(
    event: dict[str, Any],
    config: dict[str, Any],
    language: str,
    skill_snapshot: dict[str, dict[str, Any]] | None = None,
    related_events: list[dict[str, Any]] | None = None,
    similar_predictions: list[dict[str, Any]] | None = None,
    relevant_lessons: list[dict[str, Any]] | None = None,
    recent_market_trend: list[dict[str, Any]] | None = None,
    latest_backtest_report: dict[str, Any] | None = None,
) -> tuple[str, str]:
    persona = config["persona"]
    language_instruction = language_instruction_for(language)
    strategy = config["comment_strategy"]
    banned_phrases = [str(item).strip() for item in (voice_bundle(config, language).get("banned_phrases") or []) if str(item).strip()]
    style_examples = localized_prompt_examples(config, "comment_examples", language)
    anti_examples = localized_prompt_examples(config, "comment_anti_examples", language)
    anti_template_instruction = (
        " Lead with the market implication or repricing consequence, not generic scene-setting."
        " Vary sentence openings and structure."
        " Avoid formulaic scaffolding such as 'what matters here is', 'the key question is', "
        "'I would frame this through', or generic wrap-up phrases."
        " Write like a discretionary desk note, not like a persona template."
    )
    system_prompt = (
        f"You are a {role_label(config, language)}."
        f" Your tone should be {persona.get('tone', 'professional')}, and your writing style should be {persona.get('style', 'clear and restrained')}."
        f" Your areas of strength are: {', '.join(persona.get('specialties') or [])}."
        f" Your core style principle is: {voice_bundle(config, language).get('core_belief', '')}."
        f"{language_instruction} Keep it to 2-4 sentences, avoid boilerplate, sound like a real market participant, and do not open with self-introductions such as 'As a trader' or 'As a macro investor'."
        f"{anti_template_instruction}"
    )
    title = event.get('title') if language == 'en' else (event.get('title_zh') or event.get('title'))
    user_prompt = (
        f"Write a market comment based on the event below.\n"
        f"Title: {title or ''}\n"
        f"Event type: {event.get('event_type') or ''}\n"
        f"Severity: {event.get('severity') or ''}\n"
        f"Description: {event.get('description') or ''}\n"
        f"Tags: {', '.join(str(tag) for tag in event.get('tags') or [])}\n"
        f"Priority assets: {', '.join(strategy.get('focus_assets') or [])}\n"
        f"Closing style requirement: {strategy.get('closing_style') or 'Provide a trading framework for the next 24 to 72 hours'}\n"
        "Writing constraints:\n"
        "- Start with the market move or repricing implication.\n"
        "- Make one concrete directional claim or one clear mixed/uncertain read.\n"
        "- Use at most one explicit 'watch' or 'confirmation' sentence.\n"
        "- Do not turn the comment into a checklist or a summary list.\n"
        "- Avoid canned openers and closers."
    )
    if banned_phrases:
        user_prompt += "\nDo not use these phrases: " + ", ".join(banned_phrases)
    if style_examples:
        user_prompt += "\nReference examples for tone and structure only (do not copy wording):\n"
        user_prompt += "\n".join(f"- {item}" for item in style_examples[:3])
    if anti_examples:
        user_prompt += "\nPatterns to avoid:\n"
        user_prompt += "\n".join(f"- {item}" for item in anti_examples[:3])
    if related_events:
        user_prompt += "\nRelated events to synthesize into the same comment:\n" + "\n".join(
            f"- {line}" for line in format_related_event_lines(related_events, language)
        )
        user_prompt += (
            "\nUse those related events to form one integrated market read, "
            "but keep the comment anchored to the primary event rather than turning it into a list."
        )
    skill_lines = format_skill_lines(skill_snapshot, language)
    if skill_lines:
        user_prompt += "\nOptional external market data:\n" + "\n".join(f"- {line}" for line in skill_lines)
        user_prompt += "\nUse these as pricing-validation clues rather than reciting them mechanically."

    knowledge_block = format_knowledge_context(
        similar_predictions or [], relevant_lessons or [], recent_market_trend or [], latest_backtest_report
    )
    if knowledge_block:
        user_prompt += f"\nRelevant history:\n{knowledge_block}"

    return system_prompt, user_prompt


def generate_comment_content(
    agent: MarketCommentAgent,
    event: dict[str, Any],
    config: dict[str, Any],
    language: str,
    skill_snapshot: dict[str, dict[str, Any]] | None = None,
    related_events: list[dict[str, Any]] | None = None,
) -> str:
    result = generate_comment_result(
        agent,
        event,
        config,
        language,
        skill_snapshot,
        related_events,
    )
    return str(result.get("content") or "").strip()


def generate_comment_result(
    agent: MarketCommentAgent,
    event: dict[str, Any],
    config: dict[str, Any],
    language: str,
    skill_snapshot: dict[str, dict[str, Any]] | None = None,
    related_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    knowledge_query_text = " ".join(
        part for part in [str(event.get("title") or "").strip(), str(event.get("description") or "").strip()] if part
    )
    similar_predictions = (
        storage_retrieval.get_similar_predictions(knowledge_query_text, persona_id=agent.persona_id, asset_key=None, k=5)
        if knowledge_query_text else []
    )
    relevant_lessons = (
        storage_retrieval.get_relevant_lessons(knowledge_query_text, persona_id=agent.persona_id, asset_key=None, k=3)
        if knowledge_query_text else []
    )
    recent_market_trend = storage_retrieval.get_recent_market_trend(asset_key=None, lookback_hours=24)
    latest_backtest_report = storage_retrieval.get_latest_backtest_report(agent.persona_id)

    system_prompt, user_prompt = build_comment_prompts(
        event,
        config,
        language,
        skill_snapshot,
        related_events,
        similar_predictions=similar_predictions,
        relevant_lessons=relevant_lessons,
        recent_market_trend=recent_market_trend,
        latest_backtest_report=latest_backtest_report,
    )
    context = {
        "news_id": str(event.get("id") or ""),
        "language": language,
        "related_event_ids": [
            str(item.get("id") or "")
            for item in (related_events or [])
            if isinstance(item, dict) and item.get("id")
        ],
    }
    try:
        result = agent.generate_text_with_metadata(system_prompt, user_prompt)
    except ApiError as exc:
        agent.record_llm_usage(action="comment", status="error", context=context, result=None, error=str(exc))
        raise
    if isinstance(result, dict):
        agent.record_llm_usage(action="comment", status="success", context=context, result=result)
        return result
    if uses_template_backend(config):
        return {"content": compose_market_comment(event, config, language, skill_snapshot, related_events)}
    raise ApiError("LLM did not return usable comment content; refusing to fall back to a template.")


def compose_market_comment(
    event: dict[str, Any],
    config: dict[str, Any],
    language: str,
    skill_snapshot: dict[str, dict[str, Any]] | None = None,
    related_events: list[dict[str, Any]] | None = None,
) -> str:
    severity = (event.get("severity") or "medium").lower()
    event_type = (event.get("event_type") or "market_event").lower()
    impacts_gold = bool(event.get("impacts_gold"))
    tags = [str(tag).lower() for tag in event.get("tags") or []]
    persona = config["persona"]
    comment_strategy = config["comment_strategy"]
    voice = voice_bundle(config, language)
    skill_line = skill_summary_line(skill_snapshot, language)
    related_line = related_events_summary_line(event, related_events or [], language)
    seed = f"comment:{event.get('id','')}:{persona.get('role','')}"

    if language == "en":
        focus_assets = comment_strategy.get("focus_assets_en") or comment_strategy.get("focus_assets") or [
            "the dollar",
            "rates",
            "equities",
        ]
        asset_group = ", ".join(str(item) for item in focus_assets if str(item).strip()) or "the dollar, rates, and equities"
        if event_type == "geopolitical":
            if impacts_gold or "gold" in tags:
                lead = "This pushes markets toward a higher geopolitical risk premium, with gold and oil bid first and equities left to absorb the growth hit."
            else:
                lead = "This should trade as a risk-off geopolitical headline first, not as a clean growth-positive impulse."
        elif event_type in {"economic_release", "macro", "fed", "policy"}:
            if event_mentions(event, "inflation", "cpi", "ppi", "tariff"):
                lead = "The real trade here is inflation repricing, so the front end and the dollar matter more than the headline summary."
            elif event_mentions(event, "employment", "payroll", "labor", "unemployment"):
                lead = "This is mainly a rates-path headline, and the first read should come from the front end rather than from index-level equity reactions."
            else:
                lead = "This matters only if it shifts the policy path, because that is what the dollar, yields, and equities will actually price."
        else:
            lead = "Price should react through relative repricing across the dollar, yields, and equities, not through headline emotion alone."

        if severity == "critical":
            market_line = "At this intensity, forced positioning and hedge demand can matter as much as the underlying macro signal."
        elif event_mentions(event, "iran", "middle east", "oil", "shipping", "tehran"):
            market_line = "The immediate check is whether oil, gold, and the dollar all confirm the same stress signal; if they do not, the first move can fade."
        elif event_mentions(event, "inflation", "cpi", "ppi"):
            market_line = "If 2Y yields and DXY do not extend in the same direction, the inflation read is probably not strong enough to sustain a second leg."
        elif event_mentions(event, "employment", "payroll", "unemployment"):
            market_line = "For a labor-market shock, I would trust the 2Y and Nasdaq reaction more than the first equity index headline."
        else:
            market_line = f"The useful question is whether {asset_group} confirm the same move instead of fragmenting into a headline-only reaction."

        checkpoint_candidates = []
        if skill_line:
            checkpoint_candidates.append(skill_line)
        if related_line:
            checkpoint_candidates.append(related_line)
        checkpoint_candidates.append(
            "I would only press the view if the next session keeps the same cross-asset direction rather than retracing it."
        )
        close = select_pattern(seed + ":close:en", voice.get("closing_patterns") or []) or comment_strategy.get("closing_style_en") or ""
        parts = [lead, market_line, checkpoint_candidates[0] if checkpoint_candidates else "", close]
    else:
        focus_assets = comment_strategy.get("focus_assets") or ["美元", "利率", "美股"]
        asset_group = "、".join(str(item) for item in focus_assets if str(item).strip()) or "美元、利率和美股"
        if event_type == "geopolitical":
            if impacts_gold or "gold" in tags:
                lead = "这类 headline 更像是在抬升地缘风险溢价，先受影响的通常是黄金、原油和风险资产贴现。"
            else:
                lead = "这更像是典型的 risk-off 地缘冲击，不是可以直接外推增长改善的消息。"
        elif event_type in {"economic_release", "macro", "fed", "policy"}:
            if event_mentions(event, "inflation", "cpi", "ppi", "tariff"):
                lead = "真正该交易的是通胀和政策路径的再定价，所以先看前端利率和美元，而不是 headline 本身。"
            elif event_mentions(event, "employment", "payroll", "labor", "unemployment"):
                lead = "这首先是利率路径的消息，先看前端利率的反应，比看指数 headline 更有信息量。"
            else:
                lead = "只有当它真的改变政策路径时，这条消息才会持续影响美元、利率和风险资产。"
        else:
            lead = "更有价值的读法是看美元、利率和权益资产是否出现同向再定价，而不是先被 headline 带节奏。"

        if severity == "critical":
            market_line = "这种强度下，仓位被动调整和避险需求本身就会放大行情。"
        elif event_mentions(event, "iran", "middle east", "oil", "shipping", "tehran"):
            market_line = "短线先看原油、黄金和美元能不能给出同一个压力信号；如果不同步，第一波走势就容易回吐。"
        elif event_mentions(event, "inflation", "cpi", "ppi"):
            market_line = "如果 2Y 和美元没有同步走强，这种通胀解读大概率不够强，难以支撑第二腿。"
        elif event_mentions(event, "employment", "payroll", "unemployment"):
            market_line = "遇到劳动力市场冲击时，我会更信 2Y 和纳指的反应，而不是指数 headline 的第一眼波动。"
        else:
            market_line = f"关键不是观点是否好听，而是 {asset_group} 能不能给出一致确认。"

        checkpoint_candidates = []
        if skill_line:
            checkpoint_candidates.append(skill_line)
        if related_line:
            checkpoint_candidates.append(related_line)
        checkpoint_candidates.append("如果下一时段不能延续同样的跨资产方向，这种交易叙事就要降级处理。")
        close = select_pattern(seed + ":close", voice.get("closing_patterns") or []) or comment_strategy.get("closing_style") or ""
        parts = [lead, market_line, checkpoint_candidates[0] if checkpoint_candidates else "", close]

    return sanitize_style(join_generated_sentences(parts, language), config)


def format_accuracy_feedback(
    stats: dict[str, Any],
    challenge: dict[str, Any],
) -> str:
    if not stats or stats.get("total", 0) < 3:
        return ""
    lines: list[str] = []
    total = stats["total"]
    accuracy = stats["accuracy"]
    lines.append(f"Your overall prediction accuracy: {stats['correct']}/{total} ({accuracy:.0%}).")

    asset_key = extract_asset_key(str(challenge.get("asset") or ""))
    by_asset = stats.get("by_asset") or {}
    if asset_key and asset_key in by_asset:
        a = by_asset[asset_key]
        lines.append(
            f"On {asset_key.upper()}: {a['correct']}/{a['total']} ({a['accuracy']:.0%})."
        )

    ctype = str(challenge.get("challenge_type") or "daily").lower()
    by_type = stats.get("by_type") or {}
    if ctype in by_type:
        t = by_type[ctype]
        lines.append(
            f"On {ctype} challenges: {t['correct']}/{t['total']} ({t['accuracy']:.0%})."
        )

    if accuracy < 0.5:
        lines.append(
            "Your accuracy is below 50%. Recalibrate: lower confidence when signals conflict,"
            " and lean toward neutral when conviction is weak."
        )
    elif accuracy >= 0.7:
        lines.append("Your accuracy is strong. Maintain your current signal-weighting approach.")
    return " ".join(lines)


def build_prediction_prompts(
    challenge: dict[str, Any],
    config: dict[str, Any],
    language: str,
    skill_snapshot: dict[str, dict[str, Any]] | None = None,
    event: dict[str, Any] | None = None,
    is_revision: bool = False,
    prev_direction: str | None = None,
    prev_confidence: float | None = None,
    accuracy_stats: dict[str, Any] | None = None,
    gold_trade_doc: str | None = None,
    similar_predictions: list[dict[str, Any]] | None = None,
    relevant_lessons: list[dict[str, Any]] | None = None,
    recent_market_trend: list[dict[str, Any]] | None = None,
    latest_backtest_report: dict[str, Any] | None = None,
) -> tuple[str, str]:
    persona = config["persona"]
    language_instruction = language_instruction_for(language)
    system_prompt = (
        f"You are a {role_label(config, language)}."
        f" Your tone should be {persona.get('tone', 'professional')}, and your writing style should be {persona.get('style', 'clear and restrained')}."
        f" Your areas of strength are: {', '.join(persona.get('specialties') or [])}."
        f" {language_instruction} Return only valid JSON with keys direction, confidence, reasoning, and summary."
        " direction must be exactly bullish, bearish, or neutral."
        " confidence must be a number between 0 and 1."
        " reasoning must be detailed, trading-relevant, and at least 20 characters."
        " summary must be a concise 1-3 sentence human-readable rationale no longer than 500 characters."
        " Do not wrap the JSON in markdown fences."
        " When making a prediction, reason in this order: (1) What does the K-line data say about price momentum and trend over the last 12 bars?"
        " (2) What does the macro/event context say about direction? (3) Do they agree or disagree?"
        " (4) Count your signals: how many point bullish vs bearish? If 2 or more signals agree, confidence >= 0.6. If signals are split or only 1 is clear, confidence < 0.5 and consider neutral."
        " Only choose neutral when K-line momentum and macro signals point in opposite directions with roughly equal conviction, or when the K-line shows a clear reversal pattern that contradicts the macro direction."
        " Do not use neutral as a hedge against uncertainty or incomplete information."
    )
    challenge_asset = extract_asset_key(str(challenge.get("asset") or ""))
    if gold_trade_doc and challenge_asset in GOLD_SILVER_ASSET_KEYS:
        system_prompt += (
            "\n\nFor this gold/silver prediction, apply the following analytical framework:\n"
            + gold_trade_doc
        )

    challenge_type = str(challenge.get("challenge_type") or "daily").lower()
    session_name = str(challenge.get("session_name") or "").lower()
    flash_trigger = str(challenge.get("flash_trigger") or "").lower()

    if challenge_type == "flash":
        horizon_line = (
            f"This is a 1-hour flash challenge"
            + (f" triggered by {flash_trigger}" if flash_trigger else "")
            + ". Focus on immediate price momentum and any triggering catalyst — do not anchor on multi-day macro narratives."
        )
    elif challenge_type == "session":
        horizon_line = (
            f"This is a 4-hour session challenge"
            + (f" ({session_name} session)" if session_name else "")
            + ". Focus on current session flow, positioning, and intraday momentum rather than multi-day swing setups."
        )
    else:
        horizon_line = "This is a 24-hour daily challenge. Consider the broader macro picture and multi-session price action."

    if challenge_type == "flash":
        data_guidance_line = (
            "Data priority: focus exclusively on momentum signals and recent price action."
            " Ignore multi-day macro narratives."
            " K-line focus: look only at the last 3 bars — their direction and volume are your primary momentum signal."
        )
    elif challenge_type == "session":
        data_guidance_line = (
            "Data priority: weight intraday momentum signals over macro."
            " Use macro only as a directional filter."
            " K-line focus: compare the first 2 bars (session open bias) with the last 2 bars (current momentum) to determine whether the session is accelerating or fading."
        )
    else:
        data_guidance_line = (
            "Data priority: macro/fundamental signals and technical signals carry equal weight."
            " K-line focus: assess the overall 12-bar trend direction and confirm it aligns with your macro thesis before deciding."
        )

    user_prompt = (
        "Analyze the prediction challenge below and decide the most defensible market direction.\n"
        f"Question: {challenge.get('question') or ''}\n"
        f"Asset: {challenge.get('asset') or ''}\n"
        f"Challenge type: {challenge_type}\n"
        f"Time horizon: {horizon_line}\n"
        f"Status: {challenge.get('status') or ''}\n"
        f"Deadline: {challenge.get('deadline') or ''}\n"
        f"Resolve at: {challenge.get('resolve_at') or ''}\n"
        f"Open price: {challenge.get('open_price') if challenge.get('open_price') is not None else ''}\n"
        f"Data guidance: {data_guidance_line}\n"
    )
    if event:
        user_prompt += (
            f"Linked event title: {event_title_for_language(event, language)}\n"
            f"Linked event type: {event.get('event_type') or ''}\n"
            f"Linked event severity: {event.get('severity') or ''}\n"
            f"Linked event description: {event.get('description') or ''}\n"
            f"Linked event tags: {', '.join(str(tag) for tag in event.get('tags') or [])}\n"
        )
    anchor_line = format_akshare_prediction_context(
        (skill_snapshot or {}).get("akshare"), challenge_asset, language
    )
    skill_lines = format_skill_lines(skill_snapshot, language)

    # #3: Filter data by challenge type — flash sees only K-line/quotes,
    # session drops hedgefundmonitor, daily sees everything.
    if challenge_type == "flash":
        skill_lines = [
            line for line in skill_lines
            if not any(kw in line.lower() for kw in (
                "treasury", "yield", "fed funds", "cpi", "unemployment",
                "dollar index", "high yield", "leverage", "repo", "vix",
            ))
        ]
    elif challenge_type == "session":
        skill_lines = [
            line for line in skill_lines
            if not any(kw in line.lower() for kw in ("leverage", "repo"))
        ]

    if anchor_line or skill_lines:
        user_prompt += "Primary market context — reason from this data before deciding direction:\n"
        if anchor_line:
            user_prompt += f"- {anchor_line}\n"
        for line in skill_lines:
            if anchor_line and anchor_line in line:
                continue  # suppress duplicate of the anchor line
            user_prompt += f"- {line}\n"

    knowledge_block = format_knowledge_context(
        similar_predictions or [], relevant_lessons or [], recent_market_trend or [], latest_backtest_report
    )
    if knowledge_block:
        user_prompt += f"Relevant history from past predictions:\n{knowledge_block}\n"

    accuracy_block = format_accuracy_feedback(accuracy_stats or {}, challenge)
    if accuracy_block:
        user_prompt += f"Performance calibration: {accuracy_block}\n"

    if is_revision and prev_direction:
        direction_changed_hint = (
            f"Your previous prediction was {prev_direction}"
            + (f" with confidence {prev_confidence:.2f}" if prev_confidence is not None else "")
            + ". This is a revision."
            " Your reasoning MUST explain what has changed since your last prediction — new price action, data, or developments that justify updating your view."
            " Do not simply restate the current market setup."
        )
        if prev_direction:
            direction_changed_hint += (
                " If your new direction differs from the previous one, additionally state why the previous direction is no longer supported and explain the flip."
            )
        user_prompt += direction_changed_hint + "\n"

    if is_revision:
        user_prompt += (
            'If the setup is mixed or weak, choose "neutral" with lower confidence.'
            ' Return JSON like {"direction":"bullish","confidence":0.67,"reasoning":"detailed market rationale here","summary":"short market rationale here",'
            '"revision_reason":"one sentence: what new data or event shifted your view, e.g. \'Fed surprise hawkish — dollar strength bearish for gold.\'"}'
        )
    else:
        user_prompt += (
            'If the setup is mixed or weak, choose "neutral" with lower confidence.'
            ' Return JSON like {"direction":"bullish","confidence":0.67,"reasoning":"detailed market rationale here","summary":"short market rationale here"}'
        )
    return system_prompt, user_prompt


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = str(text or "").strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        payload = json.loads(stripped)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        try:
            payload = json.loads(stripped[start : end + 1])
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            pass
    raise ApiError("Prediction response did not contain a usable JSON object")


def normalize_prediction_payload(payload: dict[str, Any]) -> dict[str, Any]:
    direction = str(payload.get("direction") or "").strip().lower()
    if direction not in {"bullish", "bearish", "neutral"}:
        raise ApiError(f"Prediction direction must be bullish, bearish, or neutral; got {direction or 'empty'}")
    confidence = numeric_value(payload.get("confidence"))
    if confidence is None:
        raise ApiError("Prediction confidence was missing or not numeric")
    confidence = max(0.0, min(1.0, confidence))
    reasoning = str(payload.get("reasoning") or "").strip()
    if len(reasoning) < 20:
        raise ApiError("Prediction reasoning must be at least 20 characters to satisfy PredictRequest")
    summary_raw = payload.get("summary")
    summary = str(summary_raw or "").strip() or None
    if summary and len(summary) > 500:
        raise ApiError("Prediction summary must be 500 characters or fewer to satisfy PredictRequest")
    revision_reason_raw = payload.get("revision_reason")
    revision_reason = str(revision_reason_raw or "").strip() or None
    if revision_reason and len(revision_reason) > 1000:
        revision_reason = revision_reason[:1000]
    return {
        "direction": direction,
        "confidence": round(confidence, 4),
        "reasoning": reasoning,
        "summary": summary,
        "revision_reason": revision_reason,
    }


def build_prediction_request(
    decision: dict[str, Any],
    *,
    token_usage: dict[str, Any] | None = None,
    is_revision: bool = False,
    trigger_event_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "direction": str(decision.get("direction") or ""),
        "confidence": float(decision.get("confidence") or 0.0),
        "reasoning": str(decision.get("reasoning") or "").strip(),
        "is_revision": is_revision,
    }
    summary = str(decision.get("summary") or "").strip()
    if summary:
        payload["summary"] = summary
    if is_revision:
        revision_reason = str(decision.get("revision_reason") or "").strip() or None
        if revision_reason:
            payload["revision_reason"] = revision_reason
        if trigger_event_id:
            payload["trigger_event_id"] = trigger_event_id
    if token_usage is not None:
        payload["token_usage"] = token_usage
    return payload


def build_cn_prediction_request(
    decision: dict[str, Any],
) -> dict[str, Any]:
    """Build a CN arena prediction payload: direction bullish/bearish/neutral, confidence, reasoning."""
    direction = str(decision.get("direction") or "").strip().lower()
    reasoning = str(decision.get("reasoning") or decision.get("summary") or "").strip()
    return {
        "direction": direction,
        "confidence": float(decision.get("confidence") or 0.0),
        "reasoning": reasoning,
    }


def is_within_predict_window(challenge: dict[str, Any], windows: list[dict[str, Any]], now: datetime) -> bool:
    """Return False if the challenge asset has predict_windows defined but the current time falls in none of them.

    An asset may have multiple windows (e.g. day session + night session). Returns True if the current
    time falls within ANY matching window. Uses exact-match first, then falls back to substring match,
    so patterns like 'HS300' and 'SH' won't cross-match each other when both are present.
    """
    if not windows:
        return True
    from zoneinfo import ZoneInfo
    asset = str(challenge.get("asset") or "").strip().upper()
    # Prefer exact matches; fall back to substring so 'ES' still matches 'ES1!'
    matching = [w for w in windows if str(w.get("asset_pattern") or "").strip().upper() == asset]
    if not matching:
        matching = [w for w in windows if str(w.get("asset_pattern") or "").strip().upper() and str(w.get("asset_pattern") or "").strip().upper() in asset]
    if not matching:
        return True  # no windows configured for this asset — allow
    for window in matching:
        tz_name = str(window.get("timezone") or "America/New_York")
        try:
            local_now = now.astimezone(ZoneInfo(tz_name))
        except Exception:
            local_now = now
        start_str = str(window.get("window_start") or "")
        end_str = str(window.get("window_end") or "")
        if not start_str or not end_str:
            return True
        try:
            sh, sm = map(int, start_str.split(":"))
            eh, em = map(int, end_str.split(":"))
        except ValueError:
            return True
        current_minutes = local_now.hour * 60 + local_now.minute
        start_minutes = sh * 60 + sm
        end_minutes = eh * 60 + em
        if start_minutes <= current_minutes < end_minutes:
            return True
    return False
    return True


def is_us_trading_day(moment: datetime) -> bool:
    """Return False on US market weekends/holidays (NYSE calendar)."""
    import pandas_market_calendars as mcal
    from zoneinfo import ZoneInfo

    eastern_date = moment.astimezone(ZoneInfo("America/New_York")).date()
    schedule = mcal.get_calendar("NYSE").schedule(start_date=eastern_date, end_date=eastern_date)
    return not schedule.empty


def choose_prediction_candidates(
    agent: MarketCommentAgent,
    config: dict[str, Any],
    candidate_events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]] | None]:
    strategy = config.get("prediction_strategy") or {}
    if not strategy.get("enabled", False):
        return [], [], None

    challenges_path: str | None = strategy.get("challenges_path") or None
    challenges = agent.list_prediction_challenges(str(strategy.get("status") or "open"), challenges_path=challenges_path)
    items = challenges.get("items") or []
    if not isinstance(items, list):
        return [], [], None

    now = datetime.now(timezone.utc)
    max_predictions = max(1, int(strategy.get("max_predictions_per_cycle", 1)))
    skip_predicted = bool(strategy.get("skip_predicted_challenges", True))
    allow_revision = bool(strategy.get("allow_revision", True))
    revision_min_interval = int(strategy.get("revision_min_interval_seconds", 3600))
    revision_confidence_threshold = float(strategy.get("revision_confidence_threshold", 0.15))
    predict_windows = strategy.get("predict_windows") or []
    blocked_challenge_keywords = [kw.lower() for kw in (strategy.get("blocked_challenge_keywords") or [])]
    max_deadline_hours_ahead = int(strategy.get("max_deadline_hours_ahead") or 0)
    event_by_id = {str(event.get("id")): event for event in candidate_events if event.get("id")}
    candidates: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    eligible: list[dict[str, Any]] = []
    revision_eligible: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        challenge_id = str(item.get("id") or "").strip()
        if not challenge_id:
            continue
        challenge_haystack = " ".join([
            str(item.get("asset") or ""),
            str(item.get("title") or ""),
            str(item.get("description") or ""),
            str(item.get("category") or ""),
        ]).lower()
        if blocked_challenge_keywords and any(kw in challenge_haystack for kw in blocked_challenge_keywords):
            continue
        deadline = parse_datetime_value(item.get("deadline"))
        if deadline and deadline <= now:
            continue
        if max_deadline_hours_ahead > 0 and (not deadline or deadline > now + timedelta(hours=max_deadline_hours_ahead)):
            continue
        if str(item.get("status") or "").lower() != "open":
            continue
        if not is_within_predict_window(item, predict_windows, now):
            continue
        if agent.has_predicted(challenge_id):
            if not skip_predicted:
                eligible.append(item)
            elif allow_revision:
                history = agent.get_prediction_history(challenge_id)
                last_predicted_at = parse_datetime_value((history or {}).get("predicted_at"))
                if last_predicted_at is None or (now - last_predicted_at).total_seconds() >= revision_min_interval:
                    revision_eligible.append(item)
        else:
            eligible.append(item)

    if not eligible and not revision_eligible:
        return [], [], None

    skill_snapshot = agent.get_skill_snapshots("prediction")

    def challenge_type_priority(item: dict[str, Any]) -> int:
        ct = str(item.get("challenge_type") or "daily").lower()
        if ct == "flash":
            return 0
        if ct == "session":
            return 1
        return 2

    def sort_key(item: dict[str, Any]) -> tuple[Any, Any, Any]:
        deadline = parse_datetime_value(item.get("deadline"))
        created_at = parse_datetime_value(item.get("created_at"))
        deadline_key = deadline.isoformat() if deadline else "9999-12-31T00:00:00+00:00"
        created_key = created_at.isoformat() if created_at else ""
        return (challenge_type_priority(item), deadline_key, created_key)

    # New predictions first, then revisions; both sorted by challenge type priority then deadline
    work_queue = [
        (challenge, False) for challenge in sorted(eligible, key=sort_key)
    ] + [
        (challenge, True) for challenge in sorted(revision_eligible, key=sort_key)
    ]

    pred_strategy = config.get("prediction_strategy") or {}
    challenges_path: str | None = pred_strategy.get("challenges_path") or None
    accuracy_stats = agent.collect_accuracy_stats(challenges_path=challenges_path)

    for challenge, is_revision in work_queue:
        if len(candidates) >= max_predictions:
            break
        linked_event = event_by_id.get(str(challenge.get("event_id") or "")) or find_linked_event_for_challenge(challenge, candidate_events)
        language = determine_comment_language(config)
        challenge_id = str(challenge.get("id") or "")
        context = {
            "challenge_id": challenge_id,
            "event_id": str((linked_event or {}).get("id") or ""),
            "language": language,
        }
        generation_result: dict[str, Any] | None = None

        # Fetch revision history before generating the prompt so we can pass it in
        prev_direction: str | None = None
        prev_confidence_val: float | None = None
        if is_revision:
            history = agent.get_prediction_history(challenge_id) or {}
            prev_direction = str(history.get("direction") or "") or None
            raw_conf = history.get("confidence")
            if raw_conf is not None:
                prev_confidence_val = float(raw_conf)

        # Fetch asset-scoped AV snapshot (cached 5-min per asset) and merge over the baseline
        challenge_asset = extract_asset_key(str(challenge.get("asset") or ""))
        if challenge_asset:
            av_snapshot = agent.get_alpha_vantage_snapshot(f"prediction:{challenge_asset}")
            challenge_skill_snapshot = dict(skill_snapshot)
            challenge_skill_snapshot["alpha_vantage"] = av_snapshot
        else:
            challenge_skill_snapshot = skill_snapshot

        gold_trade_doc: str | None = None
        if challenge_asset in GOLD_SILVER_ASSET_KEYS:
            gold_trade_doc = load_gold_trade_doc(config)

        knowledge_query_text = " ".join(
            part for part in [
                str(challenge.get("question") or "").strip(),
                str((linked_event or {}).get("description") or "").strip(),
            ] if part
        )
        similar_predictions = (
            storage_retrieval.get_similar_predictions(
                knowledge_query_text, persona_id=agent.persona_id, asset_key=challenge_asset, k=5
            )
            if knowledge_query_text else []
        )
        relevant_lessons = (
            storage_retrieval.get_relevant_lessons(
                knowledge_query_text, persona_id=agent.persona_id, asset_key=challenge_asset, k=3
            )
            if knowledge_query_text else []
        )
        recent_market_trend = (
            storage_retrieval.get_recent_market_trend(challenge_asset) if challenge_asset else []
        )
        latest_backtest_report = storage_retrieval.get_latest_backtest_report(agent.persona_id)

        try:
            system_prompt, user_prompt = build_prediction_prompts(
                challenge,
                config,
                language,
                challenge_skill_snapshot,
                linked_event,
                is_revision=is_revision,
                prev_direction=prev_direction,
                prev_confidence=prev_confidence_val,
                accuracy_stats=accuracy_stats,
                gold_trade_doc=gold_trade_doc,
                similar_predictions=similar_predictions,
                relevant_lessons=relevant_lessons,
                recent_market_trend=recent_market_trend,
                latest_backtest_report=latest_backtest_report,
            )
            generation_result = agent.generate_text_with_metadata(system_prompt, user_prompt)
            content = generation_result.get("content") if isinstance(generation_result, dict) else None
            if not isinstance(content, str) or not content.strip():
                raise ApiError("LLM did not return usable prediction content")
            prediction = normalize_prediction_payload(extract_json_object(content))

            # For revisions, skip submission if nothing meaningful changed
            if is_revision:
                new_direction = prediction.get("direction", "")
                new_confidence = float(prediction.get("confidence") or 0.0)
                direction_changed = new_direction != (prev_direction or "")
                confidence_shifted = abs(new_confidence - (prev_confidence_val or 0.0)) >= revision_confidence_threshold
                if not direction_changed and not confidence_shifted:
                    agent.record_llm_usage(
                        action="prediction_revision_skipped",
                        status="success",
                        context=context,
                        result=generation_result,
                    )
                    continue

            trigger_event_id = str(linked_event.get("id") or "") if linked_event else None
            cn_mode = str(strategy.get("challenge_mode") or "").lower() == "cn"
            if cn_mode:
                request_body = build_cn_prediction_request(prediction)
            else:
                request_body = build_prediction_request(
                    prediction,
                    token_usage=generation_result.get("usage_raw") if isinstance(generation_result, dict) else None,
                    is_revision=is_revision,
                    trigger_event_id=trigger_event_id if is_revision else None,
                )
            agent.record_llm_usage(
                action="prediction",
                status="success",
                context=context,
                result=generation_result,
            )
            candidates.append(
                {
                    "challenge": challenge,
                    "event": linked_event,
                    "prediction": prediction,
                    "request_body": request_body,
                    "is_revision": is_revision,
                }
            )
        except ApiError as exc:
            agent.record_llm_usage(
                action="prediction",
                status="error",
                context=context,
                result=generation_result,
                error=str(exc),
            )
            errors.append(
                {
                    "challenge_id": challenge.get("id"),
                    "error": str(exc),
                }
            )

    return candidates, errors, skill_snapshot


def reply_matches_preferences(comment: dict[str, Any], config: dict[str, Any]) -> bool:
    strategy = config["reply_strategy"]
    if not strategy.get("enabled", True):
        return False
    score = int(comment.get("like_count") or 0) * 2 + int(comment.get("reply_count") or 0)
    if score < int(strategy.get("min_candidate_score", 1)):
        return False
    text = str(comment.get("content") or "").lower()
    keywords = [item.lower() for item in strategy.get("preferred_keywords") or []]
    if keywords and not any(keyword in text for keyword in keywords):
        return False
    preferred_agents = set(strategy.get("preferred_agents") or [])
    comment_agent_id = ((comment.get("agent") or {}).get("agent_id"))
    if preferred_agents and comment_agent_id not in preferred_agents:
        return False
    return True


def compose_reply(
    comment: dict[str, Any],
    config: dict[str, Any],
    language: str,
    skill_snapshot: dict[str, dict[str, Any]] | None = None,
) -> str:
    persona = config["persona"]
    stance = config["reply_strategy"].get("stance") or "supplement and refine"
    focus = ", ".join((config["comment_strategy"].get("focus_assets") or [])[:2])
    voice = voice_bundle(config, language)
    seed = f"reply:{comment.get('comment_id','')}:{persona.get('role','')}"
    opening = select_pattern(seed + ":opening", voice.get("reply_opening_patterns") or [])
    closing = select_pattern(seed + ":closing", voice.get("reply_closing_patterns") or [])
    skill_line = skill_summary_line(skill_snapshot, language)
    if language == "en":
        focus_en = ", ".join((config.get("comment_strategy", {}).get("focus_assets_en") or ["front-end yields", "the dollar"])[:2])
        stance_en = config.get("reply_strategy", {}).get("stance_en") or "add one more pricing condition"
        reply_text = (
            f"{opening.capitalize()}. If price action does not confirm through {focus_en}, "
            f"the narrative can get overextended very quickly. "
            f"{skill_line or ''} "
            f"My bias is to {stance_en}. {closing}"
        )
    else:
        reply_text = (
            f"{opening} If subsequent price action does not confirm through {focus}, "
            f"the durability of this narrative may be overestimated. "
            f"{skill_line or ''} "
            f"My bias is to {stance}. {closing}"
        )
    return sanitize_style(polish_sentence(reply_text, language), config)


def build_reply_prompts(
    comment: dict[str, Any],
    config: dict[str, Any],
    language: str,
    skill_snapshot: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, str]:
    persona = config["persona"]
    strategy = config["reply_strategy"]
    language_instruction = language_instruction_for(language)
    system_prompt = (
        f"You are a {role_label(config, language)}, with strengths in {', '.join(persona.get('specialties_en') or ['Fed expectations', 'rates pricing', 'macro risk'])}."
        f" Your reply style should {strategy.get('stance', 'supplement and refine')}, stay professional and restrained, and avoid attacking the other person."
        f"{language_instruction} Keep it to 2-3 sentences and sound like a professional market participant discussing another view."
    )
    agent = comment.get("agent") or {}
    user_prompt = (
        f"Reply to the following comment from another agent.\n"
        f"Other agent: {agent.get('name') or ''}\n"
        f"Other model: {agent.get('model_provider') or ''} {agent.get('model_name') or ''}\n"
        f"Comment content: {comment.get('content') or ''}\n"
        f"Your goal: add one trading or risk-management angle without repeating the original comment."
    )
    skill_lines = format_skill_lines(skill_snapshot, language)
    if skill_lines:
        user_prompt += "\nOptional external market data:\n" + "\n".join(f"- {line}" for line in skill_lines)
        user_prompt += "\nUse these data points to add a validation framework instead of simply restating the original comment."
    return system_prompt, user_prompt


def generate_reply_content(
    agent: MarketCommentAgent,
    comment: dict[str, Any],
    config: dict[str, Any],
    language: str,
    skill_snapshot: dict[str, dict[str, Any]] | None = None,
) -> str:
    result = generate_reply_result(
        agent,
        comment,
        config,
        language,
        skill_snapshot,
    )
    return str(result.get("content") or "").strip()


def generate_reply_result(
    agent: MarketCommentAgent,
    comment: dict[str, Any],
    config: dict[str, Any],
    language: str,
    skill_snapshot: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    reply_system_prompt, reply_user_prompt = build_reply_prompts(
        comment,
        config,
        language,
        skill_snapshot,
    )
    context = {
        "comment_id": str(comment.get("comment_id") or ""),
        "news_id": str(comment.get("news_id") or ""),
        "language": language,
        "target_agent_id": str((comment.get("agent") or {}).get("agent_id") or ""),
    }
    try:
        result = agent.generate_text_with_metadata(reply_system_prompt, reply_user_prompt)
    except ApiError as exc:
        agent.record_llm_usage(action="reply", status="error", context=context, result=None, error=str(exc))
        raise
    if isinstance(result, dict):
        agent.record_llm_usage(action="reply", status="success", context=context, result=result)
        return result
    if uses_template_backend(config):
        return {"content": compose_reply(comment, config, language, skill_snapshot)}
    raise ApiError("LLM did not return usable reply content; refusing to fall back to a template.")


def extract_follow_candidates(public_comments: dict[str, Any], self_agent_id: str) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for comment in public_comments.get("comments") or []:
        agent = comment.get("agent") or {}
        agent_id = agent.get("agent_id")
        if not agent_id or agent_id == self_agent_id:
            continue
        score = int(comment.get("like_count") or 0) * 2 + int(comment.get("reply_count") or 0)
        if agent_id not in candidates or score > candidates[agent_id]["score"]:
            candidates[agent_id] = {
                "agent_id": agent_id,
                "name": agent.get("name"),
                "model_provider": agent.get("model_provider"),
                "model_name": agent.get("model_name"),
                "score": score,
            }
    return sorted(candidates.values(), key=lambda item: (-item["score"], item["agent_id"]))


def extract_reply_candidates(public_comments: dict[str, Any], self_agent_id: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for comment in public_comments.get("comments") or []:
        agent = comment.get("agent") or {}
        if agent.get("agent_id") in {None, self_agent_id}:
            continue
        if reply_matches_preferences(comment, config):
            candidates.append(comment)
    return sorted(
        candidates,
        key=lambda item: (-(int(item.get("like_count") or 0) * 2 + int(item.get("reply_count") or 0)), item.get("comment_id") or ""),
    )


def sort_events_for_comment(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}

    def key(event: dict[str, Any]) -> tuple[Any, Any]:
        severity = (event.get("severity") or "medium").lower()
        timestamp = event.get("timestamp") or ""
        return (severity_rank.get(severity, 9), -int(datetime.fromisoformat(timestamp).timestamp()) if timestamp else 0)

    return sorted(events, key=key)


def load_candidate_events(agent: MarketCommentAgent, config: dict[str, Any]) -> list[dict[str, Any]]:
    cn_mode = str((config.get("prediction_strategy") or {}).get("challenge_mode") or "").lower() == "cn"
    if cn_mode:
        return sort_events_for_comment(agent.get_cn_events(since_hours=48, limit=50))
    recent_event_limit = int(config.get("runtime", {}).get("recent_event_limit", 50))
    events_today = agent.get_events_today()
    if events_today:
        return sort_events_for_comment(events_today)
    return sort_events_for_comment(agent.get_recent_events(recent_event_limit))


def print_json(data: Any) -> None:
    # default=str: DB rows carry datetime/UUID values that json.dumps otherwise rejects.
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def run_cycle(agent: MarketCommentAgent, dry_run: bool, follow_limit: int) -> dict[str, Any]:
    config = agent.reload_config()
    follow_strategy = config["follow_strategy"]
    reply_strategy = config["reply_strategy"]
    all_candidate_events = load_candidate_events(agent, config)
    events = [event for event in all_candidate_events if event_matches_preferences(event, config)]
    prediction_candidates, prediction_errors, prediction_skill_snapshot = choose_prediction_candidates(
        agent,
        config,
        all_candidate_events,
    )
    if not events and not prediction_candidates:
        result: dict[str, Any] = {
            "selected_event": None,
            "context_events": [],
            "follow_candidates": [],
            "reply_candidates": [],
            "prediction_candidates": [],
            "prediction_errors": prediction_errors,
            "actions": [],
            "dry_run": dry_run,
            "status": "idle",
            "reason": "No events matched the configured preferences and no eligible prediction challenges were found",
            "skill_context": {},
        }
        if prediction_skill_snapshot:
            result["skill_context"]["prediction"] = prediction_skill_snapshot
        return result

    comment_payload: dict[str, Any] | None = None
    context_events: list[dict[str, Any]] = []
    follow_candidates: list[dict[str, Any]] = []
    reply_candidates: list[dict[str, Any]] = []
    comment_skill_snapshot: dict[str, dict[str, Any]] | None = None

    if events:
        skip_commented_events = config["comment_strategy"].get("skip_commented_events", True)
        chosen_event = next(
            (event for event in events if not skip_commented_events or not agent.has_commented(event["id"])),
            events[0],
        )
        news_id = chosen_event["id"]
        context_events = select_comment_context_events(chosen_event, events, config)
        related_events = [event for event in context_events if event.get("id") != news_id]
        comment_language = determine_comment_language(config)
        comment_skill_snapshot = agent.get_skill_snapshots("comment")
        enabled_spaces = [s for s in (config["content_preferences"].get("enabled_spaces") or []) if s]
        comment_space_id = config["comment_strategy"].get("space_id") or pick_space_for_event(chosen_event, enabled_spaces)
        comment_payload = {
            "news_id": news_id,
            "space_id": comment_space_id,
            "content": generate_comment_content(
                agent,
                chosen_event,
                config,
                comment_language,
                comment_skill_snapshot,
                related_events,
            ),
            "title": chosen_event.get("title") if comment_language == "en" else (chosen_event.get("title_zh") or chosen_event.get("title")),
        }

        public_comments = agent.get_public_comments(news_id)
        follow_candidates = extract_follow_candidates(public_comments, agent.credentials.get("agent_id", ""))
        follow_candidates = [
            item
            for item in follow_candidates
            if not agent.has_followed(item["agent_id"])
            and item["score"] >= int(follow_strategy.get("min_candidate_score", 0))
        ]
        if follow_strategy.get("preferred_model_providers"):
            allowed_providers = {provider.lower() for provider in follow_strategy.get("preferred_model_providers") or []}
            filtered_candidates = [
                item for item in follow_candidates if (item.get("model_provider") or "").lower() in allowed_providers
            ]
            if filtered_candidates:
                follow_candidates = filtered_candidates
        if follow_strategy.get("preferred_agents"):
            preferred_agents = set(follow_strategy.get("preferred_agents") or [])
            preferred_candidates = [item for item in follow_candidates if item["agent_id"] in preferred_agents]
            if preferred_candidates:
                follow_candidates = preferred_candidates
        follow_candidates = follow_candidates[: min(follow_limit, int(follow_strategy.get("max_follows_per_cycle", follow_limit)))]

        reply_candidates = extract_reply_candidates(public_comments, agent.credentials.get("agent_id", ""), config)
        reply_candidates = [item for item in reply_candidates if not agent.has_replied(item.get("comment_id") or "")]
        reply_candidates = reply_candidates[: int(reply_strategy.get("max_replies_per_cycle", 1))]

    result: dict[str, Any] = {
        "selected_event": comment_payload,
        "context_events": [
            {
                "id": event.get("id"),
                "title": event.get("title"),
                "severity": event.get("severity"),
                "event_type": event.get("event_type"),
            }
            for event in context_events
        ],
        "skill_context": {
        },
        "follow_candidates": follow_candidates,
        "reply_candidates": [
            {
                "comment_id": item.get("comment_id"),
                "agent": item.get("agent"),
                "content": item.get("content"),
            }
            for item in reply_candidates
        ],
        "prediction_candidates": [
            {
                "challenge": item.get("challenge"),
                "event": {
                    "id": item.get("event", {}).get("id"),
                    "title": item.get("event", {}).get("title"),
                    "severity": item.get("event", {}).get("severity"),
                    "event_type": item.get("event", {}).get("event_type"),
                } if isinstance(item.get("event"), dict) else None,
                "prediction": item.get("prediction"),
                "request_body": item.get("request_body"),
            }
            for item in prediction_candidates
        ],
        "prediction_errors": prediction_errors,
        "actions": [],
        "dry_run": dry_run,
    }
    if comment_skill_snapshot:
        result["skill_context"]["comment"] = comment_skill_snapshot
    if prediction_skill_snapshot:
        result["skill_context"]["prediction"] = prediction_skill_snapshot

    if dry_run:
        return result

    if comment_payload and config["comment_strategy"].get("enabled", True) and not agent.has_commented(comment_payload["news_id"]):
        cn_comment_mode = str((config.get("prediction_strategy") or {}).get("challenge_mode") or "").lower() == "cn"
        if cn_comment_mode:
            comment_result = agent.post_cn_comment(comment_payload["news_id"], comment_payload["content"])
        else:
            comment_result = agent.post_comment(comment_payload["news_id"], comment_payload["content"], comment_payload["space_id"])
        agent.mark_commented(comment_payload["news_id"])
        record_comment_history(agent, comment_payload)
        result["actions"].append({"type": "comment", "result": comment_result})

    reply_strategy = config["reply_strategy"]
    if comment_payload and reply_strategy.get("enabled", True):
        reply_skill_snapshot = agent.get_skill_snapshots("reply")
        result["skill_context"]["reply"] = reply_skill_snapshot
        for candidate in reply_candidates:
            comment_id = candidate.get("comment_id")
            if not comment_id:
                continue
            reply_language = determine_reply_language(candidate, config)
            reply_content = generate_reply_content(
                agent,
                candidate,
                config,
                reply_language,
                reply_skill_snapshot,
            )
            reply_result = agent.post_reply(comment_id, reply_content)
            agent.mark_replied(comment_id)
            result["actions"].append({"type": "reply", "comment_id": comment_id, "result": reply_result})

    if follow_strategy.get("enabled", True):
        for candidate in follow_candidates:
            follow_result = agent.follow_agent(candidate["agent_id"])
            agent.mark_followed(candidate["agent_id"])
            result["actions"].append({"type": "follow", "agent_id": candidate["agent_id"], "result": follow_result})

    if config.get("prediction_strategy", {}).get("enabled", False):
        pred_strategy = config.get("prediction_strategy") or {}
        cn_mode = str(pred_strategy.get("challenge_mode") or "").lower() == "cn"
        predict_path_template: str | None = pred_strategy.get("predict_path_template") or None
        if prediction_candidates and not cn_mode:
            result["scope_update"] = agent.ensure_scopes(["prediction:submit"])
        for candidate in prediction_candidates:
            challenge = candidate.get("challenge") or {}
            challenge_id = str(challenge.get("id") or "").strip()
            prediction = candidate.get("prediction") or {}
            if cn_mode:
                request_body = candidate.get("request_body") or build_cn_prediction_request(prediction)
            else:
                request_body = candidate.get("request_body") or build_prediction_request(prediction)
            if not challenge_id or not isinstance(prediction, dict):
                continue
            prediction_result = agent.submit_prediction(
                challenge_id,
                request_body,
                predict_path_template=predict_path_template,
            )
            agent.mark_predicted(
                challenge_id,
                direction=str(prediction.get("direction") or ""),
                confidence=float(prediction.get("confidence") or 0.0),
            )
            record_prediction_history(agent, challenge, challenge_id, prediction)
            result["actions"].append({"type": "prediction", "challenge_id": challenge_id, "result": prediction_result})

    return result


_BACKFILL_PAGE_SIZE = 100
_BACKFILL_MAX_PAGES = 50


def run_backfill_outcomes(agent: "MarketCommentAgent") -> dict[str, Any]:
    pred_strategy = agent.config.get("prediction_strategy") or {}
    configured_challenges_path = pred_strategy.get("challenges_path") or None

    # CN (and any other persona with a custom challenges_path) uses a differently-shaped
    # endpoint that doesn't support resolved_since/offset -- keep the old single-page behavior.
    if configured_challenges_path:
        resolved = agent.list_prediction_challenges("resolved", challenges_path=configured_challenges_path)
        items = resolved.get("items") or []
        updated = storage_settlement.backfill_outcomes(agent.persona_id, items)
        return {"persona_id": agent.persona_id, "resolved_items": len(items), "updated": updated}

    earliest_pending = storage_settlement.get_earliest_pending_date(agent.persona_id)
    earliest_pending_shadow_dt = storage_strategy_cards.get_earliest_pending_shadow_date(agent.persona_id)
    earliest_pending_shadow = earliest_pending_shadow_dt.date() if earliest_pending_shadow_dt else None
    pending_dates = [d for d in (earliest_pending, earliest_pending_shadow) if d is not None]
    if not pending_dates:
        return {"persona_id": agent.persona_id, "resolved_items": 0, "updated": 0, "shadow_updated": 0}
    earliest_pending = min(pending_dates)

    # 1-day safety margin: a challenge can resolve the same day it was created.
    resolved_since = (earliest_pending - timedelta(days=1)).isoformat()
    items: list[dict[str, Any]] = []
    offset = 0
    for _ in range(_BACKFILL_MAX_PAGES):
        path = (
            f"/api/v1/eval/challenges?status=resolved&resolved_since={resolved_since}"
            f"&limit={_BACKFILL_PAGE_SIZE}&offset={offset}"
        )
        resolved = agent.list_prediction_challenges("resolved", challenges_path=path)
        page_items = resolved.get("items") or []
        items.extend(page_items)
        offset += len(page_items)
        if len(page_items) < _BACKFILL_PAGE_SIZE or offset >= (resolved.get("total") or 0):
            break

    updated = storage_settlement.backfill_outcomes(agent.persona_id, items)
    shadow_updated = storage_strategy_cards.backfill_shadow_outcomes(agent.persona_id, items)
    return {
        "persona_id": agent.persona_id,
        "resolved_items": len(items),
        "updated": updated,
        "shadow_updated": shadow_updated,
    }


def build_reflection_prompt(asset_key: str, strategy: str, rows: list[dict[str, Any]]) -> tuple[str, str]:
    system_prompt = (
        "You are a trading performance analyst. Review a batch of settled predictions for one asset and "
        "strategy, and distill exactly one concise, actionable lesson (2-4 sentences) that should inform "
        "future predictions. Focus on patterns in what went right or wrong. Respond with plain text only."
    )
    correct = sum(1 for row in rows if row.get("outcome") == "correct")
    lines = [f"Asset: {asset_key}, Strategy: {strategy}, Sample size: {len(rows)}, Correct: {correct}/{len(rows)}", ""]
    for row in rows:
        reasoning = ""
        model_output = row.get("model_output")
        if isinstance(model_output, dict):
            reasoning = str(model_output.get("reasoning") or "").strip()
        lines.append(
            f"- direction={row.get('direction')}, confidence={row.get('confidence')}, "
            f"outcome={row.get('outcome')}, reasoning={reasoning[:300]}"
        )
    return system_prompt, "\n".join(lines)


def run_generate_reflections(agent: "MarketCommentAgent", lookback_hours: int) -> dict[str, Any]:
    rows = storage_reflection.select_settled_predictions_for_reflection(agent.persona_id, lookback_hours)
    groups = storage_reflection.group_by_asset_and_strategy(rows)
    generated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for (asset_key, strategy), group_rows in groups.items():
        if len(group_rows) < storage_reflection.MIN_SAMPLE_SIZE:
            skipped.append(
                {"asset_key": asset_key, "strategy": strategy, "sample_size": len(group_rows), "reason": "insufficient_sample"}
            )
            continue
        system_prompt, user_prompt = build_reflection_prompt(asset_key, strategy, group_rows)
        context = {"asset_key": asset_key, "strategy": strategy}
        try:
            generation_result = agent.generate_text_with_metadata(system_prompt, user_prompt)
        except ApiError as exc:
            agent.record_llm_usage(action="reflection", status="error", context=context, result=None, error=str(exc))
            skipped.append({"asset_key": asset_key, "strategy": strategy, "reason": str(exc)})
            continue
        content = generation_result.get("content") if isinstance(generation_result, dict) else None
        if not isinstance(content, str) or not content.strip():
            skipped.append({"asset_key": asset_key, "strategy": strategy, "reason": "empty_llm_response"})
            continue
        agent.record_llm_usage(action="reflection", status="success", context=context, result=generation_result)
        supporting_ids = [str(row["id"]) for row in group_rows]
        storage_reflection.save_lesson(agent.persona_id, asset_key, strategy, content.strip(), supporting_ids, len(group_rows))
        generated.append({"asset_key": asset_key, "strategy": strategy, "sample_size": len(group_rows)})
    return {"persona_id": agent.persona_id, "generated": generated, "skipped": skipped}


def run_generate_backtest_report(agent: "MarketCommentAgent", period_days: int) -> dict[str, Any]:
    period_end = datetime.now(timezone.utc).date()
    period_start = period_end - timedelta(days=period_days)
    metrics = storage_backtest.compute_backtest_metrics(agent.persona_id, period_start, period_end)
    if metrics is None:
        return {"persona_id": agent.persona_id, "status": "skipped", "reason": "no_settled_predictions"}
    storage_backtest.save_backtest_report(agent.persona_id, period_start, period_end, metrics)
    return {
        "persona_id": agent.persona_id,
        "status": "ok",
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "metrics": metrics,
    }


_STRATEGY_PROPOSAL_FIELD_DESCRIPTIONS = {
    "blocked_challenge_keywords": "list of keywords; challenges whose title/description/asset/category contain any of these are skipped entirely",
    "max_predictions_per_cycle": "integer cap on how many challenges this persona will predict on in a single cycle",
    "revision_confidence_threshold": "float 0-1; a revision is only submitted if confidence shifted by at least this much (or direction changed)",
    "revision_min_interval_seconds": "integer; minimum seconds between two revisions of the same challenge",
    "allow_revision": "boolean; whether this persona is allowed to revise an already-submitted prediction at all",
    "skip_predicted_challenges": "boolean; whether to skip challenges this persona has already predicted on",
}


def build_strategy_proposal_prompt(
    persona_id: str,
    prediction_strategy: dict[str, Any],
    backtest_metrics: dict[str, Any],
    lessons: list[dict[str, Any]],
    track_record: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    system_prompt = (
        "You are a trading strategy analyst reviewing one persona's recent prediction performance. "
        "You may propose changing AT MOST ONE of the following config fields, using their exact current values as "
        "the baseline:\n"
        + "\n".join(
            f"- {field}: current value = {prediction_strategy.get(field)!r}. {desc}"
            for field, desc in _STRATEGY_PROPOSAL_FIELD_DESCRIPTIONS.items()
        )
        + "\nIf nothing in the data below warrants a change, set proposed_field to null and still classify the signal. "
        "Never propose a field that is not in the list above. "
        "Before proposing, check the track record below: do not re-propose a field/value combination that was "
        "previously rejected, or that was applied and measurably regressed performance, unless your rationale "
        "explains what has genuinely changed since then. "
        'Respond with JSON only, like {"signal_type":"overconfidence","rationale":"...",'
        '"proposed_field":"revision_confidence_threshold","proposed_value":0.25} or '
        '{"signal_type":"low_volume","rationale":"...","proposed_field":null,"proposed_value":null}. '
        "signal_type must be exactly one of: overconfidence, underconfidence, revision_thrash, stale_keyword_filter, "
        "low_volume, other."
    )
    lines = [
        f"Persona: {persona_id}",
        f"Backtest metrics: {json.dumps(backtest_metrics)}",
        "Recent lessons:",
    ]
    for lesson in lessons:
        lines.append(f"- ({lesson.get('asset_key')}/{lesson.get('strategy_scope')}) {lesson.get('lesson_text')}")
    if not lessons:
        lines.append("(none)")

    lines.append("Track record of your past proposals for this persona (most recent first):")
    for entry in track_record or []:
        outcome = entry.get("outcome_status")
        outcome_note = ""
        if outcome and outcome not in ("not_yet_evaluated",):
            metrics = entry.get("outcome_metrics") or {}
            before = metrics.get("before_win_rate")
            after = metrics.get("after_win_rate")
            if before is not None and after is not None:
                outcome_note = f", outcome: {outcome} (win rate {before:.1%} -> {after:.1%})"
            else:
                outcome_note = f", outcome: {outcome}"
        lines.append(
            f"- {entry.get('proposed_field')} -> {entry.get('proposed_value')!r}: "
            f"{entry.get('review_status')}{outcome_note}"
        )
    if not track_record:
        lines.append("(none yet)")

    return system_prompt, "\n".join(lines)


def _generate_strategy_proposal_signal(
    agent: "MarketCommentAgent", config: dict[str, Any], system_prompt: str, user_prompt: str, context: dict[str, Any]
) -> tuple[str, str, Any, Any]:
    """Returns (signal_type, rationale, proposed_field, proposed_value).

    Routes through the LangChain agentic loop (Langfuse-traceable) when this persona has
    observability.langfuse.enabled, otherwise through the plain per-backend HTTP path used by
    every other deterministic-flow LLM call (generate-reflections, predict-open, etc).
    """
    langfuse_config = (config.get("observability") or {}).get("langfuse") or {}
    if langfuse_config.get("enabled", False):
        chat_model = build_langchain_chat_model(config)
        callbacks, langfuse_metadata = build_langfuse_callbacks(config, agent.persona_id, "strategy_proposal")

        def record_turn(message: Any) -> None:
            usage_metadata = getattr(message, "usage_metadata", None) or {}
            agent.record_llm_usage(
                action="strategy_proposal_agentic_turn",
                status="success",
                context=context,
                result={"usage_raw": dict(usage_metadata) if usage_metadata else None},
            )

        result = run_agentic_generation(
            chat_model, [], system_prompt, user_prompt, StrategyProposalOutput,
            on_turn=record_turn, callbacks=callbacks, metadata=langfuse_metadata,
        )
        proposed_value = json.loads(result["proposed_value_json"]) if result.get("proposed_value_json") else None
        return result["signal_type"], result["rationale"], result.get("proposed_field"), proposed_value

    generation_result = agent.generate_text_with_metadata(system_prompt, user_prompt)
    content = generation_result.get("content") if isinstance(generation_result, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ApiError("empty_llm_response")
    agent.record_llm_usage(action="strategy_proposal", status="success", context=context, result=generation_result)
    payload = extract_json_object(content)
    return (
        str(payload.get("signal_type") or "other").strip().lower(),
        str(payload.get("rationale") or "").strip(),
        payload.get("proposed_field"),
        payload.get("proposed_value"),
    )


def run_generate_strategy_proposals(
    agent: "MarketCommentAgent", config: dict[str, Any], lookback_days: int = 30
) -> dict[str, Any]:
    # Close the loop on prior applies before proposing anything new.
    for pending_eval in storage_strategy_proposals.list_proposals_needing_evaluation(agent.persona_id):
        storage_strategy_proposals.evaluate_applied_proposal_outcome(str(pending_eval["id"]))

    period_end = datetime.now(timezone.utc).date()
    period_start = period_end - timedelta(days=lookback_days)
    metrics = storage_backtest.compute_backtest_metrics(agent.persona_id, period_start, period_end)
    if metrics is None:
        return {"persona_id": agent.persona_id, "status": "idle", "reason": "no_settled_predictions"}

    prediction_strategy = config.get("prediction_strategy") or {}
    lessons = storage_strategy_proposals.get_recent_lessons_for_persona(agent.persona_id)
    track_record = storage_strategy_proposals.get_proposal_track_record(agent.persona_id)
    system_prompt, user_prompt = build_strategy_proposal_prompt(
        agent.persona_id, prediction_strategy, metrics, lessons, track_record
    )

    context = {"lookback_days": lookback_days}
    try:
        signal_type, rationale, proposed_field, proposed_value = _generate_strategy_proposal_signal(
            agent, config, system_prompt, user_prompt, context
        )
    except ApiError as exc:
        agent.record_llm_usage(action="strategy_proposal", status="error", context=context, result=None, error=str(exc))
        return {"persona_id": agent.persona_id, "status": "skipped", "reason": str(exc)}

    if not proposed_field:
        return {"persona_id": agent.persona_id, "status": "ok", "signal_type": signal_type, "proposal": None}

    proposed_field = str(proposed_field).strip()
    if proposed_field not in storage_strategy_proposals.ALLOWED_PROPOSAL_FIELDS:
        return {
            "persona_id": agent.persona_id,
            "status": "skipped",
            "reason": "field_not_whitelisted",
            "proposed_field": proposed_field,
        }

    current_value = prediction_strategy.get(proposed_field)

    validation_status = "not_simulatable"
    validation_metrics: dict[str, Any] | None = None
    if proposed_field == "revision_confidence_threshold":
        current_threshold = numeric_value(current_value)
        proposed_threshold = numeric_value(proposed_value)
        if current_threshold is not None and proposed_threshold is not None:
            validation_metrics = storage_strategy_proposals.simulate_revision_confidence_threshold(
                agent.persona_id, current_threshold, proposed_threshold
            )
            validation_status = validation_metrics.get("status", "error")

    save_result = storage_strategy_proposals.save_proposal(
        persona_id=agent.persona_id,
        signal_type=signal_type,
        rationale=rationale,
        proposed_field=proposed_field,
        current_value=current_value,
        proposed_value=proposed_value,
        supporting_backtest_report_id=None,
        supporting_lesson_ids=[str(lesson["id"]) for lesson in lessons],
        validation_status=validation_status,
        validation_metrics=validation_metrics,
    )
    if save_result.get("status") == "skipped_duplicate":
        return {
            "persona_id": agent.persona_id,
            "status": "skipped",
            "reason": "duplicate_pending",
            "proposed_field": proposed_field,
        }

    return {
        "persona_id": agent.persona_id,
        "status": "ok",
        "signal_type": signal_type,
        "proposal": {
            "proposed_field": proposed_field,
            "current_value": current_value,
            "proposed_value": proposed_value,
            "validation_status": validation_status,
            "validation_metrics": validation_metrics,
        },
    }


def build_strategy_card_prompt(
    persona_id: str,
    active_card: dict[str, Any] | None,
    lessons: list[dict[str, Any]],
    metrics: dict[str, Any],
    card_history: list[dict[str, Any]],
) -> tuple[str, str]:
    system_prompt = (
        "You maintain your own trading strategy card: a short, structured set of rules distilled from"
        " your own settled prediction record. Propose the next version of the card."
        " Keep only rules the evidence supports; drop or rewrite rules that did not help."
        " Every rule must be concrete and checkable at prediction time (not generic advice like"
        " 'be careful'). Limits: at most 6 entry_rules, 4 no_trade_conditions, 6 known_failure_modes,"
        " each under 240 characters; confidence_guidance under 480 characters."
        " You have tools to inspect your own past predictions, lessons, market trend, and backtest"
        " report -- use them if the summaries below are not enough."
    )
    lines = [
        "Propose the next version of your strategy card.",
        "",
        f"Current active card: {json.dumps(active_card.get('card'), ensure_ascii=False) if active_card else 'none (you are currently trading without a card)'}",
        "",
        f"Latest 30-day backtest metrics: {json.dumps(metrics, ensure_ascii=False, default=str)}",
    ]
    if lessons:
        lines.append("")
        lines.append("Recent distilled lessons from your settled predictions:")
        lines.extend(
            f"- ({lesson.get('asset_key')}/{lesson.get('strategy_scope')}) {lesson.get('lesson_text')}"
            for lesson in lessons
        )
    if card_history:
        lines.append("")
        lines.append(
            "Your card version history (learn from it: which past changes were promoted, rejected by"
            " shadow testing, or rolled back by the canary check, and why):"
        )
        for row in card_history:
            lines.append(
                f"- v{row.get('version')} [{row.get('status')}/{row.get('source')}"
                f"{'/canary:' + str(row.get('canary_status')) if row.get('canary_status') else ''}]"
                f" {str(row.get('rationale') or '')[:300]}"
            )
    return system_prompt, "\n".join(lines)


def run_evolve_strategy_card(agent: "MarketCommentAgent", config: dict[str, Any]) -> dict[str, Any]:
    """One tick of the strategy-card evolution state machine.

    Priority order: (1) decide a pending candidate once enough paired settled shadow samples
    exist (promote or reject), (2) evaluate a promoted card's canary window (pass or roll back),
    (3) otherwise, past the mutation cooldown, generate a new candidate from settled evidence.
    All effects are confined to the strategy_cards/shadow_predictions tables -- config and code
    are never touched.
    """
    strategy_card_config = config.get("strategy_card") or {}
    if not strategy_card_config.get("enabled", False):
        return {"persona_id": agent.persona_id, "status": "skipped", "reason": "strategy_card_not_enabled"}

    persona_id = agent.persona_id
    min_shadow_samples = int(strategy_card_config.get("min_shadow_samples", 20))
    promote_margin = float(strategy_card_config.get("promote_margin", 0.01))
    canary_min_samples = int(strategy_card_config.get("canary_min_samples", 20))
    rollback_margin = float(strategy_card_config.get("rollback_margin", 0.02))
    cooldown_days = int(strategy_card_config.get("mutation_cooldown_days", 7))

    active = storage_strategy_cards.get_active_card(persona_id)
    candidate = storage_strategy_cards.get_candidate_card(persona_id)

    if candidate is not None:
        baseline_version = int(active["version"]) if active else 0
        comparison = storage_strategy_cards.compare_shadow_brier(
            persona_id, int(candidate["version"]), baseline_version
        )
        if comparison is None or comparison["paired_n"] < min_shadow_samples:
            return {
                "persona_id": persona_id,
                "status": "waiting_for_shadow_samples",
                "candidate_version": candidate["version"],
                "needed": min_shadow_samples,
                "comparison": comparison,
            }
        if comparison["candidate_brier"] <= comparison["baseline_brier"] - promote_margin:
            outcome = storage_strategy_cards.promote_candidate(persona_id)
            return {"persona_id": persona_id, "status": "promoted", "outcome": outcome, "comparison": comparison}
        outcome = storage_strategy_cards.reject_candidate(
            persona_id,
            f"shadow Brier {comparison['candidate_brier']} did not beat baseline"
            f" {comparison['baseline_brier']} by margin {promote_margin} over {comparison['paired_n']} paired samples",
        )
        return {"persona_id": persona_id, "status": "rejected", "outcome": outcome, "comparison": comparison}

    if active is not None and active.get("canary_status") == "pending":
        activated_at = active.get("activated_at")
        canary = storage_strategy_cards.get_live_brier(persona_id, created_after=activated_at)
        baseline = storage_strategy_cards.get_live_brier(
            persona_id, created_before=activated_at, limit=max(canary_min_samples * 2, 40)
        )
        if canary["n"] < canary_min_samples or baseline["n"] < canary_min_samples:
            return {
                "persona_id": persona_id,
                "status": "canary_waiting",
                "active_version": active["version"],
                "canary": canary,
                "baseline": baseline,
                "needed": canary_min_samples,
            }
        if canary["brier"] > baseline["brier"] + rollback_margin:
            outcome = storage_strategy_cards.rollback_active(
                persona_id,
                f"canary Brier {canary['brier']} (n={canary['n']}) regressed vs baseline"
                f" {baseline['brier']} (n={baseline['n']}) beyond margin {rollback_margin}",
            )
            return {"persona_id": persona_id, "status": "rolled_back", "outcome": outcome,
                    "canary": canary, "baseline": baseline}
        storage_strategy_cards.set_canary_status(persona_id, int(active["version"]), "passed")
        return {"persona_id": persona_id, "status": "canary_passed", "active_version": active["version"],
                "canary": canary, "baseline": baseline}

    last_event_at = storage_strategy_cards.get_last_card_event_at(persona_id)
    if last_event_at is not None and datetime.now(timezone.utc) - last_event_at < timedelta(days=cooldown_days):
        return {
            "persona_id": persona_id,
            "status": "cooldown",
            "last_card_event_at": last_event_at.isoformat(),
            "cooldown_days": cooldown_days,
        }

    period_end = datetime.now(timezone.utc).date()
    period_start = period_end - timedelta(days=30)
    metrics = storage_backtest.compute_backtest_metrics(persona_id, period_start, period_end)
    if metrics is None:
        return {"persona_id": persona_id, "status": "idle", "reason": "no_settled_predictions"}

    lessons = storage_strategy_proposals.get_recent_lessons_for_persona(persona_id)
    card_history = storage_strategy_cards.get_card_history(persona_id)
    system_prompt, user_prompt = build_strategy_card_prompt(persona_id, active, lessons, metrics, card_history)

    chat_model = build_langchain_chat_model(config)
    knowledge_tools = build_knowledge_tools(persona_id)
    callbacks, langfuse_metadata = build_langfuse_callbacks(config, f"{persona_id}-card", "strategy_card")
    try:
        card_output = run_agentic_generation(
            chat_model, knowledge_tools, system_prompt, user_prompt, StrategyCardOutput,
            callbacks=callbacks, metadata=langfuse_metadata,
        )
    except Exception as exc:
        agent.record_llm_usage(action="strategy_card", status="error", context={}, result=None, error=str(exc))
        return {"persona_id": persona_id, "status": "skipped", "reason": str(exc)}

    card = {
        field: value
        for field, value in card_output.items()
        if field in storage_strategy_cards.CARD_FIELDS and value
    }
    outcome = storage_strategy_cards.create_candidate(
        persona_id, card, str(card_output.get("rationale") or ""), source="reflection"
    )
    status = "candidate_created" if outcome.get("status") == "created" else str(outcome.get("status"))
    return {"persona_id": persona_id, "status": status, "outcome": outcome, "card": card}


def build_agentic_prediction_prompt(
    challenge: dict[str, Any],
    event: dict[str, Any] | None,
    config: dict[str, Any],
    strategy_card: dict[str, Any] | None = None,
) -> tuple[str, str]:
    persona = config.get("persona") or {}
    language = determine_comment_language(config)
    system_prompt = (
        f"You are a {role_label(config, language)}."
        f" Your tone should be {persona.get('tone', 'professional')}, and your writing style should be"
        f" {persona.get('style', 'clear and restrained')}."
        " You have tools available to look up your own past predictions, distilled lessons, recent market"
        " data, and your recent backtest performance, as well as tools to fetch current market data from"
        " various sources. Use whichever tools are relevant to this specific challenge before deciding --"
        " you do not need to call every tool, and flash/short-horizon challenges typically need less macro"
        " context than daily challenges. Before finalizing, use at least one live market-data tool (not just"
        " historical retrieval) to confirm your reasoning reflects current, not stale, conditions."
        " Once you have enough information, respond with your final prediction."
        " direction must be exactly bullish, bearish, or neutral. confidence must be a number between 0 and 1."
        " reasoning must be detailed, trading-relevant, and at least 20 characters."
    )
    if strategy_card:
        card_lines = storage_strategy_cards.format_card_for_prompt(strategy_card)
        if card_lines:
            system_prompt += (
                "\n\nYour strategy card -- rules you distilled from your own settled trades."
                " Follow them unless current evidence clearly contradicts them:\n" + card_lines
            )
    challenge_type = str(challenge.get("challenge_type") or "daily").lower()
    user_prompt = (
        "Analyze the prediction challenge below and decide the most defensible market direction.\n"
        f"Question: {challenge.get('question') or ''}\n"
        f"Asset: {challenge.get('asset') or ''}\n"
        f"Challenge type: {challenge_type}\n"
        f"Status: {challenge.get('status') or ''}\n"
        f"Deadline: {challenge.get('deadline') or ''}\n"
    )
    if event:
        user_prompt += (
            f"Linked event title: {event_title_for_language(event, language)}\n"
            f"Linked event description: {event.get('description') or ''}\n"
        )
    return system_prompt, user_prompt


def select_eligible_challenges_for_agentic_pilot(
    agent: "MarketCommentAgent", config: dict[str, Any], candidate_events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    strategy = config.get("prediction_strategy") or {}
    if not strategy.get("enabled", False):
        return []

    challenges_path: str | None = strategy.get("challenges_path") or None
    challenges = agent.list_prediction_challenges(str(strategy.get("status") or "open"), challenges_path=challenges_path)
    items = challenges.get("items") or []
    if not isinstance(items, list):
        return []

    now = datetime.now(timezone.utc)
    max_predictions = max(1, int(strategy.get("max_predictions_per_cycle", 1)))
    predict_windows = strategy.get("predict_windows") or []
    blocked_challenge_keywords = [kw.lower() for kw in (strategy.get("blocked_challenge_keywords") or [])]
    max_deadline_hours_ahead = int(strategy.get("max_deadline_hours_ahead") or 0)

    eligible: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        challenge_id = str(item.get("id") or "").strip()
        if not challenge_id:
            continue
        challenge_haystack = " ".join([
            str(item.get("asset") or ""),
            str(item.get("title") or ""),
            str(item.get("description") or ""),
            str(item.get("category") or ""),
        ]).lower()
        if blocked_challenge_keywords and any(kw in challenge_haystack for kw in blocked_challenge_keywords):
            continue
        deadline = parse_datetime_value(item.get("deadline"))
        if deadline and deadline <= now:
            continue
        if max_deadline_hours_ahead > 0 and (not deadline or deadline > now + timedelta(hours=max_deadline_hours_ahead)):
            continue
        if str(item.get("status") or "").lower() != "open":
            continue
        if not is_within_predict_window(item, predict_windows, now):
            continue
        if agent.has_predicted(challenge_id):
            continue
        eligible.append(item)

    return eligible[:max_predictions]


def run_agentic_prediction_cycle(agent: "MarketCommentAgent", config: dict[str, Any]) -> dict[str, Any]:
    if not is_us_trading_day(datetime.now(timezone.utc)):
        return {"status": "idle", "reason": "Not a US market trading day"}
    candidate_events = load_candidate_events(agent, config)
    event_by_id = {str(event.get("id")): event for event in candidate_events if event.get("id")}
    eligible = select_eligible_challenges_for_agentic_pilot(agent, config, candidate_events)
    if not eligible:
        return {"status": "idle", "reason": "No eligible prediction challenges were found"}

    chat_model = build_langchain_chat_model(config)
    knowledge_tools = build_knowledge_tools(agent.persona_id)
    skill_tools = build_skill_tools(
        _SKILL_REGISTRY,
        config.get("skills") or {},
        lambda fetch_name, purpose: getattr(agent, fetch_name)(purpose),
        determine_comment_language(config),
    )
    tools = knowledge_tools + skill_tools

    strategy_card_config = config.get("strategy_card") or {}
    active_card = (
        storage_strategy_cards.get_active_card(agent.persona_id)
        if strategy_card_config.get("enabled", False)
        else None
    )

    predictions = []
    for challenge in eligible:
        linked_event = event_by_id.get(str(challenge.get("event_id") or "")) or find_linked_event_for_challenge(
            challenge, candidate_events
        )
        prediction = generate_agentic_prediction_for_challenge(
            agent,
            config,
            chat_model,
            tools,
            skill_tools,
            knowledge_tools,
            challenge,
            linked_event,
            strategy_card=active_card.get("card") if active_card else None,
        )
        predictions.append({"challenge": challenge, "event": linked_event, "prediction": prediction})

    return {
        "status": "ok",
        "predictions": predictions,
        "active_card_version": int(active_card["version"]) if active_card else 0,
    }


def generate_agentic_prediction_for_challenge(
    agent: "MarketCommentAgent",
    config: dict[str, Any],
    chat_model: Any,
    tools: list,
    skill_tools: list,
    knowledge_tools: list,
    challenge: dict[str, Any],
    linked_event: dict[str, Any] | None,
    strategy_card: dict[str, Any] | None = None,
    usage_action: str = "prediction_agentic_turn",
    trace_persona_id: str | None = None,
) -> dict[str, Any]:
    challenge_id = str(challenge.get("id") or "")
    system_prompt, user_prompt = build_agentic_prediction_prompt(
        challenge, linked_event, config, strategy_card=strategy_card
    )

    def record_turn(message: Any, challenge_id: str = challenge_id) -> None:
        usage_metadata = getattr(message, "usage_metadata", None) or {}
        backend = str(config.get("generation", {}).get("backend") or "")
        agent.record_llm_usage(
            action=usage_action,
            status="success",
            context={"challenge_id": challenge_id},
            result={
                "backend": backend,
                "model_provider": None,
                "model_name": None,
                "deployment": None,
                "usage_raw": dict(usage_metadata) if usage_metadata else None,
                "usage_normalized": {
                    "prompt_tokens": usage_metadata.get("input_tokens"),
                    "completion_tokens": usage_metadata.get("output_tokens"),
                    "total_tokens": usage_metadata.get("total_tokens"),
                }
                if usage_metadata
                else None,
            },
        )

    # "-pilot" suffix distinguishes this observe-only pilot's traces from the same
    # persona_id's real strategy_proposal traces in Langfuse (both read the same
    # config file, so agent.persona_id alone is identical for both).
    callbacks, langfuse_metadata = build_langfuse_callbacks(
        config, trace_persona_id or f"{agent.persona_id}-pilot", "prediction"
    )
    return run_agentic_generation(
        chat_model, tools, system_prompt, user_prompt, PredictionOutput, on_turn=record_turn,
        callbacks=callbacks, metadata=langfuse_metadata,
        require_tool_groups=[
            frozenset(t.name for t in skill_tools),
            frozenset(t.name for t in knowledge_tools),
        ],
    )


def run_agentic_prediction_cycle_live(
    agent: "MarketCommentAgent", config: dict[str, Any], dry_run: bool = False
) -> dict[str, Any]:
    """Like run_agentic_prediction_cycle, but actually submits each prediction -- for a dedicated,
    separately-registered agent identity only (never the observe-only pilot's persona_id).

    Each candidate is submitted independently: a failure (scope error, transient 5xx, etc.) on one
    challenge is recorded on that item and does NOT stop the remaining candidates in this cycle from
    being attempted -- a single stuck candidate previously could silently block every other real
    prediction for the rest of its deadline window.
    """
    result = run_agentic_prediction_cycle(agent, config)
    if result.get("status") != "ok":
        return result

    strategy = config.get("prediction_strategy") or {}
    min_confidence = float(strategy.get("min_confidence") or 0.0)

    if not dry_run:
        result["scope_update"] = agent.ensure_scopes(["prediction:submit"])

    actions = []
    for item in result["predictions"]:
        challenge = item.get("challenge") or {}
        prediction = item.get("prediction") or {}
        challenge_id = str(challenge.get("id") or "").strip()
        if not challenge_id:
            continue
        request_body = build_prediction_request(prediction)
        item["request_body"] = request_body
        confidence = float(prediction.get("confidence") or 0.0)
        if confidence <= min_confidence:
            item["skipped_reason"] = f"confidence {confidence:.2f} at or below min_confidence {min_confidence:.2f}"
            continue
        if dry_run:
            continue
        try:
            response = agent.submit_prediction(challenge_id, request_body)
            agent.mark_predicted(challenge_id, direction=str(prediction.get("direction") or ""), confidence=confidence)
            record_prediction_history(agent, challenge, challenge_id, prediction)
        except Exception as exc:
            item["error"] = str(exc)
            continue
        actions.append({"type": "prediction", "challenge_id": challenge_id, "result": response})
    result["actions"] = actions

    strategy_card_config = config.get("strategy_card") or {}
    if strategy_card_config.get("enabled", False) and not dry_run and result["predictions"]:
        # Baseline arm of the shadow A/B: this cycle's real generations, keyed by the card
        # version they were generated under (0 = card-less). Recorded regardless of whether
        # the prediction cleared min_confidence and got submitted, so candidate comparisons
        # are paired on the full generated set, not just the traded subset.
        active_version = int(result.get("active_card_version") or 0)
        for item in result["predictions"]:
            challenge = item.get("challenge") or {}
            prediction = item.get("prediction") or {}
            challenge_id = str(challenge.get("id") or "").strip()
            if not challenge_id or not isinstance(prediction, dict):
                continue
            storage_strategy_cards.record_shadow_prediction(
                agent.persona_id,
                active_version,
                challenge_id,
                str(challenge.get("asset") or "") or None,
                str(prediction.get("direction") or ""),
                float(prediction.get("confidence") or 0.0),
                str(prediction.get("reasoning") or "") or None,
            )
        candidate = storage_strategy_cards.get_candidate_card(agent.persona_id)
        if candidate:
            result["shadow"] = run_shadow_predictions(agent, config, result["predictions"], candidate)
    return result


def run_shadow_predictions(
    agent: "MarketCommentAgent",
    config: dict[str, Any],
    predictions: list[dict[str, Any]],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Re-generate this cycle's predictions under the candidate strategy card, observe-only.

    Nothing is submitted; results land in shadow_predictions so evolve-strategy-card can run a
    paired Brier comparison against the active card's generations on the same challenges.
    """
    chat_model = build_langchain_chat_model(config)
    knowledge_tools = build_knowledge_tools(agent.persona_id)
    skill_tools = build_skill_tools(
        _SKILL_REGISTRY,
        config.get("skills") or {},
        lambda fetch_name, purpose: getattr(agent, fetch_name)(purpose),
        determine_comment_language(config),
    )
    tools = knowledge_tools + skill_tools

    generated = 0
    errors: list[dict[str, Any]] = []
    for item in predictions:
        challenge = item.get("challenge") or {}
        challenge_id = str(challenge.get("id") or "").strip()
        if not challenge_id:
            continue
        try:
            prediction = generate_agentic_prediction_for_challenge(
                agent,
                config,
                chat_model,
                tools,
                skill_tools,
                knowledge_tools,
                challenge,
                item.get("event"),
                strategy_card=candidate.get("card") or {},
                usage_action="shadow_prediction_agentic_turn",
                trace_persona_id=f"{agent.persona_id}-shadow",
            )
            storage_strategy_cards.record_shadow_prediction(
                agent.persona_id,
                int(candidate["version"]),
                challenge_id,
                str(challenge.get("asset") or "") or None,
                str(prediction.get("direction") or ""),
                float(prediction.get("confidence") or 0.0),
                str(prediction.get("reasoning") or "") or None,
            )
            generated += 1
        except Exception as exc:
            errors.append({"challenge_id": challenge_id, "error": str(exc)})
    return {"card_version": int(candidate["version"]), "generated": generated, "errors": errors}


# Civic Index / Human Forecast (prediction-contract-v2): (outcome_shape, input_encoding)
# pairs this runtime knows how to submit. Anything else fails closed and is skipped.
_SUPPORTED_CIVIC_SHAPES = {
    ("numeric_distribution", "normal_mean_std"),
    ("binary_probability", "yes_probability"),
}
_CIVIC_EXECUTION_FAMILIES = {"macro_numeric", "human_forecast"}


def civic_question_text(challenge: dict[str, Any]) -> str:
    question = challenge.get("question")
    if isinstance(question, dict):
        return str(question.get("en") or next(iter(question.values()), ""))
    return str(question or "")


def select_open_civic_entries(agent: "MarketCommentAgent", config: dict[str, Any]) -> list[dict[str, Any]]:
    civic_config = config.get("civic_forecast") or {}
    data = agent.list_prediction_contracts()
    if data.get("api_contract_version") != "prediction-contract-v2":
        print(
            f"select_open_civic_entries: unsupported contract version "
            f"{data.get('api_contract_version')!r}, failing closed",
            file=sys.stderr,
        )
        return []
    blocked_targets = {str(t).upper() for t in (civic_config.get("blocked_targets") or [])}
    max_per_cycle = max(1, int(civic_config.get("max_forecasts_per_cycle", 2)))
    now = datetime.now(timezone.utc)

    eligible: list[dict[str, Any]] = []
    for entry in data.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        contract = entry.get("contract") or {}
        challenge = entry.get("current_challenge") or {}
        if contract.get("execution_family") not in _CIVIC_EXECUTION_FAMILIES:
            continue
        if str(challenge.get("status") or "").lower() != "open":
            continue
        challenge_id = str(challenge.get("challenge_id") or "").strip()
        if not challenge_id:
            continue
        if str(contract.get("target_key") or "").upper() in blocked_targets:
            continue
        schema = contract.get("forecast_schema") or {}
        shape_key = (str(contract.get("outcome_shape") or ""), str(schema.get("input_encoding") or ""))
        if shape_key not in _SUPPORTED_CIVIC_SHAPES:
            continue
        deadline = parse_datetime_value(challenge.get("deadline"))
        if deadline is not None and deadline <= now:
            continue
        if agent.has_civic_forecasted(challenge_id):
            continue
        eligible.append({"contract": contract, "challenge": challenge})
        if len(eligible) >= max_per_cycle:
            break
    return eligible


def normalize_civic_forecast(
    outcome_shape: str, forecast_schema: dict[str, Any], output: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Fit the LLM's forecast to the contract's frozen schema: round to the contract's precision,
    clamp into the advertised bounds, and reject non-finite or schema-violating values."""
    errors: list[str] = []
    if outcome_shape == "numeric_distribution":
        mean = numeric_value(output.get("mean"))
        std = numeric_value(output.get("std"))
        if mean is None or not math.isfinite(mean):
            return {}, ["mean is missing or not finite"]
        if std is None or not math.isfinite(std) or std <= 0:
            return {}, ["std is missing, not finite, or not positive"]
        precision = forecast_schema.get("value_precision")
        if precision is not None:
            mean = round(mean, int(precision))
            std = round(std, int(precision))
            if std <= 0:
                std = 10 ** -int(precision)
        value_min = numeric_value(forecast_schema.get("value_min"))
        value_max = numeric_value(forecast_schema.get("value_max"))
        if value_min is not None and mean < value_min:
            mean = value_min
        if value_max is not None and mean > value_max:
            mean = value_max
        return {"mean": mean, "std": std}, errors
    if outcome_shape == "binary_probability":
        probability = numeric_value(output.get("yes_probability"))
        if probability is None or not math.isfinite(probability):
            return {}, ["yes_probability is missing or not finite"]
        lower = numeric_value(forecast_schema.get("probability_min"))
        upper = numeric_value(forecast_schema.get("probability_max"))
        probability = max(0.0 if lower is None else lower, min(1.0 if upper is None else upper, probability))
        return {"yes_probability": probability}, errors
    return {}, [f"unsupported outcome_shape: {outcome_shape}"]


def build_civic_forecast_prompt(
    contract: dict[str, Any], challenge: dict[str, Any], config: dict[str, Any]
) -> tuple[str, str]:
    persona = config.get("persona") or {}
    schema = contract.get("forecast_schema") or {}
    outcome_shape = str(contract.get("outcome_shape") or "")
    system_prompt = (
        f"You are a {role_label(config, 'en')}."
        f" Your tone should be {persona.get('tone', 'professional')}."
        " You are forecasting the actual value of an official statistic before its scheduled release."
        " You have tools to look up your own past predictions, distilled lessons, recent market data,"
        " your backtest performance, and live market/economic data sources (FRED historical series are"
        " especially relevant here). Use the relevant tools before deciding -- ground your forecast in"
        " the indicator's recent trend, the stated market consensus, and any fresher signals."
    )
    if outcome_shape == "numeric_distribution":
        bounds = []
        if schema.get("value_min") is not None:
            bounds.append(f"minimum {schema['value_min']}")
        if schema.get("value_max") is not None:
            bounds.append(f"maximum {schema['value_max']}")
        system_prompt += (
            " Respond with mean (your point estimate of the released value, in the indicator's own unit)"
            " and std (your one-sigma uncertainty, strictly positive). Scoring rewards calibration:"
            " an overconfident tiny std is punished when you miss."
        )
        if bounds:
            system_prompt += f" The value is constrained to {', '.join(bounds)}."
    else:
        system_prompt += (
            " Respond with yes_probability: your probability between 0 and 1 that the stated outcome"
            " occurs. Scoring rewards calibration, not boldness."
        )
    deadline = challenge.get("deadline") or ""
    user_prompt = (
        "Forecast the official statistic below.\n"
        f"Question: {civic_question_text(challenge)}\n"
        f"Target: {contract.get('target_key') or ''}\n"
        f"Region: {contract.get('region') or ''}\n"
        f"Forecast deadline (UTC): {deadline}\n"
    )
    return system_prompt, user_prompt


def build_civic_submission_body(
    submission_route: str, forecast: dict[str, Any], stake_amount: float, rationale: str
) -> dict[str, Any]:
    if submission_route == "macro_numeric_legacy":
        body: dict[str, Any] = {
            "predicted_value": forecast["mean"],
            "predicted_std": forecast["std"],
            "amount": stake_amount,
        }
    else:
        body = dict(forecast)
        body["amount"] = stake_amount
    if rationale:
        body["rationale"] = rationale
    return body


def run_civic_forecast_cycle_live(
    agent: "MarketCommentAgent", config: dict[str, Any], dry_run: bool = False
) -> dict[str, Any]:
    """Agentic numeric/probability forecasts for Civic Index (official statistics) challenges.

    Every submission atomically stakes `civic_forecast.stake_amount` credits (the platform binds
    stake to forecast; there is no unstaked submission). Skips submission -- without marking the
    challenge, so it retries next cycle -- when the wallet balance cannot cover the stake.
    """
    civic_config = config.get("civic_forecast") or {}
    if not civic_config.get("enabled", False):
        return {"persona_id": agent.persona_id, "status": "skipped", "reason": "civic_forecast_not_enabled"}

    stake_amount = float(civic_config.get("stake_amount", 10))
    entries = select_open_civic_entries(agent, config)
    if not entries:
        return {"persona_id": agent.persona_id, "status": "idle", "reason": "no eligible civic forecast challenges"}

    # Check the wallet before burning any LLM calls: every submission stakes credits, so with an
    # unfunded wallet the whole cycle would generate forecasts it can never submit.
    balance = agent.get_credit_balance() if not dry_run else None
    if not dry_run and (balance is None or balance < stake_amount):
        return {
            "persona_id": agent.persona_id,
            "status": "insufficient_balance",
            "balance": balance,
            "stake_amount": stake_amount,
            "eligible_challenges": [str((e["challenge"] or {}).get("challenge_id")) for e in entries],
            "reason": "agent wallet cannot cover one stake; fund it via POST /agent/owner/topup",
        }

    chat_model = build_langchain_chat_model(config)
    knowledge_tools = build_knowledge_tools(agent.persona_id)
    skill_tools = build_skill_tools(
        _SKILL_REGISTRY,
        config.get("skills") or {},
        lambda fetch_name, purpose: getattr(agent, fetch_name)(purpose),
        determine_comment_language(config),
    )
    tools = knowledge_tools + skill_tools

    items: list[dict[str, Any]] = []
    for entry in entries:
        contract = entry["contract"]
        challenge = entry["challenge"]
        challenge_id = str(challenge.get("challenge_id"))
        outcome_shape = str(contract.get("outcome_shape") or "")
        item: dict[str, Any] = {
            "challenge_id": challenge_id,
            "target_key": contract.get("target_key"),
            "outcome_shape": outcome_shape,
        }
        items.append(item)

        schema_cls = NumericForecastOutput if outcome_shape == "numeric_distribution" else BinaryForecastOutput
        system_prompt, user_prompt = build_civic_forecast_prompt(contract, challenge, config)
        callbacks, langfuse_metadata = build_langfuse_callbacks(config, f"{agent.persona_id}-civic", "civic_forecast")
        try:
            output = run_agentic_generation(
                chat_model, tools, system_prompt, user_prompt, schema_cls,
                callbacks=callbacks, metadata=langfuse_metadata,
                require_tool_groups=[
                    frozenset(t.name for t in skill_tools),
                    frozenset(t.name for t in knowledge_tools),
                ],
            )
        except Exception as exc:
            item["error"] = f"generation failed: {exc}"
            continue

        rationale = str(output.get("rationale") or "")
        forecast, errors = normalize_civic_forecast(outcome_shape, contract.get("forecast_schema") or {}, output)
        item["forecast"] = forecast
        item["rationale"] = rationale
        if errors:
            item["error"] = "; ".join(errors)
            continue
        if dry_run:
            item["dry_run"] = True
            continue
        if balance is None or balance < stake_amount:
            item["error"] = f"insufficient credit balance ({balance}) for stake {stake_amount}; will retry next cycle"
            continue

        submission_route = str(contract.get("submission_route") or "")
        body = build_civic_submission_body(submission_route, forecast, stake_amount, rationale)
        try:
            response = agent.submit_civic_forecast(challenge_id, submission_route, body)
        except Exception as exc:
            item["error"] = str(exc)
            continue
        balance -= stake_amount
        agent.mark_civic_forecasted(challenge_id)
        storage_prediction_history.save_prediction(
            agent.persona_id,
            challenge_id,
            str(contract.get("target_key") or "").lower(),
            f"civic_{outcome_shape}",
            {**forecast, "reasoning": rationale},
            None,
            forecast.get("yes_probability"),
        )
        item["result"] = response

    return {
        "persona_id": agent.persona_id,
        "status": "ok",
        "stake_amount": stake_amount,
        "remaining_balance": balance,
        "items": items,
    }


def build_agentic_comment_prompt(event: dict[str, Any], config: dict[str, Any]) -> tuple[str, str]:
    persona = config.get("persona") or {}
    language = determine_comment_language(config)
    system_prompt = (
        f"You are a {role_label(config, language)}."
        f" Your tone should be {persona.get('tone', 'professional')}, and your writing style should be"
        f" {persona.get('style', 'clear and restrained')}."
        " You have tools available to look up your own past predictions, distilled lessons, recent market"
        " data, and your recent backtest performance, as well as tools to fetch current market data from"
        " various sources. Use whichever tools are relevant to this specific event before deciding what to"
        " say -- you do not need to call every tool. Before finalizing, use at least one live market-data"
        " tool (not just historical retrieval) to confirm your reasoning reflects current, not stale,"
        " conditions. Once you have enough information, respond with your"
        " final comment. Keep it to 2-4 sentences, avoid boilerplate, sound like a real market participant,"
        " and do not open with self-introductions such as 'As a trader' or 'As a macro investor'."
    )
    title = event.get("title") if language == "en" else (event.get("title_zh") or event.get("title"))
    user_prompt = (
        "Write a market comment based on the event below.\n"
        f"Title: {title or ''}\n"
        f"Event type: {event.get('event_type') or ''}\n"
        f"Severity: {event.get('severity') or ''}\n"
        f"Description: {event.get('description') or ''}\n"
    )
    return system_prompt, user_prompt


def select_eligible_event_for_agentic_pilot(
    agent: "MarketCommentAgent", config: dict[str, Any], candidate_events: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if not candidate_events:
        return None
    skip_commented_events = (config.get("comment_strategy") or {}).get("skip_commented_events", True)
    return next(
        (event for event in candidate_events if not skip_commented_events or not agent.has_commented(event["id"])),
        candidate_events[0],
    )


def run_agentic_comment_cycle(agent: "MarketCommentAgent", config: dict[str, Any]) -> dict[str, Any]:
    candidate_events = load_candidate_events(agent, config)
    event = select_eligible_event_for_agentic_pilot(agent, config, candidate_events)
    if event is None:
        return {"status": "idle", "reason": "No candidate events were found"}

    chat_model = build_langchain_chat_model(config)
    knowledge_tools = build_knowledge_tools(agent.persona_id)
    skill_tools = build_skill_tools(
        _SKILL_REGISTRY,
        config.get("skills") or {},
        lambda fetch_name, purpose: getattr(agent, fetch_name)(purpose),
        determine_comment_language(config),
    )
    tools = knowledge_tools + skill_tools

    system_prompt, user_prompt = build_agentic_comment_prompt(event, config)
    news_id = str(event.get("id") or "")

    def record_turn(message: Any) -> None:
        usage_metadata = getattr(message, "usage_metadata", None) or {}
        backend = str(config.get("generation", {}).get("backend") or "")
        agent.record_llm_usage(
            action="comment_agentic_turn",
            status="success",
            context={"news_id": news_id},
            result={
                "backend": backend,
                "model_provider": None,
                "model_name": None,
                "deployment": None,
                "usage_raw": dict(usage_metadata) if usage_metadata else None,
                "usage_normalized": {
                    "prompt_tokens": usage_metadata.get("input_tokens"),
                    "completion_tokens": usage_metadata.get("output_tokens"),
                    "total_tokens": usage_metadata.get("total_tokens"),
                }
                if usage_metadata
                else None,
            },
        )

    # "-pilot" suffix -- see the matching comment in run_agentic_prediction_cycle.
    callbacks, langfuse_metadata = build_langfuse_callbacks(config, f"{agent.persona_id}-pilot", "comment")
    comment = run_agentic_generation(
        chat_model, tools, system_prompt, user_prompt, CommentOutput, on_turn=record_turn,
        callbacks=callbacks, metadata=langfuse_metadata,
        require_tool_groups=[frozenset(t.name for t in skill_tools)],
    )
    return {"status": "ok", "event": event, "comment": comment}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agent client for Headline Arena (headlinearena.com)")
    parser.add_argument("--base-url", default=os.environ.get("MARKET_SITE_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--credential-path", default=str(DEFAULT_CREDENTIAL_PATH))
    parser.add_argument("--state-path", default=str(DEFAULT_STATE_PATH))
    parser.add_argument("--config-path", default=str(DEFAULT_CONFIG_PATH))

    subparsers = parser.add_subparsers(dest="command", required=True)

    register = subparsers.add_parser("register", help="Register the agent and save credentials locally")
    register.add_argument("--name")
    register.add_argument("--bio")
    register.add_argument("--model-provider")
    register.add_argument("--model-name")
    register.add_argument("--model-version")

    register_cn = subparsers.add_parser("register-cn", help="Register the agent on the CN arena (two-step challenge flow)")
    register_cn.add_argument("--name")
    register_cn.add_argument("--bio")
    register_cn.add_argument("--model-provider")
    register_cn.add_argument("--model-name")
    register_cn.add_argument("--model-version")

    subparsers.add_parser("profile", help="Fetch the authenticated agent profile")
    subparsers.add_parser("events", help="List today's events")
    challenges = subparsers.add_parser("challenges", help="List prediction challenges")
    challenges.add_argument("--status", default="open")
    subparsers.add_parser("show-config", help="Print the active agent behavior config")
    update_scopes = subparsers.add_parser("update-scopes", help="Self-grant supported scopes for the active agent")
    update_scopes.add_argument("scopes", nargs="+")

    follow = subparsers.add_parser("follow", help="Follow another agent by agent_id")
    follow.add_argument("target_agent_id")

    unfollow = subparsers.add_parser("unfollow", help="Unfollow another agent by agent_id")
    unfollow.add_argument("target_agent_id")

    discover = subparsers.add_parser("discover-follows", help="Discover follow candidates from public comments")
    discover.add_argument("--news-id")
    discover.add_argument("--limit", type=int, default=5)

    comment = subparsers.add_parser("comment", help="Post a comment for a specific event")
    comment.add_argument("news_id")
    comment.add_argument("--space-id")
    comment.add_argument("--content")

    auto_comment = subparsers.add_parser("comment-latest", help="Auto-comment on the highest priority event today")
    auto_comment.add_argument("--dry-run", action="store_true")

    predict_open = subparsers.add_parser("predict-open", help="Analyze and optionally submit predictions for open challenges")
    predict_open.add_argument("--dry-run", action="store_true")

    knowledge_query = subparsers.add_parser(
        "knowledge-query", help="Query the prediction/comment knowledge base by semantic similarity"
    )
    knowledge_query.add_argument("--asset-key", default=None)
    knowledge_query.add_argument("--query-text", required=True)
    knowledge_query.add_argument("--limit", type=int, default=5)

    subparsers.add_parser(
        "backfill-outcomes", help="Poll resolved challenges and backfill prediction_history outcomes"
    )

    generate_reflections = subparsers.add_parser(
        "generate-reflections", help="Distill lessons from recently-settled predictions"
    )
    generate_reflections.add_argument("--lookback-hours", type=int, default=24)

    generate_backtest_report = subparsers.add_parser(
        "generate-backtest-report", help="Compute and store a trailing-window performance report"
    )
    generate_backtest_report.add_argument("--period-days", type=int, default=7)

    generate_strategy_proposals = subparsers.add_parser(
        "generate-strategy-proposals",
        help="Propose a structured, human-reviewed prediction_strategy config change (never auto-applied)",
    )
    generate_strategy_proposals.add_argument("--lookback-days", type=int, default=30)

    strategy_proposals_parser = subparsers.add_parser(
        "strategy-proposals", help="List stored strategy proposals for human review"
    )
    strategy_proposals_parser.add_argument("--status", default="pending_review")

    mark_strategy_proposal_reviewed = subparsers.add_parser(
        "mark-strategy-proposal-reviewed",
        help="Record a human review decision on a stored strategy proposal (never applies the change itself)",
    )
    mark_strategy_proposal_reviewed.add_argument("proposal_id")
    mark_strategy_proposal_reviewed.add_argument(
        "--status", required=True, choices=sorted(storage_strategy_proposals.ALLOWED_REVIEW_STATUSES)
    )

    subparsers.add_parser(
        "evolve-strategy-card",
        help="One tick of the strategy-card evolution loop: decide a pending candidate via shadow A/B, "
        "run the post-promotion canary check, or generate a new candidate card",
    )

    strategy_cards_parser = subparsers.add_parser(
        "strategy-cards", help="List this persona's strategy card version history"
    )
    strategy_cards_parser.add_argument("--limit", type=int, default=10)

    subparsers.add_parser(
        "predict-open-agentic",
        help="Pilot: analyze open challenges with an agentic tool-calling loop (observe-only, never submits)",
    )

    predict_open_agentic_live = subparsers.add_parser(
        "predict-open-agentic-live",
        help="Agentic tool-calling loop that submits real predictions, for a dedicated agent identity",
    )
    predict_open_agentic_live.add_argument("--dry-run", action="store_true")

    civic_forecast_live = subparsers.add_parser(
        "civic-forecast-agentic-live",
        help="Agentic numeric/probability forecasts for Civic Index (official statistics) challenges; "
        "each submission atomically stakes civic_forecast.stake_amount credits",
    )
    civic_forecast_live.add_argument("--dry-run", action="store_true")

    subparsers.add_parser(
        "comment-latest-agentic",
        help="Pilot: write a comment on the latest event with an agentic tool-calling loop (observe-only, never posts)",
    )

    run_cycle_parser = subparsers.add_parser("run-cycle", help="Comment on one new event and follow discovered agents")
    run_cycle_parser.add_argument("--follow-limit", type=int, default=3)
    run_cycle_parser.add_argument("--dry-run", action="store_true")

    scheduler = subparsers.add_parser("scheduler", help="Run automation in a polling loop")
    scheduler.add_argument("--interval-seconds", type=int, default=900)
    scheduler.add_argument("--follow-limit", type=int, default=3)
    scheduler.add_argument("--max-loops", type=int, default=0)
    scheduler.add_argument("--dry-run", action="store_true")

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    agent = MarketCommentAgent(args.base_url, Path(args.credential_path), Path(args.state_path), Path(args.config_path))

    try:
        if args.command == "register":
            response = agent.register(
                {
                    "name": args.name,
                    "bio": args.bio,
                    "model_provider": args.model_provider,
                    "model_name": args.model_name,
                    "model_version": args.model_version,
                }
            )
            print("Registration complete.")
            print_json(response)
            claim_url = response.get("claim_url")
            if claim_url:
                claim_url = claim_url.replace("http://", "https://", 1)
                print("\nNext step: open this claim_url in a browser to activate the agent:")
                print(claim_url)
            return 0

        if args.command == "register-cn":
            response = agent.register_cn(
                {
                    "name": args.name,
                    "bio": args.bio,
                    "model_provider": args.model_provider,
                    "model_name": args.model_name,
                    "model_version": args.model_version,
                }
            )
            print("CN registration complete and challenge passed.")
            print_json(response)
            return 0

        if args.command == "profile":
            print_json(agent.get_profile())
            return 0

        if args.command == "events":
            config = agent.reload_config()
            print_json(load_candidate_events(agent, config))
            return 0

        if args.command == "challenges":
            print_json(agent.list_prediction_challenges(args.status))
            return 0

        if args.command == "show-config":
            print_json(agent.reload_config())
            return 0

        if args.command == "knowledge-query":
            print_json(
                {
                    "similar_predictions": storage_retrieval.get_similar_predictions(
                        args.query_text, persona_id=agent.persona_id, asset_key=args.asset_key, k=args.limit
                    ),
                    "relevant_lessons": storage_retrieval.get_relevant_lessons(
                        args.query_text, persona_id=agent.persona_id, asset_key=args.asset_key, k=args.limit
                    ),
                    "recent_market_trend": (
                        storage_retrieval.get_recent_market_trend(args.asset_key) if args.asset_key else []
                    ),
                    "latest_backtest_report": storage_retrieval.get_latest_backtest_report(agent.persona_id),
                }
            )
            return 0

        if args.command == "backfill-outcomes":
            print_json(run_backfill_outcomes(agent))
            return 0

        if args.command == "generate-reflections":
            print_json(run_generate_reflections(agent, args.lookback_hours))
            return 0

        if args.command == "generate-backtest-report":
            print_json(run_generate_backtest_report(agent, args.period_days))
            return 0

        if args.command == "generate-strategy-proposals":
            config = agent.reload_config()
            print_json(run_generate_strategy_proposals(agent, config, args.lookback_days))
            return 0

        if args.command == "strategy-proposals":
            print_json(storage_strategy_proposals.list_proposals(agent.persona_id, args.status))
            return 0

        if args.command == "mark-strategy-proposal-reviewed":
            print_json(storage_strategy_proposals.mark_reviewed(agent.persona_id, args.proposal_id, args.status))
            return 0

        if args.command == "evolve-strategy-card":
            config = agent.reload_config()
            try:
                print_json(run_evolve_strategy_card(agent, config))
            except ValueError as exc:
                raise ApiError(str(exc)) from exc
            return 0

        if args.command == "strategy-cards":
            print_json(storage_strategy_cards.get_card_history(agent.persona_id, args.limit))
            return 0

        if args.command == "predict-open-agentic":
            config = agent.reload_config()
            try:
                print_json(run_agentic_prediction_cycle(agent, config))
            except ValueError as exc:
                raise ApiError(str(exc)) from exc
            return 0

        if args.command == "predict-open-agentic-live":
            config = agent.reload_config()
            try:
                print_json(run_agentic_prediction_cycle_live(agent, config, dry_run=args.dry_run))
            except ValueError as exc:
                raise ApiError(str(exc)) from exc
            return 0

        if args.command == "civic-forecast-agentic-live":
            config = agent.reload_config()
            try:
                print_json(run_civic_forecast_cycle_live(agent, config, dry_run=args.dry_run))
            except ValueError as exc:
                raise ApiError(str(exc)) from exc
            return 0

        if args.command == "comment-latest-agentic":
            config = agent.reload_config()
            try:
                print_json(run_agentic_comment_cycle(agent, config))
            except ValueError as exc:
                raise ApiError(str(exc)) from exc
            return 0

        if args.command == "update-scopes":
            print_json(agent.update_scopes(args.scopes))
            return 0

        if args.command == "follow":
            response = agent.follow_agent(args.target_agent_id)
            agent.mark_followed(args.target_agent_id)
            print_json(response)
            return 0

        if args.command == "unfollow":
            response = agent.unfollow_agent(args.target_agent_id)
            print_json(response)
            return 0

        if args.command == "discover-follows":
            config = agent.reload_config()
            events = load_candidate_events(agent, config)
            if not events:
                print_json([])
                return 0
            news_id = args.news_id or events[0]["id"]
            candidates = extract_follow_candidates(
                agent.get_public_comments(news_id),
                agent.credentials.get("agent_id", ""),
            )
            print_json(candidates[: args.limit])
            return 0

        if args.command == "comment":
            content = args.content
            if not content:
                config = agent.reload_config()
                events = load_candidate_events(agent, config)
                event = next((item for item in events if item.get("id") == args.news_id), None)
                if not event:
                    raise ApiError(f"Event {args.news_id} not found in today's feed.")
                context_events = select_comment_context_events(event, events, config)
                related_events = [item for item in context_events if item.get("id") != event.get("id")]
                skill_snapshot = agent.get_skill_snapshots("comment")
                content = generate_comment_content(
                    agent,
                    event,
                    config,
                    determine_comment_language(config),
                    skill_snapshot,
                    related_events,
                )
            response = agent.post_comment(args.news_id, content, args.space_id)
            print_json(response)
            return 0

        if args.command == "comment-latest":
            config = agent.reload_config()
            events = load_candidate_events(agent, config)
            if not events:
                print_json({"status": "idle", "reason": "No events returned by /api/v1/events or /api/v1/events/today"})
                return 0
            event = events[0]
            context_events = select_comment_context_events(event, events, config)
            related_events = [item for item in context_events if item.get("id") != event.get("id")]
            skill_snapshot = agent.get_skill_snapshots("comment")
            comment_language = determine_comment_language(config)
            content = generate_comment_content(
                agent,
                event,
                config,
                comment_language,
                skill_snapshot,
                related_events,
            )
            payload = {
                "news_id": event["id"],
                "space_id": pick_space(event),
                "content": content,
                "title": event.get("title") if comment_language == "en" else (event.get("title_zh") or event.get("title")),
                "context_events": [
                    {
                        "id": item.get("id"),
                        "title": item.get("title"),
                        "severity": item.get("severity"),
                        "event_type": item.get("event_type"),
                    }
                    for item in context_events
                ],
            }
            if args.dry_run:
                print_json(payload)
                return 0
            cn_comment_mode = str((config.get("prediction_strategy") or {}).get("challenge_mode") or "").lower() == "cn"
            if cn_comment_mode:
                response = agent.post_cn_comment(event["id"], content)
            else:
                response = agent.post_comment(event["id"], content, pick_space(event))
            print_json(response)
            return 0

        if args.command == "predict-open":
            config = agent.reload_config()
            events = load_candidate_events(agent, config)
            prediction_candidates, prediction_errors, prediction_skill_snapshot = choose_prediction_candidates(
                agent,
                config,
                events,
            )
            if not prediction_candidates:
                print_json(
                    {
                        "status": "idle",
                        "reason": "No eligible prediction challenges were found",
                        "prediction_errors": prediction_errors,
                        "skill_context": {"prediction": prediction_skill_snapshot} if prediction_skill_snapshot else {},
                    }
                )
                return 0
            payload = {
                "prediction_candidates": [
                    {
                        "challenge": item.get("challenge"),
                        "event": item.get("event"),
                        "prediction": item.get("prediction"),
                        "request_body": item.get("request_body"),
                    }
                    for item in prediction_candidates
                ],
                "prediction_errors": prediction_errors,
                "skill_context": {"prediction": prediction_skill_snapshot} if prediction_skill_snapshot else {},
            }
            if args.dry_run:
                print_json(payload)
                return 0
            pred_strategy = config.get("prediction_strategy") or {}
            cn_mode = str(pred_strategy.get("challenge_mode") or "").lower() == "cn"
            if not cn_mode:
                payload["scope_update"] = agent.ensure_scopes(["prediction:submit"])
            predict_path_template: str | None = pred_strategy.get("predict_path_template") or None
            actions = []
            for item in prediction_candidates:
                challenge = item.get("challenge") or {}
                prediction = item.get("prediction") or {}
                if cn_mode:
                    request_body = item.get("request_body") or build_cn_prediction_request(prediction)
                else:
                    request_body = item.get("request_body") or build_prediction_request(prediction)
                challenge_id = str(challenge.get("id") or "").strip()
                if not challenge_id:
                    continue
                response = agent.submit_prediction(
                    challenge_id,
                    request_body,
                    predict_path_template=predict_path_template,
                )
                agent.mark_predicted(
                    challenge_id,
                    direction=str(prediction.get("direction") or ""),
                    confidence=float(prediction.get("confidence") or 0.0),
                )
                record_prediction_history(agent, challenge, challenge_id, prediction)
                actions.append({"type": "prediction", "challenge_id": challenge_id, "result": response})
            payload["actions"] = actions
            print_json(payload)
            return 0

        if args.command == "run-cycle":
            print_json(run_cycle(agent, args.dry_run, args.follow_limit))
            return 0

        if args.command == "scheduler":
            import time

            # Request all configured scopes once at startup so agents registered
            # via non-standard endpoints (e.g. /api/cn/register) get the same
            # permissions as those registered via the standard flow.
            startup_scopes = agent.config.get("requested_scopes") or []
            if startup_scopes:
                try:
                    agent.ensure_scopes(startup_scopes)
                except ApiError as exc:
                    print_json({"status": "warning", "reason": f"Scope request at startup failed: {exc}",
                                "timestamp": datetime.now(timezone.utc).isoformat()})

            loop_count = 0
            while True:
                try:
                    print_json(run_cycle(agent, args.dry_run, args.follow_limit))
                except ApiError as exc:
                    print_json(
                        {
                            "status": "error",
                            "reason": str(exc),
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                loop_count += 1
                if args.max_loops and loop_count >= args.max_loops:
                    return 0
                time.sleep(args.interval_seconds)

        raise ApiError(f"Unsupported command: {args.command}")
    except ApiError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
