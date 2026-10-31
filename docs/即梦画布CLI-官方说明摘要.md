# 即梦画布 CLI 使用说明摘要

更新：2026-10-31。当前核验版本：`dreamina-canvas 1.2.0`，public / cn。

本文件维护当前操作要点。[网页与 CLI 逐项核验](即梦网页功能与CLI逐项核验-2026-10-31.md)维护功能覆盖、实测结果与证据边界；[画布与导演台指南摘要](即梦短剧工作室-画布与导演台指南摘要.md)介绍网页制作方式。后续升级后重新核对，不累积已被替代的操作说明。

依据：[官方 CLI 说明](https://bytedance.larkoffice.com/wiki/QO66wGahSiakEHkJbxIcNBtDnAc)的既有阅读记录，以及本轮读取的安装 Skill、实时 `schema`、帮助和模型目录。可执行命令以已安装版本为准。

## 工具与能力发现

- 本机二进制：`.tools/bin/dreamina-canvas`。通用 `dreamina` 是另一套工具，本项目当前即梦生成使用画布 CLI。
- 先查询 `--version`、`schema`；导演台子命令查 `schema director`。
- `auth status` 查看本地认证状态；`auth account` 查询服务端账号、会员、积分余额与到期明细。
- 按媒体类型执行 `model list --type image|video|audio`；按展示名查找用 `model find`。创建节点使用返回的实际 model 值。
- `voice list` 查询音色目录，按返回的模型和分页信息选择，不编造音色名或 ID。
- CLI 画布使用 UUID 项目标识。短剧工作室数字 ID 项目与 CLI 的映射尚未确认，不直接代入。

## 常用操作

| 任务 | 当前命令与参数 |
|---|---|
| 画布 | `canvas create/ls/use/current`；使用返回的实际项目 ID 与 webUrl |
| 上传、查询、下载 | `resource upload/get/download`；支持图片、视频、音频，下载后三种媒体均已验证可读 |
| 文本 | `node create/edit text --text`；保存文本不等于运行网页文本 AI |
| 图片 | `node create/edit image --mode t2i|i2i`，结合 `--prompt --model --ratio --resolution --count --ref` |
| 视频 | `node create/edit video --mode t2v|first_last_frame|m2v`；单首帧和双帧均可使用 `first_last_frame`，混合参考使用兼容的 `m2v` 模型 |
| 朗读配音 | `node create/edit audio --mode tts`；可以按对应入口仅选音色，也可以显式指定模型及 `text-reading` 场景 |
| 全能配音 | `--mode tts --model seed_audio_v1 --scenario omni-dubbing`，按目录限制使用音色及参考素材 |
| 音乐 | `--mode music --model <目录返回值> --duration <允许时长>` |
| 通用主体 | `node create/edit element --main --auxiliary --voice`；不等于完整短剧角色档案管理 |
| 基础时间轴 | `node create/edit timeline --clip --audio-clip`；不覆盖网页完整多轨编辑器 |
| 节点查询 | `node find/show`；本版没有 `node list` |
| 报价与运行 | `node quote/confirm/run`；保存节点与提交生成分开 |
| 恢复任务 | `operation status/wait <原 submitId>` |
| 后编辑 | `node upscale image/video`、`node cutout image`、`node interpolate video` |
| 样片转正 | `node promote video-sample`；转正规格、费用与有效期另行核对 |

具体比例、时长、分辨率、数量及素材组合，取当前模型目录与调用契约的交集。目录有某项规格，不代表所有模式都可传该参数。例如，当前 `text-reading` 不接受 `--ref`，全能配音才允许对应参考。

## 引用与输入

- 画布中已有来源节点时，优先使用 `--ref node:<node-id>`；需要冻结具体素材或无来源节点时使用资源引用。
- 引用主体主图、声音及辅助素材，分别使用对应的绑定参数；生成前确保素材已可用。
- 修改生成配置时提供完整有效参数和全部引用，避免遗漏引用或无意改变生成方式；元数据修改按当前契约单独处理。
- 运行前核对回读的来源、顺序及节点连线，不能只看请求成功。
- `--dry-run` 只做本地格式检查；不验证素材资格、服务端模型准入、余额或生成结果。

## 导演台

- `director catalog --kind builtin|character|motion` 发现类型、素材与参数。
- `director create/show/pull/apply/push` 管理场景；带 `--file` 的本地工作副本修改，只有再次 push 才发布回画布。
- 对象、姿势、相机、关键帧、分镜使用目录中实际列出的类型化命令。
- `director preview` 预览；`director export --type image|video` 导出截图或动态参考。当前机器已成功导出并检查 PNG 与 H.264 MP4。
- `--to-canvas` 把导出媒体回传画布，本轮只核对接口，未执行回传。它与保存可编辑导演台是不同操作。
- 动态参考须与角色外观、场景及文本共同使用；导演台视频导出不等于通用素材时间轴成片导出。

## 时间轴边界

- `--clip` 表达画面序列；图片可以设时长，视频可以设截取、速度与音量。
- `--audio-clip` 表达音频序列，支持起点、截取、速度及音量。
- 当前配套协议要求片段来源可用、单次总 clip 不超过 20、音频片段不重叠、截取不超出真实素材长度。
- 未找到字幕轨、字幕样式、任意多音轨混音及通用时间轴成片导出的明确 CLI 入口。
- 需要这些功能时，由用户完成网页操作，或使用已验证的本地后期与剪映流程，再核对结果继续。

## 积分与恢复

- 常规节点 create/edit 不带 `--run` 时只保存；运行前查询实际报价，按已有授权范围和积分上限提交。
- 后编辑命令通常直接提交，不依赖 `--run`；只有明确声明无副作用的 `--dry-run` 才是本地检查。
- 报价不等于扣费批准；服务端报价超过授权范围时不提交。
- 超时不等于失败；保留原 submitId 或 operationRef，恢复原操作，不重复提交。下载失败优先重新下载。
- 样片、转正、超清是不同费用项；样片低价不等于最终镜头成本。
- 任务受理、终态、素材可用与实际文件验证分别记录。

## 当前验证结果

已验证只读画布／节点访问、实时目录查询、12 组本地参数检查、三种媒体下载，以及导演台本地编辑与截图／MP4 导出。通用时间轴写入与成片导出、各专用生成效果不作为本轮已验证能力。完整覆盖表和具体证据见逐项核验报告。
