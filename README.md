# AI短剧/漫剧本地工作流

现在可以不依赖即梦会员完成：分镜与提示词导出 → 图片/视频/音轨导入 → 临时旁白 → 静态预演或多镜头粗剪 → 字幕与内部审阅包。剧本和分镜由创作者/Agent设计，程序负责检查、执行和记录。

- `production.py`：本地制作，不登录平台、不调用生成服务。
- `workflow.py`：即梦CLI的保存、报价、生成、续查下载及选用；支持按镜头表串行执行。
- Python 3.9+，不需要额外Python包。媒体功能需要 `ffmpeg` / `ffprobe`；macOS临时旁白使用系统 `say`。
- 2026-10-15：24项测试通过，包括真实FFmpeg媒体测试；咖啡首帧已完成6秒、720×1280、含中文临时旁白的静态预演。即梦付费生成成功路径仍未联调。

| 文件 | 用途 |
|---|---|
| [主需求 v0.7](ai-short-drama-workflow.md) | 创作、分镜、资产及渠道要求 |
| [开发任务](docs/development-tasks.md) | 已完成的本地功能与平台待验证项 |
| [执行规格](docs/workflow-execution-spec.md) | 输入、媒体、恢复和交付约定 |
| [环境检查](docs/environment-check.md) | 工具版本与限制 |
| [咖啡完整分镜示例](examples/coffee-morning/shots.json) | 包含景别、动作、运镜、接镜和临时旁白 |
| [已生成咖啡预演](runs/coffee-local/exports/preview-105850e3cd/video.mp4) | 静态画面＋临时旁白，尚无AI动态视频 |

## 先跑本地咖啡示例

在项目根目录执行。使用一个新的运行目录，保留已有产物：

```bash
mkdir -p runs/my-coffee
cp examples/coffee-morning/project.json runs/my-coffee/project.json
cp examples/coffee-morning/shots.json runs/my-coffee/shots.json
python3 production.py --project-dir runs/my-coffee import --kind image --file assets/coffee-morning/first-frame-v1.png --source "Codex咖啡首帧"
python3 production.py --project-dir runs/my-coffee narrate
python3 production.py --project-dir runs/my-coffee storyboard
python3 production.py --project-dir runs/my-coffee render --mode preview
```

每次导出都会返回一个新的目录，不覆盖旧版。`example_only=true`只阻止平台保存和生成，不阻止本地制作。

`preview`是静态分镜预演，用来验证顺序、时长和音轨。默认输出720×1280、24fps；可用 `--size 1280x720 --fps 24` 改为横屏。保持图片比例并补边，不自动裁切。临时旁白使用系统已安装的Tingting中文音色；这是预演语音，正式表演音轨可以另行导入。

## 分镜设计与镜头顺序

Agent先按主需求第6节拆解故事、设计镜头，再填写 `shots.json`。`storyboard`检查必要字段并导出 `storyboard.md/csv`、`prompts.md`、逐镜头 `subtitles.srt` 和输入快照。它不调用大模型，不自动判断故事是否好看或接镜是否合理。

每镜头使用 `shot_id` / `revision`，填写 `narrative_goal`、`framing`、`action`、`camera`、`image_prompt`、`video_request.prompt`、`planned_edit_seconds`、`acceptance`。可补充 `continuity_in/out`、`audio.text/file/voice`、`subtitle`、`edit_in_seconds`。

`edit_order`是有序的 `shot_id@revision` 列表，每个镜头只列一个版本。没有该字段时沿用shots列表顺序，但不允许同时拼入同一镜头的多个候选。完整例子见咖啡示例。

## 网页生成的视频也能进入流程

从即梦、ComfyUI或其他工具下载文件后导入：

```bash
python3 production.py --project-dir runs/my-coffee import --kind video --shot E01-SC01-SH001 --revision v01 --file /absolute/path/clip.mp4 --source "即梦网页手动生成"
python3 production.py --project-dir runs/my-coffee select --shot E01-SC01-SH001 --revision v01 --note "已播放检查并选用"
python3 production.py --project-dir runs/my-coffee render --mode rough-cut
```

- 导入时实际探测并完整解码，复制进项目，保存来源、校验值和版本，不编造平台资源ID。手动生成费用标为unknown。
- 图片导入替换该镜头的首帧槽位，保留其余参考；已绑定生成操作的版本需先新建版本。
- 视频先导入再显式选用；新候选不会自动替换选用。所有需要视频的镜头必须选齐，缺失时停止。
- 粗剪按 `edit_order` 拼接，默认从视频第0秒取 `planned_edit_seconds`，可用 `edit_in_seconds` 改起点；不足长度会报错。
- 用 `import --kind audio --file ... --source ...` 导入正式音轨。有独立音轨时替换该镜头的原视频声音，没有时保留原声音；无声音的镜头补静音。旁白太长会报错，不静默截断。
- 可加 `render ... --music /absolute/path/music.wav --music-volume 0.15`，背景音乐循环铺满，保留已有音轨。
- 字幕是逐镜头时间段的SRT侧车文件，不自动逐字对齐、不烧录进画面；可导入剪映精调。
- 明确采用静帧的镜头设置 `render_mode: "still"`，粗剪时可与动态镜头混合。静帧保持固定画面。

## 返工与交付

```bash
python3 production.py --project-dir runs/my-coffee revise --shot E01-SC01-SH001 --revision v01 --to v02
python3 production.py --project-dir runs/my-coffee package --render-dir /absolute/path/to/exports/preview-xxxx
python3 production.py inspect --file /absolute/path/video.mp4
python3 production.py audit --kind comic /absolute/path/episode01.mp4 /absolute/path/episode02.mp4
```

`revise`保留原版本与原选用。Agent修改新版本后，明确更新 `edit_order`；视频仍须选用新版本。

`package`导出已验证的MP4、SRT、分镜、提示词、来源与校验清单。所有包均标为内部审阅包；技术检查不等于内容审核通过。已验证视频或字幕、分镜被改动时停止打包。

`audit`按用户提供的手册检查**实际文件**：重复文件、文件大小、逐集时长；漫剧首次上传/整剧累计至少1200秒、单集至少30秒。默认制作金额低于30万元，单集不超过600秒；`--high-budget`为30万元及以上，单集须短于1200秒。真人短剧使用 `--kind live`，每集不超过180秒，不套用漫剧20分钟门槛。问题返回退出码2。3GB按保守的3,000,000,000字节检查；正式发布前仍需核对当时的平台规则和材料。

## 即梦CLI：会员问题解决后使用

真实项目需填写 `canvas_project_id`、实际支持的model/duration/ratio/resolution，关闭示例标记，并登记已经获得的生成授权和积分上限。

```bash
python3 workflow.py --project-dir runs/my-coffee status
python3 workflow.py --project-dir runs/my-coffee save --all
python3 workflow.py --project-dir runs/my-coffee quote --all
python3 workflow.py --project-dir runs/my-coffee run --all
python3 workflow.py --project-dir runs/my-coffee resume --all
```

`--all`按edit_order逐镜串行执行，发生错误就停止。run/quote跳过已提交或受理不确定的版本；resume只查询原任务并下载，不重新生成。去掉 `--all`，用 `--shot ... --revision ...` 指定单个镜头。下载后仍需 `select` 选用，可用production.py统一执行媒体检查与选用。

授权沿用项目的 `approved`、`credit_cap`、`scope` 和 `approval_ref`；未知费用不视为零。报价变化超上限时不提交。原submitId与预算占用在提交前写盘，临时确认token不落盘。输入变化用新版本，下载失败只重下载。

已安装CLI位于 `.tools/bin/dreamina-canvas`，认证使用系统存储；业务恢复记录位于项目 `.cli-state`。普通沙盒可能无法读取系统登录，需同用户可读取凭据的进程；不强制重新登录、不导出凭据。

## 可灵 CLI：Kling 4.0 图生视频与全能参考

可灵 CLI 已装在 `.tools/kling`（见 [.tools/kling/README.md](.tools/kling/README.md)）。项目 `project.json` 写 `"generator": "kling"` 后，`workflow.py` 的 save／quote／run／resume／select 改走可灵，另有 `element` 命令管理主体。

```json
"generation_authorization": {"approved": true, "credit_cap": "300", "scope": ["E01-SC01-SH003@v02"],
  "approval_ref": "用户在对话中同意的记录",
  "kling_credit_rate": {"image_to_video:kling-video-v4_0@720p": "按价格页填写的每秒灵感值",
                        "omni_ref_video:kling-video-v4_0@720p": "按价格页填写的每秒灵感值"}}
```

两种镜头写法：

- **关键帧图生视频**：`{"mode": "kling_i2v", "model": "kling-video-v4_0", "prompt": "...", "duration": 5, "resolution": "720p"}`。`references` 每项是本地文件加 `slot`：`first_image` 必填一张，`tail_image` 与 `keyframe_1`…`keyframe_10` 可选，合计最多10张；画幅跟随首帧。
- **全能参考**：`{"mode": "kling_omni", "model": "kling-video-v4_0", "prompt": "单一连续镜头。<<<xiangchongchong>>>按图片1……", "duration": 5, "resolution": "720p", "aspect_ratio": "9:16", "enable_audio": false, "elements": ["xiangchongchong"]}`。`references` 的 `slot` 用 `image_N`、`video_N`、`audio_N`；提示词用 `图片1`、`视频1` 指代素材，用 `<<<元素键>>>` 指代主体，提交时自动换成主体编号。按秒计费，**参考视频的时长计入计费**；该模型的智能分镜（`prefer_multi_shots`）不能关闭，单镜头请在提示词里写明“单一连续镜头”，验收时检查是否被切成多镜。我们另配对白，建议 `enable_audio: false`。

主体（角色、道具）写在项目的 `elements.json`：

```json
{"schema_version": "0.2", "elements": [{"key": "xiangchongchong", "name": "香虫虫", "description": "……",
  "tags": ["角色"], "cover": "正面图.png", "secondary": ["侧面图.png"]}]}
```

`python3 workflow.py --project-dir 项目 element --key xiangchongchong`（或 `--all`）创建主体：先写盘，再查主体列表；远程名称带输入内容的8位校验码（如“香虫虫 #1a2b3c4d”），上次结果不确定时按这个名称找回、不会重复创建。主体图片改了就不能再绑定，需要换新的 key。标签只能取：角色、动物、道具、服饰、场景、特效、其他。

提交流程与保护：

- `save`：只在本地冻结输入和文件校验值，不联网；检查图片为 PNG/JPG、≤30MB、不超过4K、宽高比在2:5到5:2之间，提示词不超过8000字，全能参考的主体占位与 `elements` 一致。
- `quote`：按 `kling_credit_rate[工具:模型@分辨率] ×（输出时长＋参考视频时长）` 向上取整估算，并查询账户余额。可灵没有报价接口，估算以价格页为准。
- `run`：先在本地检查授权、`example_only`、主体是否已创建且未改动、预算上限（已提交和受理不确定的任务都按预留额计入）；然后读取实时 `who_am_i`，核对模型、参数取值（如1080p是否已开放）、输入槽位、主体数量与类型、素材合计上限；再查余额。全部通过后，先把 `submitting` 状态和本地编号写盘再提交；拿到 `generationId` 立即保存，再解析 `creditsConsumed`，解析失败时记 `credit_status: needs_check` 并保留预留额度。
- 提交没有拿到 `generationId`（超时、报错）时记为 `needs_check`，之后 run 和 `--all` 都不会再提交这一版本。可灵没有客户端防重编号，请先到可灵 App 的作品记录里核对。确认确实没有提交（作品记录没有、灵感值未变、CLI 报“no generation was submitted”）后，可用 `workflow.py reconcile --shot … --revision … --not-submitted --evidence "核对了什么"` 把它恢复为 `saved` 再提交；核对记录保存在 `reconciliations` 中。已有 `generationId` 的记录不能这样恢复。
- 提示词作为独立参数传给 CLI（`--prompt` 后跟内容），因为 kling-cli 0.2.3 的 `--prompt=…` 不能识别多行内容；提示词不能以 `--` 开头。CLI 本地报错时，`workflow.py` 会带上 CLI 的错误输出（已去掉链接）。
- `resume`：只按原 `generationId` 查询并下载无水印地址到 `outputs/`，不重新提交；签名下载地址不写进状态文件。下载长度与服务端声明的 `Content-Length` 不符时不记为完成，下次 `resume` 重新下载到新文件名；服务端没声明长度时记为 `length_check: not_declared`，选用前用 `production.py` 完整解码检查。
- 尚未做真实提交联调：当前账号非会员、命令行可用灵感值为0。

## 即梦 dreamina CLI：Seedance 首尾帧图生视频

官方通用 CLI `dreamina` 1.4.21 装在 `.tools/dreamina/`（见 [.tools/dreamina/README.md](.tools/dreamina/README.md)）。它和上面的画布 CLI 不是同一个程序；**仅限即梦会员使用**，Seedance 2.5 标注为 VIP-only。项目 `project.json` 写 `"generator": "jimeng"` 后，save／quote／run／resume／select／reconcile 改走这个 CLI。

```json
"generation_authorization": {"approved": true, "credit_cap": "260", "scope": ["E01-SC01-SH006@v01"],
  "approval_ref": "用户在对话中同意的原文",
  "jimeng_credit_rate": {"seedance2.5@720p": "按网页或首测实际填写的每秒积分"}}
```

- 镜头写法：`{"mode": "jimeng_frames", "model_version": "seedance2.5", "duration": 4, "video_resolution": "720p", "prompt": "..."}`；`references` 填两项，`slot` 分别为 `first_frame`、`last_frame`。只有首帧时用 `jimeng_i2v`，只填 `first_frame`。
- 本地检查（按 1.4.21 的 `-h` 编写）：seedance2.5 时长 4—30 秒，分辨率 480p／720p／1080p，**不能写 `ratio`**（画幅跟随首帧）；seedance2.0_vip 时长 4—15 秒，分辨率 720p／1080p／4k，可以写 `ratio`；其他 2.0 型号只能 720p。
- CLI 没有报价命令：`quote`／`run` 按 `jimeng_credit_rate[型号@分辨率] × 时长` 向上取整估算，并用 `user_credit` 查余额。`run` 先检查授权与预算上限，再把 `submitting` 写盘、提交；拿到 `submit_id` 立即保存，并记录提交前后的余额。CLI 不返回实际扣费，所以 `credit_status` 记为 `needs_check`，预留额一直计入预算上限。
- 提交没有拿到 `submit_id` 时记为 `needs_check`，之后不会再提交这一版本。核对 `dreamina list_task` 和网页作品记录、确认确实没有提交后，才能用 `reconcile --not-submitted --evidence ...` 恢复为 `saved`。
- `resume` 只按原 `submit_id` 调用 `query_result --download_dir`，每次下载到新的空目录，不覆盖旧文件；`gen_status=fail` 时记下 `fail_reason`，不会重新生成。下载后用 `production.py` 完整解码检查，再 `select`。
- 尚未做真实提交联调：生成命令的返回格式按官方 SKILL 文档（`submit_id`、`gen_status`、`fail_reason`）编写，首次真实提交后需核对。

## 验证

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_workflow test_production
```

2026-10-24：50项测试，49项通过，1项因本机 FFmpeg 缺少 libass 跳过（字幕烧录）。其中17项为可灵接入测试，8项为即梦 dreamina CLI 接入测试，全部使用固定返回，不调用真实服务。

媒体集成测试需要FFmpeg/ffprobe，缺少时会明确跳过。测试包括真实混合尺寸/帧率/有声与无声视频合成、实际画面顺序、字幕时间、媒体被修改、短片/长旁白阻断、打包校验，以及CLI预算和防重复提交。平台返回样例测试不等于即梦真实联调通过。
