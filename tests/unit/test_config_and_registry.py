"""Settings parsing and the exact-id capability registry."""

from __future__ import annotations

import pytest

from design_agent.config import ModelSpec, Settings
from design_agent.models.cache import CachedChatAnthropic
from design_agent.models.factory import GATEWAY_NATIVE_ROUTES, build_chat_model, resolve_model
from design_agent.models.registry import (
    CAPABILITY_REGISTRY,
    GATEWAY_NATIVE_PROVIDERS,
    HIGH_EFFORT_MIN_OUTPUT_TOKENS,
    UNKNOWN_MODEL,
    ModelConfigurationError,
    ToolDisclosure,
    capabilities_for,
)


def settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_model_spec_parses_provider_and_gateway() -> None:
    spec = ModelSpec.parse("anthropic:claude-opus-5-5")
    assert (spec.provider, spec.model, spec.via_gateway) == ("anthropic", "claude-opus-5-5", False)
    gateway = ModelSpec.parse("langsmith:anthropic/claude-opus-5-5")
    assert gateway.via_gateway and str(gateway.upstream) == "anthropic:claude-opus-5-5"
    with pytest.raises(ValueError, match="provider:model"):
        ModelSpec.parse("claude-opus-5-5")


def test_lookup_is_exact_never_substring() -> None:
    assert capabilities_for(ModelSpec.parse("anthropic:claude-opus-5-5")).verified
    assert capabilities_for(ModelSpec.parse("anthropic:claude-opus-5-5-preview")) is UNKNOWN_MODEL
    assert capabilities_for(ModelSpec.parse("anthropic:claude-opus")) is UNKNOWN_MODEL


def test_the_gateway_keeps_native_features_only_on_a_providers_own_route() -> None:
    native = capabilities_for(ModelSpec.parse("langsmith:anthropic/claude-opus-5-5"))
    assert native == CAPABILITY_REGISTRY["anthropic:claude-opus-5-5"]
    shared = capabilities_for(ModelSpec.parse("langsmith:google_genai/gemini-3-pro"))
    assert shared.disclosure is ToolDisclosure.PORTABLE_SELECTOR
    assert set(GATEWAY_NATIVE_ROUTES) == GATEWAY_NATIVE_PROVIDERS


def test_a_gateway_anthropic_model_is_the_native_class_on_the_gateway_route() -> None:
    s = settings(
        model="langsmith:anthropic/claude-opus-5-5",
        langsmith_gateway_api_key="lsv2_gateway",
        anthropic_api_key="sk-ant-never-sent",
    )
    model = build_chat_model(s, "lead")
    assert isinstance(model, CachedChatAnthropic)
    assert model.model == "claude-opus-5-5"
    assert model.anthropic_api_url == "https://gateway.smith.langchain.com/anthropic"
    assert model.anthropic_api_key.get_secret_value() == "lsv2_gateway"
    assert (model.effort, model.thinking, model.cache_ttl) == ("high", {"type": "adaptive"}, "1h")


def test_the_gateway_falls_back_to_the_langsmith_key_and_never_to_a_provider_key() -> None:
    s = settings(model="langsmith:anthropic/claude-opus-5-5", langsmith_api_key="lsv2_workspace")
    assert build_chat_model(s, "lead").anthropic_api_key.get_secret_value() == "lsv2_workspace"  # type: ignore[attr-defined]
    keyless = settings(model="langsmith:anthropic/claude-opus-5-5", anthropic_api_key="sk-ant-x")
    assert build_chat_model(keyless, "lead").anthropic_api_key.get_secret_value() == ""  # type: ignore[attr-defined]


def test_a_gateway_openai_model_uses_the_openai_route() -> None:
    s = settings(model="langsmith:openai/gpt-6-sol", langsmith_gateway_api_key="lsv2_gateway")
    model = build_chat_model(s, "lead")
    assert model.openai_api_base == "https://gateway.smith.langchain.com/openai/v1"  # type: ignore[attr-defined]


def test_custom_base_url_never_claims_native_search() -> None:
    proxied = capabilities_for(
        ModelSpec.parse("openai:gpt-6-sol"), base_url="https://proxy.example"
    )
    assert proxied.disclosure is ToolDisclosure.PORTABLE_SELECTOR
    assert (
        capabilities_for(ModelSpec.parse("openai:gpt-6-sol")).disclosure
        is ToolDisclosure.PROVIDER_NATIVE
    )


def test_lead_defaults_to_high_effort_and_look_to_low() -> None:
    s = settings()
    assert resolve_model(s, "lead").effort == "high"
    assert resolve_model(s, "look").effort == "low"


def test_unsupported_effort_is_refused_with_choices() -> None:
    with pytest.raises(ModelConfigurationError, match="choose one of"):
        resolve_model(settings(model_effort="ultra"), "lead")


def test_high_effort_gets_the_output_floor() -> None:
    resolved = resolve_model(settings(model_effort="max"), "lead")
    assert resolved.max_output_tokens is not None
    assert resolved.max_output_tokens >= HIGH_EFFORT_MIN_OUTPUT_TOKENS


def test_models_without_vision_are_refused_for_looking_roles() -> None:
    with pytest.raises(ModelConfigurationError, match="image input"):
        resolve_model(settings(model="mystery:model-1"), "lead")
    with pytest.raises(ModelConfigurationError, match="look"):
        resolve_model(settings(look_model="mystery:model-1"), "look")


def test_anthropic_model_is_built_with_registry_behavior() -> None:
    model = build_chat_model(settings(anthropic_api_key="sk-test"), "lead")
    assert model.max_retries == 0  # type: ignore[attr-defined]
    assert model.max_tokens == 128_000  # type: ignore[attr-defined]
    assert model.effort == "high"  # type: ignore[attr-defined]
    assert model.thinking == {"type": "adaptive"}  # type: ignore[attr-defined]


def test_base_url_requires_a_named_key(monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings(model="openai:gpt-6-sol", model_base_url="https://llm.example/v1")
    with pytest.raises(ModelConfigurationError, match="MODEL_API_KEY_ENV"):
        build_chat_model(s, "lead")
    monkeypatch.setenv("EXAMPLE_KEY", "secret")
    s = settings(
        model="openai:gpt-6-sol",
        model_base_url="https://llm.example/v1",
        model_api_key_env="EXAMPLE_KEY",
    )
    assert build_chat_model(s, "lead").openai_api_base == "https://llm.example/v1"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "model", ["deepseek-flash", "qwen3-vl-plus", "qwen3-vl-flash", "anthropic/claude-haiku-5.5"]
)
def test_compatible_vision_models_use_the_endpoint_for_every_inherited_role(
    model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COMPATIBLE_API_KEY", "endpoint-secret")
    s = settings(
        model=f"openai:{model}",
        model_base_url="https://provider.example/v1",
        model_api_key_env="COMPATIBLE_API_KEY",
    )
    for role in ("lead", "helper", "look", "tool_selector", "learning"):
        resolved = resolve_model(s, role)  # type: ignore[arg-type]
        assert resolved.caps.vision and resolved.caps.tool_calling
        assert resolved.caps.disclosure is ToolDisclosure.PORTABLE_SELECTOR
        built = build_chat_model(s, role)  # type: ignore[arg-type]
        assert built.openai_api_base == s.model_base_url  # type: ignore[attr-defined]
        assert built.openai_api_key.get_secret_value() == "endpoint-secret"  # type: ignore[attr-defined]
        assert built.use_responses_api is False  # type: ignore[attr-defined]
        if model == "deepseek-flash":
            assert built.extra_body == {"thinking": {"type": "enabled"}}  # type: ignore[attr-defined]


def test_custom_endpoint_never_falls_back_to_the_openai_key(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COMPATIBLE_API_KEY", raising=False)
    s = settings(
        model="openai:deepseek-flash",
        model_base_url="https://provider.example/v1",
        model_api_key_env="COMPATIBLE_API_KEY",
        openai_api_key="must-not-leak",
    )
    with pytest.raises(ModelConfigurationError, match="MODEL_API_KEY_ENV"):
        build_chat_model(s)


def test_custom_endpoint_doctor_checks_its_key_in_the_project_env(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    from design_agent.doctor import missing_model_key

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COMPATIBLE_API_KEY", raising=False)
    s = settings(
        model="openai:deepseek-flash",
        model_base_url="https://provider.example/v1",
        model_api_key_env="COMPATIBLE_API_KEY",
    )
    assert missing_model_key(s) == "COMPATIBLE_API_KEY"
    (tmp_path / ".env").write_text("COMPATIBLE_API_KEY=private-value\n")
    assert missing_model_key(s) is None


@pytest.mark.parametrize(
    "model_id",
    sorted(
        k for k, caps in CAPABILITY_REGISTRY.items() if caps.provider in {"anthropic", "openai"}
    ),
)
def test_registry_agrees_with_installed_provider_profiles(model_id: str) -> None:
    provider, model = model_id.split(":", 1)
    if provider == "anthropic":
        from langchain_anthropic.data._profiles import _PROFILES
    else:
        from langchain_openai.data._profiles import _PROFILES
    profile = _PROFILES.get(model)
    assert profile is not None, f"{model_id} is not a model the installed package knows"
    caps = CAPABILITY_REGISTRY[model_id]
    assert caps.vision == bool(profile.get("image_inputs"))
    assert caps.max_output_tokens == profile.get("max_output_tokens")
    assert caps.context_window == profile.get("max_input_tokens")
    if profile.get("reasoning_effort_levels"):
        assert caps.efforts == frozenset(profile["reasoning_effort_levels"])


def test_secrets_never_appear_in_repr() -> None:
    s = settings(anthropic_api_key="sk-very-secret")
    assert "sk-very-secret" not in repr(s)
    assert "sk-very-secret" not in str(s.model_dump())


def test_brand_path_falls_back_to_the_sample_brand(tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "examples/brands/example-studio").mkdir(parents=True)
    assert settings().resolved_brand_path(tmp_path) == tmp_path / "examples/brands/example-studio"
    (tmp_path / "workspace/brand").mkdir(parents=True)
    (tmp_path / "workspace/brand/brand.toml").write_text("name = 'x'\n")
    assert settings().resolved_brand_path(tmp_path) == tmp_path / "workspace/brand"


@pytest.mark.parametrize("name", ["Design Agent", "Design Agent Test", "design-agent_2.0", "A"])
def test_a_slack_name_mda_accepts_is_accepted(name: str) -> None:
    assert settings(slack_name=name).slack_name == name


@pytest.mark.parametrize(
    "name", ["Design Agent (test)", " Design Agent", "Design Agent-", "Design!"]
)
def test_a_slack_name_mda_would_reject_fails_before_the_deploy(name: str) -> None:
    with pytest.raises(ValueError, match="slack_name"):
        settings(slack_name=name)


@pytest.mark.parametrize(
    "model", ["deepseek-flash", "qwen3-vl-plus", "qwen3-vl-flash", "anthropic/claude-haiku-5.5"]
)
def test_compatible_models_require_an_explicit_endpoint(model: str) -> None:
    with pytest.raises(ModelConfigurationError, match="MODEL_BASE_URL"):
        resolve_model(settings(model=f"openai:{model}"))


def test_openrouter_uses_chat_completions_without_native_thinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langchain_core.messages import HumanMessage

    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-router-key")
    s = settings(
        model="openai:anthropic/claude-haiku-5.5",
        model_base_url="https://openrouter.ai/api/v1",
        model_api_key_env="OPENROUTER_API_KEY",
    )
    model = build_chat_model(s)
    payload = model._get_request_payload([HumanMessage("hello")])
    assert payload["model"] == "anthropic/claude-haiku-5.5"
    assert payload["extra_body"] == {"reasoning": {"enabled": False}}
    assert "reasoning" not in payload and "reasoning_effort" not in payload
    assert payload["max_completion_tokens"] == 128_000
    assert resolve_model(s).caps.disclosure is ToolDisclosure.PORTABLE_SELECTOR


def test_openrouter_haiku_price_is_available_to_the_meter() -> None:
    from design_agent.harness.meter import price_of

    price = price_of("openai:anthropic/claude-haiku-5.5")
    assert price is not None
    assert price.input == 0.10 and price.output == 0.50
    assert price.long_context is not None


async def test_openrouter_client_sends_reasoning_setting_and_replays_tool_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    import httpx
    from langchain_core.messages import HumanMessage, ToolMessage
    from langchain_core.tools import tool

    monkeypatch.setenv("ROUTER_TEST_KEY", "offline-only")
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer offline-only"
        assert payload["model"] == "anthropic/claude-haiku-5.5"
        assert payload["reasoning"] == {"enabled": False}
        if len(seen) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "add", "arguments": '{"a":17,"b":25}'},
                    }
                ],
            }
        else:
            assert payload["messages"][-1]["role"] == "tool"
            assert payload["messages"][-1]["content"] == "42"
            message = {"role": "assistant", "content": "42"}
        return httpx.Response(
            200,
            json={
                "id": "offline",
                "model": "anthropic/claude-haiku-5.5",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if len(seen) == 1 else "stop",
                    }
                ],
            },
        )

    @tool
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        model = build_chat_model(
            settings(
                model="openai:anthropic/claude-haiku-5.5",
                model_base_url="https://openrouter.ai/api/v1",
                model_api_key_env="ROUTER_TEST_KEY",
            ),
            http_async_client=client,
            max_completion_tokens=128,
        ).bind_tools([add])
        request = HumanMessage("Add 17 and 25")
        reply = await model.ainvoke([request])
        call = reply.tool_calls[0]
        result = await model.ainvoke(
            [
                request,
                reply,
                ToolMessage(str(add.invoke(call["args"])), tool_call_id=call["id"]),
            ]
        )
    assert result.content == "42" and len(seen) == 2
