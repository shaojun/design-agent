"""Alibaba media protocol helpers. Credentials stay on the host; no SDK global settings."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import JsonValue

from design_agent.tools.files import WorkspaceError

AUDIO_CAP = 25 * 1024 * 1024


def endpoint(base: str | None, suffix: str) -> str:
    """Require an operator-configured HTTPS Alibaba endpoint before attaching a credential."""
    if not base:
        raise WorkspaceError("Alibaba media unavailable: configure its ALIYUN_*_BASE_URL.")
    parts = urlsplit(base)
    if (
        parts.scheme != "https"
        or not (parts.hostname or "").endswith(".aliyuncs.com")
        or parts.port not in (None, 443)
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise WorkspaceError("Alibaba media requires an HTTPS aliyuncs.com base URL.")
    return base.rstrip("/") + suffix


def asset_url(url: str) -> str:
    """Provider results are downloaded without authorization or redirects, from Alibaba only."""
    parts = urlsplit(url)
    if (
        parts.scheme not in {"http", "https"}
        or not (parts.hostname or "").endswith(".aliyuncs.com")
        or parts.port not in (None, 443)
        or parts.username
        or parts.password
    ):
        raise WorkspaceError("Alibaba returned an unsupported asset URL.")
    # Official examples return HTTP OSS URLs; signed paths also work over HTTPS.
    return urlunsplit(parts._replace(scheme="https"))


def failure(status: int) -> str:
    """Never relay a provider message that might echo request text or credentials."""
    hint = {
        401: "check the API key and workspace",
        403: "check model permission, region and billing access",
        404: "check the workspace endpoint and model id",
        429: "quota or rate limit reached; check the console before retrying",
    }.get(status, "check the model parameters and Alibaba console")
    return f"Alibaba media HTTP {status}: {hint}."


@dataclass
class SpeechResult:
    """Accumulate a bounded SSE response; repeated word snapshots replace earlier snapshots."""

    audio: bytearray = field(default_factory=bytearray)
    sentences: dict[int, list[dict[str, JsonValue]]] = field(default_factory=dict)
    usage: dict[str, JsonValue] = field(default_factory=dict)
    complete: bool = False

    def event(self, data: str) -> None:
        if data == "[DONE]":
            return
        event = json.loads(data)
        if event.get("code"):
            raise WorkspaceError("Alibaba speech generation failed; check model and voice access.")
        output = event.get("output") or {}
        if encoded := (output.get("audio") or {}).get("data"):
            if len(encoded) > (AUDIO_CAP + 2) // 3 * 4:
                raise WorkspaceError("Alibaba audio exceeds the 25 MB limit.")
            self.audio.extend(base64.b64decode(encoded, validate=True))
            if len(self.audio) > AUDIO_CAP:
                raise WorkspaceError("Alibaba audio exceeds the 25 MB limit.")
        sentence = output.get("sentence") or {}
        if words := sentence.get("words"):
            converted: list[dict[str, JsonValue]] = []
            for word in words:
                start, end = float(word["begin_time"]) / 1000, float(word["end_time"]) / 1000
                if not 0 <= start <= end < 86400:
                    raise WorkspaceError("Alibaba returned invalid word timestamps.")
                converted.append({"word": word["text"], "start": start, "end": end})
            self.sentences[int(sentence["index"])] = converted
        if event.get("usage"):
            self.usage = event["usage"]
        if output.get("finish_reason") == "stop":
            self.complete = True

    @property
    def words(self) -> list[dict[str, JsonValue]]:
        return [word for index in sorted(self.sentences) for word in self.sentences[index]]


async def synthesize(
    key: str, base: str | None, model: str, inputs: dict[str, JsonValue]
) -> SpeechResult:
    result = SpeechResult()
    async with (
        httpx.AsyncClient(timeout=180, follow_redirects=False) as client,
        client.stream(
            "POST",
            endpoint(base, "/services/audio/tts/SpeechSynthesizer"),
            headers={"Authorization": f"Bearer {key}", "X-DashScope-SSE": "enable"},
            json={"model": model, "input": inputs},
        ) as response,
    ):
        if response.status_code != 200:
            raise WorkspaceError(failure(response.status_code))
        if "text/event-stream" not in response.headers.get("content-type", ""):
            raise WorkspaceError("Alibaba speech returned an unexpected response format.")
        data: list[str] = []
        size = 0
        async for line in response.aiter_lines():
            size += len(line)
            if size > 2 * AUDIO_CAP:
                raise WorkspaceError("Alibaba speech response exceeds the size limit.")
            if line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
            elif not line and data:
                result.event("\n".join(data))
                data = []
        if data:
            result.event("\n".join(data))
    if not result.complete or not result.audio:
        raise WorkspaceError("Alibaba speech stream incomplete; no audio written.")
    return result


@dataclass(frozen=True)
class SoundResult:
    url: str
    duration: float | None
    usage: dict[str, JsonValue]


async def sound_effect(
    key: str, base: str | None, model: str, prompt: str, duration: float | None
) -> SoundResult:
    """Request a complete sound file; duration is a prompt instruction, not an API guarantee."""
    description = f"Sound effects only. No speech, narration, singing or music. {prompt}"
    if duration is not None:
        description += f" Aim for {duration:g} seconds total, including the decay."
    async with httpx.AsyncClient(timeout=300, follow_redirects=False) as client:
        response = await client.post(
            endpoint(base, "/services/audio/tts/SpeechSynthesizer"),
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "input": {
                    "text_prompt": description,
                    "format": "mp3",
                    "sample_rate": 44100,
                    "channels": 2,
                },
            },
        )
    if response.status_code != 200:
        raise WorkspaceError(failure(response.status_code))
    payload = response.json()
    if payload.get("code") or payload.get("output", {}).get("finish_reason") != "stop":
        raise WorkspaceError("Alibaba sound generation failed or incomplete; no audio written.")
    audio = payload["output"]["audio"]
    duration = audio.get("duration")
    if duration is not None and not 0 < float(duration) <= 240:
        raise WorkspaceError("Alibaba returned invalid audio duration.")
    return SoundResult(
        url=asset_url(audio["url"]),
        duration=float(duration) if duration is not None else None,
        usage=payload.get("usage") or {},
    )
