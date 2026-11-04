# Surfaces: Slack, the API and the CLI

Every surface reaches the same agent. What differs is presentation: in Slack the agent shows its
work as it goes, takes follow-ups mid-job, and ends each reply with a short footer.

## Slack presentation

Code: `src/design_agent/surfaces/slack/` (Web API client, who a run answers, step phrases, the
stream) and `src/design_agent/middleware/{progress,footer,steering,tracing}.py`.

- **Live progress.** One message per turn shows a card per step in plain studio language
  ("Studying the brand", "Rendering the teaser"), through Slack's streaming API
  (`chat.startStream`/`appendStream`/`stopStream`), or one message redrawn with `chat.update` when
  streaming is refused. It settles to "Done", or is deleted when the turn's reply is `[quiet]`.
  Cards never carry argument text: no paths, queries, file ids or anybody's words. Each background
  helper streams its own message.
- **Footer.** Only what changes: agents still running, time so far, estimated cost of the job, and
  a trace link when `DESIGN_AGENT_TRACE_HOST` is set (a trace URL exposes workspace and run ids, so
  it is off by default).
- **Steering.** A follow-up while helpers run starts a turn at once (the lead ends its turn after
  handing work off), sees what is running, and can pass the note to a helper with `steer_task`;
  the helper reads it before its next model call. While the lead's own turn is busy, it reads new
  messages in the thread before each model call and folds them in; "stop", "cancel" or "never mind"
  ends the turn at its next step.
- **Trace tags.** Each run is tagged with its trigger, surface and the kinds of files delivered, and
  names the template and version it came from (`DESIGN_AGENT_TEMPLATE_TELEMETRY=0` turns that
  off); no user ids by default.

The bot token stays on the host: progress and footer call the Slack Web API from middleware, and
the sandbox never sees a token, a Slack URL or a Slack payload.

## Managed Deep Agents

MDA's channel posts the final reply, reacts to the starting message, transfers attachments to
`/workspace/attachments/` and provides `upload_files`. Presentation uses the reserved
`mda/slack-bot-token` connection and the event on `runtime.channel`.

A second message in a busy Slack thread becomes a run queued behind the current one (there is no
public way to add input to a running run). The steering middleware folds it into the running turn
by reading the thread, so the queued run ends at once with "Folded that into the work above." (it
has to say something: MDA posts a scheduled or queued run's newest assistant text). A new
top-level direct message is its own thread and runs in parallel; only the platform can route it to
the working thread. Upstream asks: [docs/upstream.md](../upstream.md).

## Self-hosted Slack

`design-agent slack` runs a Bolt app in one process with the agent (Socket Mode by default, or
signed HTTP on `/slack/events`): mentions and direct messages start turns, duplicate deliveries
start one, attachments are fetched on the host with the bot token, finals upload to the thread,
and wake-up replies post there. The same middleware draws progress and the footer. Manifest:
`config/slack-manifest.example.yaml`.

## Self-hosted Feishu

`design-agent feishu` uses the official `lark-oapi` long connection on the main thread and runs
the shared local runtime on a separate asyncio loop. SDK callbacks enqueue events and return
immediately; they do not wait for models, attachment downloads or renders. App and tenant must
match the configured installation, and only people can trigger a run. Direct messages share one
conversation until `/new` selects a fresh persisted session and sandbox; old work remains intact.
Group topics have separate thread ids. A group topic starts with an exact bot
mention; subsequent messages are handled only in a known topic, when platform permissions deliver
them. Turns and attachment staging serialize per conversation. The store retains the latest 256
message ids per conversation for replay deduplication.

Resources are downloaded from fixed Feishu API paths on the host with a size cap, never from a
URL in user content. The shared local attachment runtime preprocesses PDFs inside the sandbox
before invoking the graph: text, page previews and embedded images are indexed in a manifest
whose path is supplied to the agent. Visual review uses the same `look` tool as video frames;
the host does not parse PDF bytes or send every image into the lead's context. Extraction limits
and warnings are explicit ([Feishu](../feishu.md)). `FeishuFileDelivery` sends `upload_files` outputs back to the same chat or
topic, with a 30 MiB per-file limit also exposed in the tool schema description. Host progress
middleware retains each lead tool call in default-collapsed JSON 2.0 cards, with actual result
status and elapsed time. Updates coalesce at three seconds without dropping steps; groups of 25
steps share a card until its reserved size budget requires a continuation. Disabling progress
suppresses these cards. Card failures disable progress for that turn
without stopping execution or final delivery. Whole-card refreshes may reset a person's expanded
panel. Background helper internals are not included; wake-up replies use the adapter's reply callback.
Follow-ups queue rather than steering a busy run. This is a self-hosted transport; MDA's channel
declarations are unchanged. Setup and recovery limits: [飞书接入](../feishu.md).

## The API and the CLI

The API returns each turn's reply and delivered files, and background news through
`GET /threads/{id}/updates` ([self-hosting](../self-hosting.md)). The CLI prints each step's tool
name as it runs and stays open while background work continues.
