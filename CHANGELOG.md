# Changelog

本仓库只维护 `aiaaaa4.video-translate` 一个 Skill。版本号以 `registry.json` 为准，详细推理见对应 Git 提交。

## 1.13.0

- 模型白名单收窄到唯一一个 `qwen-audio-3.1-asr-flash-filetrans`；`qwen-audio-3.0-asr-flash-filetrans` 与 `fun-asr` 被移除，设置成这两个值会直接报错而不是静默替换。
- 原因：这三个模型之间从来没有自动兜底，另外两个只是"手动改环境变量"的逃生口，却让"固定单一流程"读起来像有三个选项；`fun-asr` 还是不同代、按秒计费的家族。

## 1.12.2

- 问卷第 8 条与首次说明去掉「归一化」等术语，改成「在本机先转成标准格式的音频（单声道 mp3）」。

## 1.12.1

- 修掉错误分类里已失效的 `okfile` 匹配，改为识别新的临时上传失败（`VTZ-E003`）。
- 修正执行契约中仍写着"默认同步模型、filetrans 只是兜底"的陈旧描述。
- 新增硬守卫测试：安装包与 README 中出现 `okfile` 即失败；凭据必须恰为 `DASHSCOPE_API_KEY` 与 `ALIYUN_WORKSPACE_ID`。

## 1.12.0

- 转写收敛为单一通道：本地归一化为 mono/16 kHz/mp3 → 上传到百炼**免费临时存储**（48 小时自动清理）→ 单个异步 filetrans 任务整片转写。长音频不再切块，也没有块偏移需要拼接。
- 提交 filetrans 时对 `oss://` 临时 URL 自动附 `X-DashScope-OssResourceResolve: enable`（缺失会被判 `REQUEST_INVALID_FILE_URL_VALUE`）。
- 彻底移除 OkFile：相关常量、上传实现、错误码说明、`OKFILE_TOKEN` 凭据与 Skill 元数据声明全部删除；凭据只剩 `DASHSCOPE_API_KEY` 与 `ALIYUN_WORKSPACE_ID`。
- 移除内联同步分块实现（`asr_sync_chunked.py`）及其 29 项测试，模型白名单只保留 filetrans 家族。
- 新增 `ALIYUN_ASR_MP3_KBPS`（默认 64，允许 16–96）控制上传音频质量。
- 实跑验证：合成语音 → 临时存储上传 → filetrans → 25–30 词，端到端约 14 秒。

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
