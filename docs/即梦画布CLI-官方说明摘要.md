# 即梦画布 CLI 官方说明摘要

来源：[即梦画布 CLI 使用说明](https://bytedance.larkoffice.com/wiki/QO66wGahSiakEHkJbxIcNBtDnAc)（飞书文档，页面显示“10月23日修改”）。2026-10-25 以访客身份阅读并摘录。命令和参数以 CLI 实时返回的 `schema`、`--help` 为准。

## 版本记录（文档顶部）

- v1.0.3（2026-10-14）：支持 Seed Audio 1.0；全能配音支持选择音色或参考素材；支持删除节点、调整节点位置。
- v1.1.0（2026-10-21）：新增团队空间支持。
- v1.1.1（2026-10-23）：上线样片模式，支持生成样片、样片转正片。

## 与 `dreamina` 通用 CLI 的关系

- 画布 CLI 的命令名是 `dreamina-canvas`，对应即梦网页上的“画布”。文档写明：旧版 `dreamina text2image`、`dreamina image2video` 等命令**不适用于**画布 CLI。
- 本项目目前用的 `.tools/dreamina/dreamina`（1.4.21）是通用 CLI，不是画布 CLI。

## 安装与登录

- 安装：`curl -fsSL https://jimeng.jianying.com/canvas-cli/install.sh | bash`。默认装到 `~/.local/bin/dreamina-canvas`，Skill 装到 `~/.agents/skills/dreamina-canvas-cli/`。安装脚本支持用 `DREAMINA_CANVAS_INSTALL_DIR`、`DREAMINA_CANVAS_SKILL_DIR` 指定目录。
- 验证：`dreamina-canvas --help`，应能看到 auth、canvas、model、node、operation、resource 等命令。
- 登录：`dreamina-canvas auth login`；自检：`dreamina-canvas auth account`。
- 本地状态：`~/Library/Application Support/dreamina-canvas/` 下的 `contexts/`（当前画布）和 `operations/<submitId>.json`（任务恢复信息）。不要手工修改。

## 命令一览

| 任务 | 命令 | 关键参数 |
|---|---|---|
| 模型与画布 | `model list --type image\|video\|audio`、`model find`、`canvas create --use`、`canvas ls` | `--model` 必须填 `model list` 返回的 model 值，别名只用于查找 |
| 素材 | `resource upload --file --name`、`resource get`、`resource download <id> --project-id --output` | — |
| 图片 | `node create image` | 模式 `t2i`、`i2i`；`--ratio --resolution --count --ref node:<id> --run --wait` |
| 视频 | `node create video` | 模式 `t2v`、`first_last_frame`、`m2v`，**没有 i2v**；多模态参考（图片、视频、音频、主体）用 `m2v`；`--duration --ref` |
| 音频 | `voice list --offset --count`、`node create audio` | `--mode tts --voice-name <voiceName>`（配音）；`--mode music --model --duration`（音乐） |
| 其他 | `node upscale image`、`node create text`、`node create element`、`node create timeline` | 主体：`--title --description --main --auxiliary --voice`（**主体可以绑定音色**）；时间轴：`--clip --audio-clip` |
| 查询与修改 | `node find/show/edit`、`operation status/wait <submitId>` | 修改生成设置时必须重新提供完整参数和全部引用，否则原引用会被清除 |

## 积分与任务

- `node create/edit` 只保存节点，不生成；加 `--run` 才会报价、确认、提交。
- `node quote` 查询服务端的预计积分（报价不等于批准），`node run` 运行已保存的节点。
- Agent 或脚本环境不能弹出交互确认：要先把报价给用户看，用户确认后再继续。设置积分上限时，最新报价超过上限，CLI 会在运行前停止。
- 退出码 10 表示“等待确认”，不是失败：节点已保存，生成还没提交，应复用返回的节点 ID 和任务 ID 继续。
- 等待超时不代表失败，也不会取消服务端任务；继续用原 submitId 查询，不要重复提交。换一个新的任务 ID 可能被当作新的生成，会再次扣积分。

## 对本剧的意义

1. 配音：画布 CLI 有 TTS（`voice list` 加 `--mode tts`）和全能配音（Seed Audio 1.0，可以选音色或参考素材）；通用 CLI 没有这些。
2. 角色音色统一：`node create element --voice` 可以把音色绑定到角色主体上，可能用于让同一角色在各镜中保持同一种声音（待实测）。
3. 省钱：Seedance 2.5 样片模式可以先出低成本的样片，选中后再转正片；配合 `node quote` 可以在提交前看到报价。
4. 只有首帧的镜头在画布 CLI 里要用 `m2v`（以图片作参考），首尾帧用 `first_last_frame`。
