# video-translate 维护规则

开始工作前读取 `README.md`、`registry.json` 和 `skills/video-translate/SKILL.md`。

- 本仓库只维护 `aiaaaa4.video-translate`，不得加入其他 Skill、App 或 Flow。
- 本仓库的维护对话全名是 `S1 video-translate`，内部 ID `S1` 由根 `registry.json` 分配。
- 保留通用上游素材交接能力，但不得反向依赖具体 Flow 名称或路径。
- 版本只在 `registry.json` 维护，并同步 README 展示。
- 提交前运行仓库验证和完整测试。
- 新 GitHub `origin` 配置完成前不得执行正式发布。
