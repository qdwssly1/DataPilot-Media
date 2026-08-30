from __future__ import annotations

import json
from typing import Any
from urllib.request import Request

import pytest

from datapilot.agent.planner import PlannerConfigurationError
from datapilot.llm import openai_compatible
from datapilot.llm.openai_compatible import OpenAICompatiblePlannerModel


class FakeHTTPResponse:
    def __enter__(self) -> FakeHTTPResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"intent":"simple_question"}',
                        }
                    }
                ]
            }
        ).encode()


def test_openai_compatible_client_requires_all_environment_values() -> None:
    with pytest.raises(PlannerConfigurationError, match="LLM_BASE_URL"):
        OpenAICompatiblePlannerModel.from_env(
            {
                "LLM_API_KEY": "test-key",
                "LLM_MODEL": "test-model",
            }
        )


def test_openai_compatible_client_builds_json_mode_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(
        request: Request,
        *,
        timeout: float,
    ) -> FakeHTTPResponse:
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeHTTPResponse()

    monkeypatch.setattr(openai_compatible, "urlopen", fake_urlopen)
    client = OpenAICompatiblePlannerModel(
        api_key="test-secret",
        base_url="https://example.test/v1",
        model="test-model",
        timeout=4.5,
    )

    content = client.complete(
        system_prompt="Plan only.",
        user_prompt="User query.",
        response_schema={"type": "object"},
    )

    request = captured["request"]
    payload = json.loads(request.data.decode())
    assert content == '{"intent":"simple_question"}'
    assert request.full_url == "https://example.test/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer test-secret"
    assert captured["timeout"] == 4.5
    assert payload["model"] == "test-model"
    assert payload["response_format"] == {"type": "json_object"}
    assert "JSON schema:" in payload["messages"][1]["content"]
