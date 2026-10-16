# Screen Context Module

Use this optional module when a video depends on visible screen text, charts, UI labels, slide titles, code, formulas, or signs that ASR cannot hear.

## Principle

The same multimodal AI that performs segmentation and translation should perform this step when possible. Do not bind the workflow to a specific model, platform, operating system, or product.

The module is lightweight context, not a replacement for ASR or word timestamps. Fun-ASR remains the transcript source of truth, and `SRC_RAW` must still come only from the word stream.

Screen context is off by default. Enable it only after the agent has reviewed the user request, filename, ASR transcript, and visible-context signals and has a concrete reason to believe screenshots will improve translation quality.

## When To Use

Use screen context when:

- the user asks for screen-text awareness;
- the video is a screen recording, trading chart, slide lecture, software demo, film scene with signs/subtitles, or code walkthrough;
- the transcript contains deictic phrases such as `this`, `here`, `look`, `click`, `on the screen`, `chart`, `indicator`, `number`, `left/right`, `top/bottom`;
- ASR appears to miss or mangle visible terms, product names, ticker symbols, formulas, indicators, menu labels, or chart annotations.

Skip it when the video is ordinary speech and the screen contains no useful text.

## Cost Expectation And Decision Checklist

Measured reference (Lesson 3 A/B test, ~25 min trading course video): enabling screen context
added roughly 40 minutes of processing and produced only marginal translation improvement.
Treat screen context as expensive by default.

Enable it only when ALL of the following hold:

1. The spoken audio alone is genuinely insufficient — key numbers, indicator settings, menu
   labels, or slide text are never read aloud but are required for a correct translation.
2. The user has confirmed the screen text matters (or explicitly asked for it).
3. The expected gain justifies roughly 30-60 extra minutes of processing and extra
   multimodal-model cost, and the user has been told about that overhead.

Keep it OFF when any of the following hold:

- talking-head, interview, podcast-style, or camera-recorded content;
- the screen shows only decorative slides whose content the speaker reads aloud;
- chart/UI text is visible but the speaker already names every level, indicator, and number;
- the goal is a fast first-pass subtitle that a human will review anyway.

A/B evidence, timing data, and per-run notes should go to `eval/known_issues.md` in the
dev workspace (`work/eval/`) so future strategy changes are
grounded in measured results, not guesses.

Before enabling it, ask the user when practical:

```text
Does this video contain dense PPT, software UI, charts, code, signs, or other important screen text?
```

If the user does not know, decide after ASR by looking for screen-dependent expressions such as `look here`, `on the screen`, `this chart`, `click`, `indicator`, `slide`, `button`, `as you can see`, and similar phrases.

## Capture Strategy

Prefer a hybrid strategy:

1. Start from targeted frames at subtitle/ASR times that contain screen-dependent phrases.
2. Add sparse baseline frames only when the whole video is a dense screen recording.
3. Deduplicate near-identical frames and frames within about 5 seconds of each other unless the screen changed materially.
4. Keep the frame count modest. The goal is to provide context, not to fully analyze every frame.

Frame limits:

- Default target: 6-12 screenshots per video.
- Soft maximum: 12 screenshots for normal videos under 30 minutes.
- Long-video maximum: 16 screenshots for 30-60 minute videos.
- Hard maximum: 20 screenshots. If more seem necessary, ask the user before continuing.

Do not sample every 20-30 seconds across a long video unless the user explicitly requests dense OCR. Too many screenshots dilute attention, increase cost, and produce junk files.

Frame capture is local and deterministic with `ffmpeg`, which is cross-platform and already required by this workflow. Do not use PIL for contact sheets or frame handling unless the user explicitly asks for custom image processing. Visual text extraction and scene interpretation are done by the multimodal AI agent. Do not use local OCR as a fallback; if the current AI cannot inspect images, ask the user to switch to a multimodal-capable model, manually provide screen context, or skip screen context.

## Output Contract

Write the result to:

```text
<outputs-dir>/.work/<run-id>/work/screen_context.txt
```

Use concise time-stamped entries:

```text
00:04:18
Visible text: CCI with averages (11, hlc3, 9, 45)
Scene context: TradingView AAPL 15-minute chart with CCI panel.
Translation hint: "CCI with averages" means "CCI 和 CCI 的平均线".
```

Rules:

- Keep entries factual and short.
- Include only text or visual context that helps translation, terminology, segmentation, or timing.
- Do not invent words not visible or not supported by the frame.
- Do not add commentary, jokes, translator notes, or long explanations.
- Do not let OCR text override ASR word timestamps. Use it only to repair `SRC_DISPLAY`, `ZH`, terminology, and context understanding.
- Prefer a short consolidated `screen_context.txt` over retaining raw visual notes. Delete or clean up screenshots and scratch files after the final subtitle is accepted.

## How The Prompt Uses It

If `work/screen_context.txt` exists before `prompt.txt` is generated, the workflow injects it into the AI segmentation/translation prompt. The translator should use it as context for visible terms and references, while still keeping every segment aligned to the current `SRC_RAW` span.
