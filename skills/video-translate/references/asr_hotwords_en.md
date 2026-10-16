# ASR Hotword Maintenance

Purpose: maintain English ASR hotwords for the fixed Alibaba ASR model (`ALIYUN_ASR_MODEL`, default `qwen-audio-3.1-asr-flash-filetrans`). These entries are for recognition accuracy only. Chinese translations belong in `trading_glossary.md` and `term_repair_rules.json`.

Hotwords are supplied through local configuration, never through this repository:

- `ALIYUN_ASR_HOTWORDS_FILE` — path to a local JSON file sent as inline hotwords.
- `ALIYUN_ASR_VOCABULARY_ID` — a precompiled Alibaba list bound to the model in `ALIYUN_ASR_MODEL`.
- Auth: local `DASHSCOPE_API_KEY` from `.env`.

The workspace identifier, the vocabulary identifiers, and the word list itself are installation-specific and are not committed here.

## Model Migration: Why A Precompiled List Does Not Carry Over

A precompiled hotword list is bound to the `target_model` it was created with, and that binding is immutable: `update_vocabulary` has no `target_model` parameter. When the recognition model and the list's `target_model` differ, Alibaba **does not error** — the task succeeds and returns text, but every hotword silently stops applying. Recognition quality therefore degrades with no visible signal.

Two supported ways to carry this list forward:

1. **Inline hotwords (recommended, no console work).** Export the entries and pass them in every request as `parameters.vocabulary`. Inline hotwords are supported only by the Qwen-Audio-3.x models, need no precompiled list, cost nothing, and have no binding to get wrong. Point `ALIYUN_ASR_HOTWORDS_FILE` at the exported JSON.
2. **A new precompiled list.** Recreate the list with `target_model` set to the new model, then set `ALIYUN_ASR_VOCABULARY_ID` to the new ID. Note the account-wide limit of 10 lists, shared by all models, so delete the old list once the new one is proven.

Export and verification are done with `tools/migrate_asr_hotwords.py` (dry-run by default). Export first, compare recognition quality on one real video, and only then switch production over.

Weights: `1`-`5` for normal hotwords (use 4 as the starting point) and `50` for at most 50 "super hotwords" that only the Qwen-Audio-3.x models support. At most 2000 hotwords apply per request.

Maintenance rules:

- Keep ASR hotwords concise. Prefer proper names, tickers, platform names, acronyms, and multi-word terms that ASR often splits or mishears.
- Do not put Chinese translations here.
- Do not add ordinary English words unless repeated ASR errors prove they are necessary.
- For pure English/ASCII entries, keep each item at 7 space-separated fragments or fewer.
- Deduplicate case-insensitively before updating the remote vocabulary.

## Maintenance SOP

This file documents the maintenance method; the word list itself lives in the installation's local configuration described above. Remote vocabulary maintenance is not part of a normal translation run. Before any remote update, obtain a separate explicit confirmation that names the Alibaba workspace and vocabulary ID, explains that the full vocabulary is replaced, and records or exports the current remote entries for rollback. The remote Alibaba vocabulary should mirror the local hotword list only after that approved maintenance.

### Decide ASR Hotword vs Translation/QC Rule

Add a term to the ASR hotword list when the problem happens before translation:

- `SRC_RAW` contains the wrong word, a split word, or a misheard proper noun.
- The ASR model repeatedly splits a multi-word product, platform, indicator, ticker, person, or brand name.
- The term is a high-value acronym/ticker/indicator that should be recognized exactly even if it appears only once.

Examples:

- `r hyth mic` should become `Rithmic`.
- `trade ev ade` should become `Trade Evade`.
- `from Tony` in a date/number context may need review because the intended phrase can be `from twenty`.
- `GEX`, `NQ`, `First Red Day`, `Net Convexity`, and product/person names belong here when they affect ASR recognition.

Do not add a term to ASR hotwords when the audio was recognized correctly and only the Chinese wording is wrong. Put those fixes in `trading_glossary.md` or `term_repair_rules.json`.

Examples:

- `scalper` recognized correctly but translated as `黄牛`: translation/QC rule.
- `first red day` recognized correctly but translated as `第一个红日`: translation/QC rule.
- `gamma/delta/vega/theta` recognized correctly but rendered as Chinese: translation/QC rule.
- Awkward Chinese style, overly long subtitles, punctuation, or line breaks: QA/export rule, not ASR hotword.

### Promotion Threshold

Promote a candidate into ASR hotwords when one of these is true:

- The same ASR mistake appears in two or more videos.
- The term is a known recurring domain term, ticker, product name, platform name, indicator, or speaker/person name for the current video set.
- The term is rare but high-impact: one ASR error would likely break alignment, confuse translation, or damage domain credibility.

Do not promote one-off common English words unless a repeated ASR failure proves they are needed.

### Review Cadence

- After every 3-5 same-domain videos, review `final_qa_report.md`, `segments.txt`, and obvious ASR mistakes in `SRC_RAW`/`SRC_DISPLAY`.
- For a single large batch, review once after the batch finishes.
- Update the remote ASR vocabulary only after collecting a small stable batch of candidates, unless a high-impact proper noun or ticker must be fixed immediately.

### Update Procedure

Do not perform these steps from an ordinary subtitle request. First show the proposed additions, workspace ID, vocabulary ID, overwrite behavior, and rollback copy, then obtain explicit confirmation for this remote state change.

1. Add approved terms to the local hotword list, not to this file.
2. Query the remote vocabulary.
3. Merge old terms and new terms.
4. Deduplicate case-insensitively.
5. Preserve existing terms.
6. Submit the full merged vocabulary to Alibaba, because update is treated as a full vocabulary update rather than a simple append.
7. Re-query and verify key terms exist.
8. Keep `.env` pointing to the active hotword source: `ALIYUN_ASR_HOTWORDS_FILE` for inline hotwords, or an `ALIYUN_ASR_VOCABULARY_ID` whose `target_model` matches `ALIYUN_ASR_MODEL`. Never leave a list bound to a different model in production; it fails silently.

Never expose `DASHSCOPE_API_KEY` or package `.env`.

## Where The Live List Lives

The concrete hotword set is a running installation's local configuration and is deliberately absent from this repository. Keep it in one of:

- the local JSON referenced by `ALIYUN_ASR_HOTWORDS_FILE` (inline hotwords, Qwen-Audio-3.x models only), or
- a precompiled Alibaba list referenced by `ALIYUN_ASR_VOCABULARY_ID` whose `target_model` matches `ALIYUN_ASR_MODEL`.

Neither the word list nor the workspace and vocabulary identifiers belong in this source repository. Use `tools/migrate_asr_hotwords.py` to export, inspect, or re-create a list; it takes those identifiers as arguments, defaults to a dry run, and never echoes the API key.
