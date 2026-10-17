# Changelog

本仓库只维护 `aiaaaa4.video-translate` 一个 Skill。版本号以 `registry.json` 为准，详细推理见对应 Git 提交。

## 1.11.3

- 删除仓库级产品说明书 `docs/`，同时清除 README、SKILL.md、问卷中的指引。
- 删除问卷与文档里的「推荐在 Codex 中使用 GPT-5.6」建议（模型建议易过期）。
- 测试改为反向断言：问卷不得再出现说明书链接与该模型建议。

## 1.11.2

- 问卷第 8 条去掉 `-filetrans` 内部术语，改为「只有你明确要求改用文件转写兜底方式时」。
- 首次说明补明 `.env` 的查找范围（Skill 目录或上级工作区根目录均可被自动发现）。
- 首次说明补明 `check_env.py` 的检查项：Python ≥3.9、ffmpeg、ffprobe、凭据。

## 1.11.1

- 把问卷、首次说明、agent 元数据、CLI 帮助与报错、`screen_context.md` 同步到新的传输方式。
- 新增回归测试，禁止问卷与文档再出现「上传 OkFile + Fun-ASR」的旧措辞。

## 1.11.0

- 转写传输改为「本地归一化 → 5 分钟上限切块 → Base64 内联同步提交」，默认不再经 OkFile 公网中转。
- 切点优先落在目标边界前 30 秒内的静音中点，块间不重叠；单块音频不切分。
- 逐块退避重试，失败报出块序号与原因；词级时间戳按块偏移拼接。
- 落盘前强制校验：单调不减、无重叠重复、覆盖全片（±1 秒），不满足即报错。
- `-filetrans` / `fun-asr` 保留为显式兜底路径；只有该路径需要 `OKFILE_TOKEN`。
- 新增 `ALIYUN_ASR_SYNC_MAX_SECONDS`、`ALIYUN_ASR_SYNC_CONCURRENCY`、`ALIYUN_ASR_SYNC_RETRIES`、`ALIYUN_ASR_MP3_KBPS`。

## 1.10.2

- 凭据文件改为自动发现（显式 `--env-file` → `$ALIYUN_ENV_FILE` → 从 Skill 目录向上查找），不再依赖当前工作目录。
- 主流程解析一次并显式传给转写、分段、环境检查三个步骤。
- 缺失凭据时一次列出全部缺失项与实际搜索路径；显式指定的文件不存在则立即失败。
- 修复词级 `confidence` 被丢弃、恒为 0.0 的问题。

## 1.10.1

- `SKILL.md` 中越出 Skill 目录的说明书链接改为仓库链接，并新增「安装包内相对链接不得越界」回归测试。

## 1.10.0

- 转写模型由 `fun-asr` 切换为 `qwen-audio-3.1-asr-flash-filetrans`（同端点、同结果结构）。
- `ALIYUN_ASR_MODEL` 开始真正生效（此前写在模板里但代码未读取）。
- 新增 `ALIYUN_ASR_HOTWORDS_FILE` 即时热词；无词级时间戳时直接报错。
- 移除源码中的个人热词表与工作空间/热词库标识，新增热词迁移工具 `tools/migrate_asr_hotwords.py`。

## 1.9.6

- 修复中文源字幕句尾丢字：分段器改为 CJK 感知的字符级对齐，并加入覆盖率断言。

## 1.9.5

- 分发审计改为从注册表动态生成独立仓库链接。
