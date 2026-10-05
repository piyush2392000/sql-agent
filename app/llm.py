"""LLM access. Provider-agnostic: any OpenAI-compatible endpoint (OpenAI, Groq, OpenRouter, Gemini's
OpenAI-compatible endpoint, Ollama...) or Azure OpenAI. Configure via environment variables:

  LLM_API_KEY    (or OPENAI_API_KEY)
  LLM_BASE_URL   optional, e.g. https://api.groq.com/openai/v1
  LLM_MODEL      default gpt-4o-mini

  Azure OpenAI instead: AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_DEPLOYMENT,
  AZURE_OPENAI_API_VERSION
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache


class LLMConfigError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def get_llm():
    if os.environ.get("AZURE_OPENAI_ENDPOINT"):
        from langchain_openai import AzureChatOpenAI

        return AzureChatOpenAI(
            azure_deployment=os.environ["AZURE_OPENAI_DEPLOYMENT"],
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            temperature=0, timeout=60, max_retries=2,
        )
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise LLMConfigError("No LLM API key configured. Set LLM_API_KEY (see .env.example).")
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        api_key=api_key,
        base_url=os.environ.get("LLM_BASE_URL") or None,
        temperature=0, timeout=60, max_retries=2,
    )


def _text(resp) -> str:
    content = getattr(resp, "content", resp)
    if isinstance(content, list):  # some providers return content blocks
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content)


def call_text(system: str, human: str) -> str:
    return _text(get_llm().invoke([("system", system), ("human", human)])).strip()


def _parse_json(text: str) -> dict | None:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def call_json(system: str, human: str) -> dict:
    """Call the model and parse a JSON object; retries once with a stricter reminder."""
    text = call_text(system, human)
    data = _parse_json(text)
    if data is None:
        text = call_text(system, human + "\n\nReturn ONLY one valid JSON object, nothing else.")
        data = _parse_json(text)
    return data or {}
