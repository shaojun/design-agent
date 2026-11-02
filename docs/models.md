# Models

Design Agent talks to models through LangChain, so any chat model with tool calling and vision can
run it. Set one in `.env` (or `uv run design-agent config set …`):

```bash
DESIGN_AGENT_MODEL=anthropic:claude-opus-5-5     # provider:model
ANTHROPIC_API_KEY=…
```

`provider:model` is LangChain's `init_chat_model` form. Anthropic and OpenAI are installed by
default; Google, Groq and Mistral are extras (`uv sync --extra google`). The LangSmith Gateway is an
optional preset (`langsmith:anthropic/claude-opus-5-5` with `LANGSMITH_GATEWAY_API_KEY`, else
`LANGSMITH_API_KEY`), never required. Anthropic and OpenAI models go through the gateway's native
routes and keep every feature in the table below; other providers use its shared route. For an OpenAI-compatible endpoint, set `DESIGN_AGENT_MODEL_BASE_URL` and
`DESIGN_AGENT_MODEL_API_KEY_ENV` (the name of the variable that holds its key).

## DeepSeek and Alibaba Cloud

These endpoints use the installed OpenAI-compatible chat client (`openai:` selects the protocol,
not the company receiving the request). Set all three endpoint settings together:

```bash
uv run design-agent config set \
  DESIGN_AGENT_MODEL=openai:deepseek-flash \
  DESIGN_AGENT_MODEL_BASE_URL=https://api.deepseek.com \
  DESIGN_AGENT_MODEL_API_KEY_ENV=DEEPSEEK_API_KEY
```

Set `DEEPSEEK_API_KEY` in the host environment or private `.env`. The current `deepseek-flash`
endpoint serves V4.1 Flash with image input; the older `deepseek-v4-flash` name is a legacy alias,
not a separate registry entry. The DeepSeek Chat Completions adapter explicitly enables thinking
and defaults to `high` effort (`look` uses `low`). It retains `reasoning_content` from regular and
streaming responses in message metadata and replays it on every historical assistant message,
including final answers, in subsequent requests. Checkpoints and helper transcripts preserve it;
it is not added to the user-visible answer. Old non-thinking messages keep their history and send
an empty reasoning field; no reasoning is fabricated.

DeepSeek receives `reasoning_effort`, not the OpenAI Responses `reasoning` object. `low`, `high`
and `max` pass through; `minimal` maps to `low`, `medium` and `xhigh` to `high`, and `ultra` to `max`,
following DeepSeek's documented mapping. Changing the effort does not turn thinking off. The
generic `ChatOpenAI` client alone drops DeepSeek reasoning fields, so do not bypass this adapter.
Restart the backend after updating it; an already-running process keeps its existing model clients.

Alibaba Cloud Model Studio uses the same route with `DESIGN_AGENT_MODEL=openai:qwen3-vl-plus`
(or `openai:qwen3-vl-flash`), `DESIGN_AGENT_MODEL_API_KEY_ENV=DASHSCOPE_API_KEY`, and the
OpenAI-compatible base URL shown in your Model Studio console. The API key and endpoint must belong
to the same region. These Qwen entries are documented for vision and function calling; they still
need a live check with your Alibaba credential.

`doctor` checks the selected endpoint's named key, including keys in the project `.env`, without
requiring an Anthropic or OpenAI key. Run `uv run design-agent test model` for a small billable text
probe. This does not establish visual quality or complete design-task success.

All inherited roles use the lead's endpoint and key. A different model id for a role does not
inherit that endpoint; using different models on the same custom endpoint needs further routing
support. Other vendors can use the same configuration once their exact vision and tool-calling
model ids have been checked and added to the registry. Keys stay on the host, outside Docker.
AI image generation uses its own `OPENAI_API_KEY`; changing the chat endpoint does not change it.

## Roles

| Role | Setting | Default | What it does |
| --- | --- | --- | --- |
| Lead | `DESIGN_AGENT_MODEL`, `DESIGN_AGENT_MODEL_EFFORT` | `anthropic:claude-opus-5-5` at `high` | the conversation, plans, creative calls |
| Helpers | `DESIGN_AGENT_HELPER_MODEL`, `DESIGN_AGENT_HELPER_EFFORT_CAP` | the lead's model; effort per task (`low`, `xhigh`, `max`) | background builders, critics, renders |
| `look` | `DESIGN_AGENT_LOOK_MODEL` | the lead's model at `low` | looks at renders (up to 8 images per call) |
| Tool selector | `DESIGN_AGENT_TOOL_SELECTOR_MODEL` | the lead's model | picks MCP tools, only for models without native tool search |
| Learning | `DESIGN_AGENT_LEARNING_MODEL` | a low-effort helper | the post-job review that proposes observations |

Every role needs vision. `uv run design-agent doctor` checks each one against the registry.

## The capability registry

`src/design_agent/models/registry.py` records, per exact model id, what the agent may rely on:
vision, effort levels, context and output limits, prompt-cache lifetimes, server-side context
editing, and how tools are disclosed. Only verified entries claim provider features; anything else
runs on the portable path (all features that do not depend on the provider).

| Model | Tool disclosure | Notes |
| --- | --- | --- |
| `anthropic:claude-opus-5-5`, `claude-fable-5-1`, `claude-sonnet-5-5` | provider tool search | efforts `low`…`max`; adaptive thinking; 5 m / 1 h cache |
| `anthropic:claude-haiku-5-5` | provider tool search | no effort parameter; good for `look` and the selector |
| `openai:gpt-6.1-sol`, `gpt-6-sol`, `gpt-5.6-sol` | provider tool search | Responses API; reasoning effort |
| `google_genai:gemini-3.6-flash` | bounded selector | no provider-side tool search |
| `openai:deepseek-flash` | bounded selector | explicit DeepSeek endpoint; vision; thinking enabled with reasoning replay |
| `openai:qwen3-vl-plus`, `openai:qwen3-vl-flash` | bounded selector | explicit Model Studio endpoint; vision and function calling |
| anything else, the gateway's shared route, or a base URL | bounded selector | no provider-feature claims |

Adding a model: add an entry with the facts from the provider's current documentation, a test in
`tests/unit/test_config_and_registry.py`, and run the eval set (`docs/evals.md`) before relying on it.

## Cost and limits

- Each call is bounded by `DESIGN_AGENT_MODEL_TIMEOUT_SECONDS` (600 by default); provider SDK
  retries are off and the agent's own retry handles transient failures
  ([failure boundaries](architecture/context-and-failures.md#failure-boundaries)).
- The lead's prompt cache lives an hour (`DESIGN_AGENT_CACHE_TTL`), helpers' five minutes.
- Spend is estimated per job from `config/prices.toml`; `DESIGN_AGENT_JOB_BUDGET_USD` makes the
  agent ask before going past a budget.

## Alibaba media tools

Media providers are independent of the lead, helper and vision models. To use Alibaba for image
creation and narration, configure the following host-only settings in the ignored `.env`:

```dotenv
DASHSCOPE_API_KEY=                         # your workspace API key
ALIYUN_API_HOST=<workspace>.cn-beijing.maas.aliyuncs.com
ALIYUN_OPENAI_BASE_URL=https://<workspace>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
ALIYUN_DASHSCOPE_BASE_URL=https://<workspace>.cn-beijing.maas.aliyuncs.com/api/v1
DESIGN_AGENT_IMAGE_PROVIDER=aliyun
DESIGN_AGENT_IMAGE_MODEL=qwen-image-3.0
DESIGN_AGENT_TTS_PROVIDER=aliyun
DESIGN_AGENT_TTS_MODEL=qwen-audio-3.1-tts-flash
DESIGN_AGENT_TTS_VOICE=longanhuan_v3.1
DESIGN_AGENT_SFX_PROVIDER=aliyun
DESIGN_AGENT_SFX_MODEL=qwen-audio-3.1-tts-next
```

Use the exact endpoints displayed with the key; the region, workspace and model must match.
`ALIYUN_API_HOST` records the console value; requests use the two explicit base URLs. The key stays
on the host and is never staged into the sandbox. `doctor` checks configuration, not account access.
Existing processes must reload configuration before using changes; saving `.env` does not restart
Slack or deploy an update.

`generate_image` uses `/images/generations`, downloads the returned image URL without the API key,
and saves PNG. Qwen's provider-default quality applies; `quality=low` does not establish a cheaper
price. `params` supports provider extensions; do not put `quality`, `background` or `output_format`
inside it. A transparent background request is added to the prompt and the returned pixels checked.

`generate_voiceover` uses streaming HTTP TTS to collect MP3 and word timestamps in seconds into the
existing `.words.json` schema, with `provider: "aliyun"` and reported usage. Omit `voice_id` and
`model` to use the configured defaults; direct delivery with `instruction`. Missing timestamps are
reported and must be obtained by transcription before caption synchronization. In a sound plan,
mark generated voice files `source: "aliyun"` so the provenance ledger records the provider.

`generate_sound_effect` routes independently through `DESIGN_AGENT_SFX_PROVIDER`. Alibaba uses
`qwen-audio-3.1-tts-next`, the DashScope base URL and the same host key. It sends a sound-only prompt
and saves MP3 plus `.audio.json` provenance, provider-reported duration and usage. Requested duration
is a prompt instruction, not an exact-length guarantee. Measure the saved file before placing it;
`loop=true` and `prompt_influence` are rejected without a provider call. The API makes no automatic
fallback to local synthesis on failure.

The tool description exposes the host's no-speech/no-music direction: do not request piano chords,
musical stings or a score through Alibaba effects. Use the music tool, an approved existing track,
or local synth cues for tonal accents. Optional spectrum inspection uses the sound skill's
`spectrogram.mjs`, with a fixed FFT-friendly height, a 30-second maximum input window and a
10-second process timeout; a failed diagnostic must not block mixing. Use it only for onset, ending
and pause timing, not sound-quality scoring. Deliver a preview for human audition and explicit
confirmation before production use.

For simple interface cues, use the sound skill's local `synth-cue.mjs` recipes or `source: "synth"`
in the mix plan. They work without a key and remain available when a provider is missing. Generated
effects use `source: "aliyun"`. Music remains separate: `generate_music` requires ElevenLabs or
use a supplied track. Fun-Music access must be verified before a future integration.
