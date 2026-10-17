# 人工级视频字幕翻译

独立 Skill 仓库：`aiaaaa4.video-translate`。

**`aiaaaa4.video-translate` · v1.11.0 · [ClawHub](https://clawhub.ai/aiaaaa4/video-translate)**

这个 Skill 面向本地视频文件或单视频项目文件夹，通过本地确定性检查自动发现项目根目录与 `.work/input/` 中匹配的音频和原语言字幕，并能按实际媒体流识别音频型 WebM、视频显示尺寸和旋转。随后本地归一化并按 5 分钟上限切块，逐块内联提交给阿里 Qwen-Audio-3.1-ASR-Flash 获取词级时间戳（默认不经 OkFile 公网中转），并通过全文分析、翻译、重译审校、语义重分段、确定性 QA 和最终 QC 交付双语 ASS/SRT。ASS/SRT 默认按横版、竖版或方形显示尺寸选择同一份绑定布局；竖版字幕块顶部固定，避免长短字幕上下跳动。普通任务直接复用固定布局，不逐片生成预览或重新调参。成功后，项目文件夹、原视频、ASS 和 SRT 统一使用同一个中文净标题。它支持接收标准 `.work/input/` 素材，但不依赖任何具体 Flow。

完整的人类说明见 `docs/video-translate/视频翻译工作流说明书.md`。

## Repository Boundary

- 唯一资产：`skills/video-translate/`
- 唯一版本来源：`registry.json`
- 通用验证：`python3 tools/validate_repo.py`
- 回归测试：`python3 -m unittest discover -s tests`
- 版本更新：`python3 tools/bump_skill_version.py --skill video-translate --version <version>`
- 发布预检：`python3 tools/release_skill.py --skill video-translate --changelog "<summary>" --dry-run`

该仓库由代码维护项目中的 `video-translate` 独立对话负责。个人翻译偏好和组合步骤属于日常生产 Flow，不写入本仓库。
