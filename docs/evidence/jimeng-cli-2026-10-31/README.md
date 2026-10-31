# 即梦 CLI 1.2.0 核验证据

日期：2026-10-31。对应[逐项核验报告](../../即梦网页功能与CLI逐项核验-2026-10-31.md)。

本次完成只读查询、12 组本地参数检查、2 组预期拒绝、既有素材下载及导演台本地渲染。没有执行生成提交、后编辑提交、云端节点保存或导演台 push。

## 纳入仓库的通用证据

- `version.json`、`schema*.json`、`command-index.json`：版本与命令、参数契约。
- `help-*.txt`：相关命令帮助。
- `models-*.json`、`voice-sample.json`：查询当时的模型与音色首项。
- `director-catalog-*.json`：导演台内置命令与素材首项；非必要 URL 已省略。
- `account-capability-summary.json`：账户查询结果的字段路径，不含账户展示名、账号 ID 或具体余额。
- `numeric-project-id-check.json`、`text-reading-reference-check.json`：两个预期拒绝的错误回执。
- `director-local-test-commands.json`、`director-local-test.mp4`、`director-video-frame-*.png`：不含作品剧情的方块运动与相机推进测试，以及实际视频起、中、末抽帧。
- `media-verification.json`：格式、时长、文件大小、SHA-256 与完整解码结果，不含项目媒体正文。
- `cloud-unchanged-check.json`：本地试验前后云端导演台版本与正文一致性检查。
- `verification-summary.json`：对应记录分类统计与最终核验结果。

## 仅留本地的项目原始记录

画布列表、节点查找和读回可能包含香虫虫的人物、台词、镜头描述或提示词；这些记录不提交。下载的图片、音频、视频，以及带实际项目绑定的运行回执和导演台工作副本，也保留本地。对应文件由项目 `.gitignore` 排除，不删除原始素材。

因此，从仓库检出后可以查看通用能力证据与测试视频；带作品内容的原始记录不会随仓库分发。本地原始记录不是后续写入、扣费或发布授权，测试工作副本也不应直接发布到原作品。
