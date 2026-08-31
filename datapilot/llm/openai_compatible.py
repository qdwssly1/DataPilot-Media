"""Minimal OpenAI-compatible Chat Completions client for DataPilot agents."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from datapilot.agent.planner import PlannerConfigurationError, PlannerError


@dataclass(frozen=True, slots=True)
class OpenAICompatiblePlannerModel:
    """Call an OpenAI-compatible ``/chat/completions`` JSON endpoint."""

    api_key: str
    base_url: str
    model: str
    timeout: float = 30.0

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise PlannerConfigurationError("LLM_API_KEY is required")
        if not self.model.strip():
            raise PlannerConfigurationError("LLM_MODEL is required")
        parsed_url = urlparse(self.base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise PlannerConfigurationError("LLM_BASE_URL must be an HTTP(S) URL")

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> OpenAICompatiblePlannerModel:
        """Build a client from the three documented LLM environment variables."""

        values = os.environ if environ is None else environ
        required = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")
        missing = [name for name in required if not values.get(name, "").strip()]
        if missing:
            raise PlannerConfigurationError(
                f"missing LLM configuration: {', '.join(missing)}"
            )
        return cls(
            api_key=values["LLM_API_KEY"],
            base_url=values["LLM_BASE_URL"],
            model=values["LLM_MODEL"],
        )

    @property
    def endpoint(self) -> str:
        base_url = self.base_url.rstrip("/")
        if base_url.endswith("/chat/completions"):
            return base_url
        return f"{base_url}/chat/completions"

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
    ) -> str:
        """Return the assistant content from one JSON-mode chat completion."""

        schema_text = json.dumps(
            response_schema,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": f"{user_prompt}\n\nJSON schema:\n{schema_text}",
                },
            ],
        }
        request = Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=self.timeout) as response:
                response_data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise PlannerError(f"LLM API returned HTTP status {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            raise PlannerError("LLM API request failed") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlannerError("LLM API returned invalid JSON") from exc

        try:
            content = response_data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise PlannerError("LLM API response is missing message content") from exc
        if not isinstance(content, str) or not content.strip():
            raise PlannerError("LLM API returned empty message content")
        return content


OpenAICompatibleModel = OpenAICompatiblePlannerModel
