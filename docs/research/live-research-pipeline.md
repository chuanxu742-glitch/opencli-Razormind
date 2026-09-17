# Live research data pipeline

The existing MCP source tools now accept `channel_type: "doubao_research"`.
Create a source with a stable research question, then trigger it with MCP. A
run stores the answer and its de-duplicated URL citations in the normal record
pipeline, so downstream evidence and research operators use the same trace.

```json
{
  "name": "Doubao citation check",
  "channel_type": "doubao_research",
  "channel_config": {
    "question": "麻将机内容为什么会被 AI 反复引用？请列出实际引用的完整来源 URL。",
    "extract_citations": true,
    "site_session": "ephemeral",
    "observation_capture": {"version": "1", "mode": "append_per_run"}
  },
  "tags": ["research", "doubao"]
}
```

`trigger_task` may replace the configured question with
`parameters.question`. The channel asks Doubao to list verifiable URLs and
persists `question`, `citations`, `citation_count`, and `citation_capture` with
the answer. URL extraction does not alter the research question. A zero count
is recorded as zero; it is not presented as evidence.
`ephemeral` is the default because it isolates a collection run from an
interactive browser session; use `persistent` only when its logged-in session
has been verified for background collection.

`observation_capture` is optional and only supported by `doubao_research`.
When enabled, every completed TaskRun persists one append-only answer snapshot
per distinct answer hash in `geo_answer_observations`, including an answer that
the normal `collected_records` projection skips as already known. Each snapshot
has its collector-owned `observed_at`, `source_id`, `task_id`, and `task_run_id`.
Read it through `GET /api/v1/geo-observations?source_id=<id>` (optional
`task_id`, `task_run_id`, `page`, `limit`). The normal record de-duplication
contract remains unchanged for all other sources and for this source's normal
record projection.

For one public Douyin video, use `channel_type: "douyin_detail"`. It takes a
canonical video URL (or a numeric `aweme_id`) and records the description,
author, timestamps, engagement fields, tags, cover and media URL through the
same normalizer and evidence pipeline.

```json
{
  "name": "Douyin video detail",
  "channel_type": "douyin_detail",
  "channel_config": {
    "url": "https://www.douyin.com/video/7664819289043537167"
  },
  "tags": ["research", "douyin", "video"]
}
```

For fast video understanding, invoke `tool.realtime.vl.interaction` with a
locally served `JOYAI_VL_URL` and, when the direct video is too expensive:

```json
{
  "videoUrl": "https://example.invalid/video.mp4",
  "sampleVideoFrames": 6,
  "sampleIntervalSeconds": 1,
  "prompt": "按时间点总结画面内容，提取画面可读文字（OCR），并列出可确认的产品、人物和事件。"
}
```

Sampling is opt-in, bounded to 16 JPEG frames, and requires `ffmpeg` on the
worker PATH. Direct video mode remains available for a model deployment that
supports audio transcription; frame sampling intentionally performs visual OCR
and content understanding only.

The same OpenAI-compatible endpoint parameter also works with a local Ollama
vision model. For the installed `qwen2.5vl:3b` model, set the node's
`endpoint` to `http://127.0.0.1:11434` and `model` to `qwen2.5vl:3b`:

```json
{
  "endpoint": "http://127.0.0.1:11434",
  "model": "qwen2.5vl:3b",
  "videoUrl": "<recorded media URL>",
  "sampleVideoFrames": 2,
  "prompt": "描述画面并提取可读文字；无法确认时明确说明。"
}
```
