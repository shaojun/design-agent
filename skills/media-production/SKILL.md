---
name: media-production
description: Route any design or motion request to the right pipeline and skill, from a single still to a launch film. Use first for every new ask.
---

# Media production

Pick the pipeline from what the person will receive, then open its skill.

| They will receive | Pipeline | Skill |
| --- | --- | --- |
| One image, an ad set in several sizes, a social card, a banner, a paged PDF | HTML still rendered in the browser, every size recomposed | `stills` |
| A deck | HyperFrames `slideshow` (animated) or HTML-to-PDF (static) | `stills`, `slideshow` |
| A chart, stat card or diagram | JSON spec through ECharts or D2 with brand tokens | `data-viz` |
| An edited image: crop, resize, cut-out, composite, format | `img.mjs` operations on the file | `image-ops` |
| A generated image, texture or plate | `generate_image`, then image-ops | `image-generation` |
| A GIF | a short render or clip, palette-optimized | `gif-edit` |
| Thumbnails | a set of stills with legibility checks | `thumbnails` |
| A storyboard | frames from HTML or HyperFrames snapshots, one contact sheet | `storyboard` |
| A video, animation or motion graphic | storyboard, animatic, HyperFrames composition, render | `storyboard`, `motion-direction`, `hyperframes` |
| A 1-3 minute launch film | the `launch_film` pipeline (`run_pipeline`) | `launch-film` |
| Voice, music or sound | host audio tools, then the mixer | `sound` |
| A teardown of a reference film | cuts, timing and audio measured, then compared | `film-breakdown` |
| What to learn from a film someone shares | a frame-by-frame study written to `workspace/references/` | `reference-study` |

If the ask is vague, use `content-brief` first: at most two questions, each with its reason;
otherwise decide and say what you decided.

Judgment sits beside every pipeline: `brand-design` for the concept gate, ground, accent and type,
and for keeping a supplied reference's structure; `design-wiki` for hierarchy, typography, colour,
layout, platform specs and the review rubric. For a job with several outputs or a hard requirement,
write `artifact-contract.json` from
[brand-design's example](../brand-design/references/artifact-contract.example.json) and check it
with `artifact-contract.mjs` at plan and final.

## Where work lives

Each job gets `/workspace/projects/<slug>/` with `BRIEF.md` (the ask in your words), `PLAN.md`
for anything longer than a few minutes, sources, compositions, `renders/` and `review/`. Files
people drop in the thread are in `/workspace/attachments/`.

Self-hosted Docker workspaces can expire after their container stays stopped (three days by default). If earlier
sources or attachments are missing, ask for them again; saved chat context does not restore files.

For image references, use `look` to answer visual questions without retaining the image in the
lead's context. Self-hosted Docker `read_file` also returns complete images up to 20 MiB;
larger originals stay in the sandbox, where you can create a smaller preview or crop before
looking. A model provider can impose a smaller image request limit.

For supplied video, operate on the local file with FFmpeg or HyperFrames (`media-use`).
`read_file` can sample video into timestamped images for your own vision. For a whole-film
overview, pass only `file_path`: at most 10 frames, paired about one second apart near 0%,
25%, 50%, 75%, 100%; the end pair shifts before EOF and short films are deduplicated.
For a local action, explicitly pass `offset` and `limit` in seconds (0.5 fps, up to 64 frames).
Docker reads videos up to 100 MiB; each decode has a 4 MiB frame budget and 10-second deadline. Read
coverage warnings, choose short windows around important actions, and never claim the whole
film was inspected from one sample. Provider image limits also apply. For longer material,
extract frames or contact sheets (`reference-study`), then use `look`; transcribe speech
separately with `transcribe_audio`. Do not pass an MP4 to `look` or assume a successful attachment
download means the film has been inspected. These reads do not invoke `look` per frame.

## Before you deliver

1. `node /workspace/tools/brand-lint.mjs <composition.html>` passes (exit 0). Exit 1 means fix
   the composition; exit 2 means the gate could not run, which you report, not ignore.
2. Look at every output yourself (`look` with the files and one question; `visual-verify` says
   how). Fix what is off.
3. Check outputs against the ask: `node /workspace/tools/artifact-contract.mjs --expect
   <file>:<W>x<H> ...`.
4. Deliver with `upload_files`, then reply in one or two lines naming the calls you made.

The delivery tool states the current channel's per-file limit (Feishu: 30 MiB). Check final file
sizes before uploading; reduce or split an oversized output and recheck its contract. An
unconfirmed upload is not delivery: report it and keep the source files for recovery.

Motion adds `node /workspace/tools/video-gate.mjs <index.html>` and `hyperframes check` (it lints
too) before any render, and a storyboard sign-off before production.

The sandbox has no network. Copy what a composition loads next to it: GSAP from
`/opt/design-agent/render/node_modules/gsap/dist/gsap.min.js`, the brand's `tokens.css` and logos
from `/workspace/brand/`. Upstream HyperFrames examples load libraries from a CDN; that fails here.

## Paper

When Paper is connected and the work is something people will want to nudge (layouts,
storyboards, thumbnails), draft it on the canvas as well (`paper-design`). The thread always gets
an image people can approve, whether or not Paper is connected.
