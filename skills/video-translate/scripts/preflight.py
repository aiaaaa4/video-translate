#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from setup_state import initialized


FIRST_USE_INTRO = """首次使用说明：`video-translate` 是一套面向本地录制视频的高质量双语字幕工作流。它把选定的音视频在本地归一化（mono / 16 kHz / mp3），按 5 分钟上限切块，逐块以内联 Base64 方式提交给阿里同步语音识别模型，获取词级时间戳，并以该词级时间轴作为字幕边界依据；翻译前会通读完整源文，识别视频领域、术语、人物、专名和歧义，再通过 `qwen-mt-plus` 或当前 Agent 完成初译。初译后还会进行逐段语义复核、跨段错位检查、语义重分段、确定性 QA 和最终全文 QC。只有全部门禁通过后，才会导出双语 ASS/SRT。它的优势是全文理解、词级时间对齐、专业术语一致、连续错位防护、可恢复执行和严格交付门禁。

开始字幕翻译前，请先准备固定转写服务的凭据：
- 阿里百炼（必需）：固定负责转写和词级时间戳，需要 `DASHSCOPE_API_KEY` 与 `ALIYUN_WORKSPACE_ID`。获取 API Key：https://help.aliyun.com/zh/model-studio/get-api-key
- OkFile（可选，仅兜底）：只有在显式选择文件转写（`-filetrans`）模型、需要把音频临时上传成可读取的 HTTPS 地址时才用，需要 `OKFILE_TOKEN`。注册地址：https://www.okfile.com/；API Key 页面：https://www.okfile.com/en/account/api-keys。默认同步路径不上传任何公网中转。
- 请把凭据填写到本机 `.env`，不要在聊天中发送。`.env` 放在 Skill 目录，或其上级工作区根目录（例如工作区根的 `.env`）都会被自动发现；显式指定路径时以指定为准。缺失时，Agent 会在获得同意后创建并打开配置文件。

配置完成后运行 `python scripts/check_env.py`；它会一并检查 Python 版本（≥3.9）、ffmpeg、ffprobe 和凭据，缺什么会逐条列出。检查通过时会在当前用户环境中记录初始化完成；以后直接进入任务问卷。更多信息请阅读 [视频翻译工作流说明书](https://github.com/aiaaaa4/video-translate/blob/main/docs/video-translate/%E8%A7%86%E9%A2%91%E7%BF%BB%E8%AF%91%E5%B7%A5%E4%BD%9C%E6%B5%81%E8%AF%B4%E6%98%8E%E4%B9%A6.md)。"""

QUESTIONNAIRE_BODY = """2. 字幕文件命名：默认从原视频文件名或媒体项目目录提取真实标题，外文标题由 Agent 按视频领域术语翻译为中文。翻译成功后，原视频、项目文件夹、ASS 和 SRT 统一使用这个中文标题。
3. 源语言：默认英语；如不是英语，请回复实际语言。
4. 翻译目标与交付：默认翻译为简体中文，并固定输出中文在上、原文在下的双语 ASS 和 SRT；如需其他目标语言，请填写，否则回复“默认”。
5. 翻译模型：默认选择 A；如选择 B，请明确回复“Agent 大模型翻译”。
   A. `qwen-mt-plus`：阿里旗舰翻译专用大模型，稳定、性价比高，使用同一个阿里 DashScope API Key 调用，适合作为公开 Skill 的默认方案。
   B. 当前 Agent 编排模型：由当前 Agent 在通读全文后直接翻译，不需要额外翻译 API Key，但会消耗当前 Agent 的模型额度，耗时和质量取决于所选模型。若当前环境支持，推荐在 Codex 中使用 GPT-5.6 以获得更高质量。
6. 屏幕上下文：默认关闭；如果 PPT、图表、软件界面、代码或画面文字会影响理解，请回复“开启”。
7. 输出位置：默认输出到视频所在的项目文件夹；如需其他位置，请填写绝对路径。
8. 外发处理：是否同意把处理音频在本地归一化并按 5 分钟上限切块后，逐块内联发送给阿里语音识别服务？默认路径不会上传到任何公网中转（只有你明确要求改用“文件转写兜底方式”时，才会改为上传 OkFile 并发送临时链接）。选择 A 时字幕文本还会发送给阿里 `qwen-mt-plus`；选择 B 时字幕文本由当前 Agent 模型服务处理。
9. 文件覆盖：默认不覆盖任何已存在的同名字幕；如允许覆盖，请明确说明。

如除外发处理外全部使用默认设置（包括选择 A），请回复：确认默认设置，并同意外发处理。"""

QUESTIONNAIRE_GUIDE = "完整流程与字幕布局说明：[视频翻译工作流说明书](https://github.com/aiaaaa4/video-translate/blob/main/docs/video-translate/%E8%A7%86%E9%A2%91%E7%BF%BB%E8%AF%91%E5%B7%A5%E4%BD%9C%E6%B5%81%E8%AF%B4%E6%98%8E%E4%B9%A6.md)。"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show the video-translate onboarding and task questionnaire.")
    parser.add_argument(
        "--resolved-video",
        type=Path,
        default=None,
        help="Video path already resolved by video_to_subtitles.py --inspect-inputs.",
    )
    return parser.parse_args()


def questionnaire(resolved_video: Path | None = None) -> str:
    if resolved_video is None:
        video_line = "1. 视频路径：请提供本地视频文件路径或存有视频的文件夹路径。"
    else:
        display_path = str(resolved_video).replace("\r", " ").replace("\n", " ").replace("`", "'")
        video_line = f"1. 视频路径：已经识别到该路径中的视频文件：`{display_path}`。"
    return f"请一次性确认以下内容：\n{video_line}\n{QUESTIONNAIRE_BODY}"


def main(resolved_video: Path | None = None) -> int:
    if not initialized():
        print(FIRST_USE_INTRO)
        print()
    print(questionnaire(resolved_video))
    print()
    print(QUESTIONNAIRE_GUIDE)
    return 0


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(main(args.resolved_video))
