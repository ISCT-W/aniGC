# 图片制作后端与 Nano Banana 适配器

已实现 Gemini / Nano Banana 的 REST 适配器，并接入本地请求计数、输入冻结、产物收录与恢复。当前验证采用离线假响应，没有发送真实生成请求，密钥有效性与实际画面效果尚未验证。用户明确启动生图后，才使用本文的实际执行命令。

## 如何替换制作方式

共同保留的是参考基线、图片版本、逐轮监修、用户反馈与交付规则。制作入口按其实际方式接入：

| 后端标识 | 制作方式 | 当前状态 | 后续接入边界 |
| --- | --- | --- | --- |
| `gemini` | Google 图片生成／语义编辑 API | 已实现 | 通过同一个生成执行器计数和保存 |
| `gpt-image-2` | 以后选择内置图片工具或 OpenAI API | 仅声明入口，未实现 | API 可实现同一请求／结果契约；内置工具由 Codex 调用并接回轮次记录 |
| `clip-studio` | Codex 操作电脑绘画 | 仅声明入口，未实现 | 独立桌面执行流程，保存 `.clip` 工程和每轮导出图；普通画笔操作不算生图请求 |

运行 `PYTHONPATH=src python3 -m anigc backends` 可以查看状态。选择尚未实现的路线会明确返回错误，不会偷偷换成 Gemini。现在没有创建“返回成功”的假 GPT／Clip Studio 实现。

代码边界如下：

- `backends/types.py` 定义生成／编辑请求、按顺序的图片输入、返回图片和执行信息。这里没有 Google 的 `contents/parts` 字段。
- `backends/gemini.py` 负责 Gemini 能力校验、HTTP 请求构造和响应解析；不读配置、不写任务文件、不负责视觉监修。
- `backends/__init__.py` 负责显式选择后端和构造实例。增加 API 后端时实现 `validate`、`generate` 并登记工厂；公共执行器不用复制。
- `generation.py` 负责取回冻结输入、先计数再发送一次、保存返回数据。假后端的离线测试验证过替换后端后仍共享原任务预算。
- `task_store.py` 继续负责版本、状态、恢复与交付门槛。

Clip Studio 不需要伪装成一个生图 API。它未来可以在同一任务下保存原生工程与导出图，再进入共同的监修阶段；桌面绘画轮次和操作预算尚未实现，不能直接用本次 `generate` 命令控制 Clip Studio。

## 配置与模型

默认从仓库根 `.env` 读取 `GEMINI_API_KEY`，兼容 `GOOGLE_API_KEY`；进程环境变量优先。密钥仅在实际发送前加载到内存，放在 Google 请求头里，不写进任务、命令行参数、URL 或错误日志。配置解析不会执行 shell、展开变量或导入其他服务的配置。

生图模型选择顺序为：**准备轮次时的 `--model` → `GEMINI_IMAGE_MODEL` → `gemini-2.5-flash-image`**。它与现有通用 `GEMINI_MODEL` 分开；本次没有修改原 `.env`。可按 [.env.example](../.env.example) 配置独立图像模型。模型在准备轮次时固定，后来改变环境变量不会改变旧轮次的实际模型。

本实现核实并支持三个正式模型：原版 Nano Banana 的 `gemini-2.5-flash-image`、Nano Banana 2 的 `gemini-3.1-flash-image`、Nano Banana Pro 的 `gemini-3-pro-image`。不把昵称或旧 preview ID 自动换成另一个模型。[Google 官方说明](https://ai.google.dev/gemini-api/docs/generate-content/image-generation)

原版在本地保守限制为最多 3 张输入，只设置比例；Pro、3.1 Flash 最多 14 张，并可设置各自支持的尺寸档位。PNG、JPEG、WebP 是本实现支持的格式。请求上限按包含 base64 与文字的完整 JSON 字节检查，超出 20 MB 时在发送前拒绝。[官方图片输入说明](https://ai.google.dev/gemini-api/docs/generate-content/image-understanding)

编辑是传入一张标记为 `base` 的底图、可选参考图和完整修改文字。当前没有接入专门的遮罩字段；`mask` 会在本地被拒绝，不能把一张遮罩图片宣称成精确局部编辑。每轮发送独立的用户输入，不回放模型对话，不保存模型 thought 内容或假装延续其内部上下文。

## 准备与离线检查

实际任务的建目录方式仍见 [本地工具说明](local-tools.md)。准备 API 轮次时，区分**人类可读的完整输入记录**与**精确发送给模型的文字**：

```sh
PYTHONPATH=src python3 -m anigc prepare generations/<任务目录> \
  --backend gemini \
  --model gemini-2.5-flash-image \
  --prompt-file prompt-record.md \
  --api-text-file actual-text.md \
  --reference-file reference-baseline.md \
  --input reference references/R01.png \
  --aspect-ratio 3:4

PYTHONPATH=src python3 -m anigc check-request generations/<任务目录> 001
```

以上路径为示意。`actual-text.md` 中的全部文字会原样提交，不自动剥离标题或模板。`prompt-record.md` 是 prompt 模板填写后的解释与设置记录；适配器不会把这些说明一起发给模型。如果你的 prompt 本来就是纯提交文字，两项可以指向同一个文件。必须由 Codex 核对两份记录与实际设置是否一致；程序不理解自由格式正文。

准备时不读 key、不联网、不占次数；可读取独立的非敏感图像模型配置。图片按 `--input` 顺序复制进任务，参考图和底图的用途应在实际文字中明确说明。每轮另存 `submission.md` 和 `settings.md`，冻结确切文字、模型、参数、输入顺序及内容校验值；不能修改旧轮次后继续沿用原请求或监修。

修改已有候选时使用 `--operation edit --input base <底图>`，可重复 `--input reference <参考图>`，并用 `--parent rounds/001/output-01.png` 记录来源。`--parent` 只是溯源，不能代替实际传入底图。设置 `--image-size` 前确认选用模型支持该档位；原版应省略它。

## 明确授权后的单次发送

```sh
PYTHONPATH=src python3 -m anigc generate generations/<任务目录> 001 \
  --execute --evidence-ready
```

必须是 `generation` 模式的任务、尚未预留的轮次、有生成授权原话和足够证据。**API 路线不要提前手工 `reserve`**：`generate` 已包含验证、预留和发送；已预留／已发送的轮次不会再次发送。参数错误、缺少 key、离线任务或未给执行标志时不会调用后端；发送后的失败照样占次数。

当前使用固定 Google `v1beta/models/{model}:generateContent` 同步接口，单次 HTTP POST，不启用 SDK 的额外重试，也不跟随重定向或切换后端。若之后要重试，应根据监修／故障结论准备新的轮次，重新计一次请求。[API 定义](https://ai.google.dev/api/generate-content)

返回时只保存实际最终图片与可见返回文字；内部 thought 图片／文字和签名不会作为最终结果。即便请求设置一个候选，也遍历并保存实际返回的所有可读图片。图片文件头匹配不代表视觉正确，后续仍必须逐张监修。

`response.md` 记录请求模型、实际返回模型（若提供）、本地耗时、可获得的用量、终止原因及检查事项；不落盘原始 JSON、base64 或密钥，未核实价格不推算费用。异常原文可能含敏感内容，因此只记录经过限制的错误类型与 HTTP 状态。

## 失败、中断与恢复

- **明确 HTTP 拒绝或未返回图片：** 记录已发送失败，占一次额度，没有自动重试。
- **超时、断连、服务端 5xx 或无法解析响应：** 保守记录 `unknown`，占用额度并阻止继续发送。同步 generateContent 本次未核实到按请求 ID 查询结果的接口，不能声称可以自动取回丢失响应；先保存现状再处理。
- **返回部分可读图片、还有不能解析的产物：** 保存可读部分与不完整标记；该轮候选可检查，但不能进入 final_output。可以在已有授权和剩余额度内准备后续轮次，不能把不完整当成全部成功。
- **收图时本地中断：** 先把预期文件哈希和完整性记入现有 state，再保存图片与响应说明；恢复时核对缺件。若所有响应文件已暂存，可运行 `collect` 完成本地收录，不产生新请求。若缺件，则保留未决状态和已存文件，不能直接重新生图。

```sh
PYTHONPATH=src python3 -m anigc recover generations/<任务目录>
PYTHONPATH=src python3 -m anigc collect generations/<任务目录> 001
```

`collect` 不读 key、不调用 API。状态成功但预览更新中断时，`recover` 重建展示。用户停止后保留文件与请求记录，不借更换模型、新目录或恢复进程清零预算。

监修通过后的 `review`、`promote`、`accept` 与之前相同。制作后端返回了图片不等于 Agent 已通过，更不等于用户已验收。
