import os
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import AzureChatOpenAI, ChatOpenAI

_SUPPORTED_BACKENDS = {
    "openai_compatible",
    "azure_openai_deployment",
    "azure_ai_inference",
    "azure_foundry_anthropic",
}


def _resolve_value(generation: dict[str, Any], key: str, default: Any = None) -> Any:
    env_key = str(generation.get(f"{key}_env") or "").strip()
    if env_key:
        env_value = os.environ.get(env_key)
        if env_value not in {None, ""}:
            return env_value
    value = generation.get(key, default)
    return default if value is None else value


def build_langchain_chat_model(config: dict[str, Any]) -> BaseChatModel:
    generation = config.get("generation") or {}
    backend = str(generation.get("backend") or "").strip().lower()
    if backend not in _SUPPORTED_BACKENDS:
        raise ValueError(f"Unsupported backend for the agentic pilot: {backend or '(empty)'}")

    api_key_env = str(generation.get("api_key_env") or "")
    api_key = os.environ.get(api_key_env) if api_key_env else None
    if not api_key:
        raise ValueError(f"Missing API key in environment variable {api_key_env or '(unset api_key_env)'}")

    temperature = generation.get("temperature", 0.4)
    max_tokens = generation.get("max_tokens", 400)
    request_timeout_seconds = int(generation.get("request_timeout_seconds", 60))

    if backend == "openai_compatible":
        base_url = str(_resolve_value(generation, "api_base_url", "https://api.openai.com/v1")).rstrip("/")
        model_name = str(_resolve_value(generation, "model", "gpt-5.4"))
        return ChatOpenAI(
            base_url=base_url,
            api_key=api_key,
            model=model_name,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=request_timeout_seconds,
        )

    if backend == "azure_openai_deployment":
        base_url = str(_resolve_value(generation, "api_base_url", "")).rstrip("/")
        if not base_url:
            raise ValueError("Missing Azure OpenAI base URL")
        deployment = str(
            _resolve_value(
                generation,
                "deployment",
                _resolve_value(generation, "model", config.get("model", {}).get("name", "gpt-5.4")),
            )
        ).strip()
        if not deployment:
            raise ValueError("Missing Azure OpenAI deployment name")
        api_version = str(generation.get("api_version") or "2024-10-21")
        return AzureChatOpenAI(
            azure_endpoint=base_url,
            api_key=api_key,
            azure_deployment=deployment,
            api_version=api_version,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=request_timeout_seconds,
        )

    if backend == "azure_ai_inference":
        base_url = str(_resolve_value(generation, "api_base_url", "")).rstrip("/")
        if not base_url:
            raise ValueError("Missing Azure AI Inference base URL")
        model_name = str(_resolve_value(generation, "model", "")).strip()
        if not model_name:
            raise ValueError("Missing Azure AI Inference model name")
        api_version = str(generation.get("api_version") or "2024-05-01-preview")
        return ChatOpenAI(
            base_url=f"{base_url}/models",
            api_key=api_key,
            model=model_name,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=request_timeout_seconds,
            default_query={"api-version": api_version},
        )

    base_url = str(_resolve_value(generation, "api_base_url", "")).rstrip("/")
    if not base_url:
        raise ValueError("Missing Azure Foundry Anthropic base URL")
    model_name = str(_resolve_value(generation, "model", "")).strip()
    if not model_name:
        raise ValueError("Missing Azure Foundry Anthropic model name")
    return ChatAnthropic(
        base_url=base_url,
        api_key=api_key,
        model=model_name,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=request_timeout_seconds,
    )
