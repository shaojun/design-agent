"""Alibaba media contracts: streaming alignment, credential boundaries and explicit routing."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from design_agent.config import Settings
from design_agent.tools import aliyun
from design_agent.tools.files import WorkspaceError
from design_agent.tools.media import build_media_tools
from tests.unit.test_host_tools import MemoryBackend, _png

BASE = "https://example.cn-beijing.maas.aliyuncs.com"
KEY = "test-secret-not-for-sandbox"


def settings(**kwargs: Any) -> Settings:
    return Settings(
        _env_file=None,
        dashscope_api_key=KEY,
        image_provider="aliyun",
        image_model="qwen-image-3.0",
        tts_provider="aliyun",
        aliyun_openai_base_url=BASE + "/compatible-mode/v1",
        aliyun_dashscope_base_url=BASE + "/api/v1",
        **kwargs,
    )


def test_aliyun_effect_description_exposes_host_prompt_restrictions() -> None:
    tool = next(
        t
        for t in build_media_tools(settings(sfx_provider="aliyun"), lambda rt: MemoryBackend())
        if t.name == "generate_sound_effect"
    )
    assert "No speech, narration, singing or music" in tool.description
    assert "generate_music" in tool.description
    assert "synth-cue.mjs" in tool.description


def sse(*events: dict[str, Any]) -> bytes:
    return "".join(
        "event:result\n"
        + "\n".join("data:" + line for line in json.dumps(e, indent=1).splitlines())
        + "\n\n"
        for e in events
    ).encode()


def speech_events() -> list[dict[str, Any]]:
    word = {"text": "好", "begin_time": 160, "end_time": 400}
    return [
        {"output": {"type": "sentence-begin", "sentence": {"index": 0, "words": [word]}}},
        {
            "output": {
                "type": "sentence-synthesis",
                "audio": {"data": base64.b64encode(b"MP3").decode()},
                "sentence": {"index": 0, "words": [word]},
            }
        },
        {"output": {"type": "sentence-end", "sentence": {"index": 0, "words": [word]}}},
        {"output": {"finish_reason": "stop"}, "usage": {"input_tokens": 1, "output_tokens": 3}},
    ]


async def test_tts_writes_audio_and_deduplicated_seconds_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_client = httpx.AsyncClient
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, content=sse(*speech_events()), headers={"content-type": "text/event-stream"}
        )

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw)
    )
    backend = MemoryBackend()
    tools = {t.name: t for t in build_media_tools(settings(), lambda _rt: backend)}  # type: ignore[arg-type,return-value]
    result = await tools["generate_voiceover"].coroutine(
        text="好", sandbox_path="/workspace/projects/p/vo.mp3", runtime=None, instruction="自然清晰"
    )  # type: ignore[misc]
    assert "wrote" in result
    record = json.loads(backend.files["/workspace/projects/p/vo.words.json"])
    assert record["words"] == [{"word": "好", "start": 0.16, "end": 0.4}]
    assert record["provider"] == "aliyun" and record["usage"]["input_tokens"] == 1
    assert backend.files["/workspace/projects/p/vo.mp3"] == b"MP3"
    body = json.loads(seen[0].content)
    assert body["model"] == "qwen-audio-3.1-tts-flash"
    assert body["input"]["voice"] == "longanhuan_v3.1" and body["input"]["word_timestamp_enabled"]
    assert body["input"]["instruction"] == "自然清晰"
    assert seen[0].headers["Authorization"] == "Bearer " + KEY
    assert KEY not in result and all(KEY.encode() not in value for value in backend.files.values())


@pytest.mark.parametrize(
    "status,events",
    [(403, []), (200, speech_events()[:-1]), (200, [{"code": "AccessDenied", "message": KEY}])],
)
async def test_tts_errors_never_write_partial_files_or_echo_secret(
    monkeypatch: pytest.MonkeyPatch, status: int, events: list[dict[str, Any]]
) -> None:
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    status, content=sse(*events), headers={"content-type": "text/event-stream"}
                )
            ),
            **kw,
        ),
    )
    backend = MemoryBackend()
    tool = next(
        t
        for t in build_media_tools(settings(), lambda _rt: backend)
        if t.name == "generate_voiceover"
    )  # type: ignore[arg-type,return-value]
    result = await tool.coroutine(
        text="好", sandbox_path="/workspace/projects/p/vo.mp3", runtime=None
    )  # type: ignore[misc]
    assert not backend.files and KEY not in result and "wrote" not in result


async def test_image_uses_aliyun_defaults_and_downloads_without_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_client = httpx.AsyncClient
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"data": [{"url": "http://results.oss-cn-beijing.aliyuncs.com/image.png"}]},
            )
        return httpx.Response(200, content=_png(2, 2, 0))

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw)
    )
    backend = MemoryBackend()
    tool = next(
        t for t in build_media_tools(settings(), lambda _rt: backend) if t.name == "generate_image"
    )  # type: ignore[arg-type,return-value]
    result = await tool.coroutine(
        prompt="lamp",
        sandbox_path="/workspace/projects/p/art.png",
        runtime=None,
        background="transparent",
    )  # type: ignore[misc]
    assert "alpha verified" in result
    assert str(seen[0].url) == BASE + "/compatible-mode/v1/images/generations"
    body = json.loads(seen[0].content)
    assert (
        body["model"] == "qwen-image-3.0"
        and not {"quality", "background", "output_format"} & body.keys()
    )
    assert seen[1].url.scheme == "https" and "authorization" not in seen[1].headers
    assert KEY.encode() not in backend.files["/workspace/projects/p/art.png"]


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com/v1",
        "http://example.aliyuncs.com",
        "https://user:pass@example.aliyuncs.com",
        "https://example.aliyuncs.com/?key=x",
        "https://example.aliyuncs.com.evil.test",
    ],
)
def test_only_alibaba_https_endpoints_receive_keys(url: str) -> None:
    with pytest.raises(WorkspaceError):
        aliyun.endpoint(url, "/images/generations")


def test_audio_size_guard_and_untrusted_download_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aliyun, "AUDIO_CAP", 2)
    with pytest.raises(WorkspaceError):
        aliyun.SpeechResult().event(json.dumps(speech_events()[1]))
    with pytest.raises(WorkspaceError):
        aliyun.asset_url("http://169.254.169.254/metadata")


async def test_sfx_uses_next_sound_only_prompt_and_retains_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_client = httpx.AsyncClient
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "output": {
                        "finish_reason": "stop",
                        "audio": {
                            "url": "https://results.oss-cn-beijing.aliyuncs.com/effect.mp3",
                            "duration": 3.2,
                        },
                    },
                    "usage": {"duration": 4},
                },
            )
        return httpx.Response(200, content=b"MP3")

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw)
    )
    backend = MemoryBackend()
    tool = next(
        t
        for t in build_media_tools(settings(sfx_provider="aliyun"), lambda rt: backend)
        if t.name == "generate_sound_effect"
    )  # type: ignore[arg-type,return-value]
    result = await tool.coroutine(
        prompt="water drop",
        sandbox_path="/workspace/projects/p/effect.mp3",
        runtime=None,
        duration_seconds=2,
    )  # type: ignore[misc]
    assert "wrote" in result and "measure with ffprobe" in result
    body = json.loads(seen[0].content)
    assert body["model"] == "qwen-audio-3.1-tts-next"
    assert (
        "No speech" in body["input"]["text_prompt"] and "2 seconds" in body["input"]["text_prompt"]
    )
    assert "duration_seconds" not in body["input"] and "voice" not in body["input"]
    assert "X-DashScope-SSE" not in seen[0].headers and "Authorization" not in seen[1].headers
    record = json.loads(backend.files["/workspace/projects/p/effect.audio.json"])
    assert record["provider"] == "aliyun" and record["requested_duration_s"] == 2
    assert record["provider_duration_s"] == 3.2 and record["usage"]["duration"] == 4
    assert KEY not in result and all(KEY.encode() not in b for b in backend.files.values())


@pytest.mark.parametrize(
    "arguments",
    [{"loop": True}, {"prompt_influence": 0.5}, {"output_format": "wav"}, {"duration_seconds": 0}],
)
async def test_sfx_rejects_unsupported_controls_before_spending(
    arguments: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_request(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Rejected controls must never call the provider")

    monkeypatch.setattr(aliyun, "sound_effect", no_request)
    backend = MemoryBackend()
    tool = next(
        t
        for t in build_media_tools(settings(sfx_provider="aliyun"), lambda rt: backend)
        if t.name == "generate_sound_effect"
    )  # type: ignore[arg-type,return-value]
    result = await tool.coroutine(
        prompt="drop", sandbox_path="/workspace/projects/p/effect.mp3", runtime=None, **arguments
    )  # type: ignore[misc]
    assert "rejected" in result and not backend.files


@pytest.mark.parametrize(
    "status,payload",
    [
        (403, {"message": KEY}),
        (200, {"output": {"finish_reason": "error"}}),
        (
            200,
            {
                "output": {
                    "finish_reason": "stop",
                    "audio": {"url": "http://169.254.169.254/metadata"},
                }
            },
        ),
    ],
)
async def test_sfx_provider_failure_cannot_write_files_or_fallback(
    status: int, payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_client = httpx.AsyncClient
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=payload)

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw)
    )
    backend = MemoryBackend()
    tool = next(
        t
        for t in build_media_tools(settings(sfx_provider="aliyun"), lambda rt: backend)
        if t.name == "generate_sound_effect"
    )  # type: ignore[arg-type,return-value]
    result = await tool.coroutine(
        prompt="drop", sandbox_path="/workspace/projects/p/effect.mp3", runtime=None
    )  # type: ignore[misc]
    assert not backend.files and "wrote" not in result and KEY not in result
    assert len(seen) == 1
