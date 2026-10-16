# Video Translate Maintenance

The public source of truth is [`skills/video-translate`](../../skills/video-translate/). Edit its scripts, references, `SKILL.md`, and `agents/openai.yaml` directly.

The Chinese product guide is [视频翻译工作流说明书.md](视频翻译工作流说明书.md). It is public documentation, not an installable skill dependency.

Run the public test suite from the repository root:

```bash
python3 -m unittest discover -s tests
```

The ignored `local-projects/video-translate skill/` folder is a legacy archive with historical local changes. It is not a release source and must not be copied into a public release without an explicit review.

Use the repository-wide release procedure in [`docs/RELEASING.md`](../RELEASING.md). Public versions and display names are defined only in [`registry.json`](../../registry.json).
