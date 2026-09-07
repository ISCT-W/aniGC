# 本地任务工具的使用与边界

本地工具保存任务、完整输入、各轮图片、监修和验收，保护次数与版本，生成 Markdown 预览。Gemini API 执行入口现已实现，见 [制作后端说明](generation-backends.md)；只有显式 `generate --execute` 会发生成请求。普通存档、检查和恢复不联网，MCP 仍由 Codex 直接调用。

代码使用 Python 3.10+ 标准库，支持当前 macOS 和有 `fcntl` 的 Linux；尚未实现原生 Windows 文件锁。无需安装第三方依赖。在仓库根目录运行 `PYTHONPATH=src python3 -m anigc --help` 查看命令。下面的路径均为示意，占位内容需要换成实际文件；本文不会自动执行生成。

## 操作顺序

1. **建任务：** 用户明确启动生成后，保存原始请求和授权原话，再运行 `init`。默认任务模式为 `offline`，正式任务必须显式选择 `generation`，并在本地 `.env` 设置 `ANIGC_REFERENCE_PROJECT_ID`。离线任务使用独立的模拟项目，不读取真实项目配置。离线例子放临时目录，不写入正式 `generations/`。目录已存在则拒绝创建，恢复用 `recover`。
2. **准备：** 根据模板另写完整 prompt 和本次参考基线，使用 `prepare` 保存不可覆盖的本轮副本，按实际输入顺序反复给出 `--input`。`--parent` 记录从哪张旧候选继续，但不会自动把父图提交给后端；编辑时仍须把它列入 `--input base`。
3. **发送前：** Gemini API 使用 `--api-text-file` 冻结精确提交文字，通过 `check-request` 后执行 `generate --execute --evidence-ready`，命令内部预留额度再发送。下面的手工 `reserve` 只用于以后由 Codex 直接调用工具的路线，不应在 API 命令之前重复执行。完整 prompt 记录中的说明不是实际提交文字，程序不自动解析模板。
4. **返回后：** `finish` 保存全部返回图片，记录成功、已发送后失败、结果未知或明确未发送。结果未知先核实原请求，不自动重发。耗时、费用、返回的模型信息、可见 prompt 改写等放结果说明；没有的信息明确未知。
5. **监修：** Codex 查看真实图片和参考基线，写好报告，用 `review` 逐张登记结论、阻断问题和关键待核验项。首次报告为 `review.md`，后来登记生成 `review-v002.md` 等新版本，不改旧报告。报告可涵盖多个候选，但每个候选必须独立登记。程序不会从正文自动提取问题，也不检查“报告说通过”是否符合实际画面；登记字段必须与报告一致。
6. **交付：** `promote` 只复制无阻断、无关键待核验项的通过候选，并核对图片、参考输入和监修报告的内容校验值。用户尚未回复是待验收；回复后用 `accept` 绑定准确版本保存原话。普通评论和规则提炼由 Codex 按反馈说明写 `feedback.md`，不会自动启动生成。
7. **恢复：** `recover` 检查文件和未决请求、刷新预览；它不发送任何请求，也不重新计算或重置历史额度。有新一批明确生成授权时，`new-batch` 在同一个任务内保留历史并增开一批，不能绕过未决请求。暂停／停止时用 `status` 保存执行状态。

## 命令示例

所有长文本通过 Markdown 文件输入，避免在命令里拼接用户原话和密钥。示意任务名包含时区；工具还保存 UTC 时间和随机任务 ID。

```sh
PYTHONPATH=src python3 -m anigc init generations/20260907T150000+0900-example --title '任务名' --brief-file brief.md --mode generation --authorization-file authorization.md
PYTHONPATH=src python3 -m anigc prepare generations/20260907T150000+0900-example --prompt-file prompt-draft.md --reference-file reference-draft.md --backend gemini --model '<核实后的正式模型ID>' --input reference references/R01.png
PYTHONPATH=src python3 -m anigc reserve generations/20260907T150000+0900-example 001 --evidence-ready
```

**上例是手工记录工具调用的方式，reserve 自身不发送请求。** Gemini API 请改用制作后端说明中的准备／执行命令；`generate` 已自动保存返回结果。只有手工调用其他工具或核对旧请求的本地收录，才按下面的例子执行 `finish`：

```sh
PYTHONPATH=src python3 -m anigc finish generations/20260907T150000+0900-example 001 --status succeeded --output returned.png --note-file execution-note.md
PYTHONPATH=src python3 -m anigc review generations/20260907T150000+0900-example 001 output-01.png --report-file review-draft.md --verdict fail --blocker I001
PYTHONPATH=src python3 -m anigc recover generations/20260907T150000+0900-example
```

通过时登记 `--verdict pass`，不能附带未解决的 `--blocker` 或 `--unknown`。`--unknown` 只填写影响本次验收的关键待核验项；不适用或不影响验收的局限保留在报告中。

```sh
PYTHONPATH=src python3 -m anigc promote generations/20260907T150000+0900-example 002 output-01.png
PYTHONPATH=src python3 -m anigc accept generations/20260907T150000+0900-example final_output/final-v001-01.png --status changes_requested --comment-file comment.md
PYTHONPATH=src python3 -m anigc status generations/20260907T150000+0900-example paused --note-file pause-note.md
```

验收结论可用 `accepted` 或 `changes_requested`；`comment.md` 必须是用户对应版本的原话。未回复无需运行 `accept`。`preview` 与 `recover` 都更新任务 README；直接在 Codex 打开它即可浏览实际图片和报告。不存在上传或启动网页服务的步骤。

## 次数与中断的准确含义

| 状态 | 本批占一次额度 | 下一步 |
| --- | --- | --- |
| prepared | 否 | 核对资料与实际输入后 reserve |
| reserved | 保守占用 | 即将发送或发送情况尚未登记；恢复后先核对 |
| unknown | 是 | 查询原请求或人工核实，不能默认免费重试 |
| succeeded | 是 | 保存全部原图，继续实际监修 |
| failed | 是 | 已发送失败，若再发必须新增轮次并再计一次 |
| not_sent | 否 | 仅 reserved 可转入；须有明确发送前本地失败依据 |

默认最多 4 次，含首次生成。每轮对应一次请求；同样输入的重试也创建新轮次，保留全部失败记录。看图、检索、准备和查询原任务状态不占额度。后端改变不影响计数。一次只允许一个未决请求，防止并发重复提交；文件锁和原子状态写入不承诺远端请求只执行一次。

如果提交后超时，使用 `unknown`；后来确认取回成功或远端失败，可对原轮次再 `finish succeeded/failed`，不产生第二次调用计数。若服务端没有查询能力，保留阻塞，不能编造已失败来发新请求。`not_sent` 是调用者提供的事实，工具不能独立证明请求是否真的发送。

工具只约束经过这些入口的本地操作。现阶段无法拦截 Codex 绕过记录直接调用其他工具，也不能识别用户是否真的授权新任务或新批次；这些由 skill 和 AGENTS 约束。Gemini 适配器已将计数与真实发送接在一起，不保留隐藏重试；新后端接入时也须遵守。不得通过另建目录、复制状态或换后端重置同一批预算。

工具将正式任务的项目 ID 与本地 `ANIGC_REFERENCE_PROJECT_ID` 比较；配置变化不会重写旧任务，项目不匹配时拒绝继续。工具不会从自由格式 Markdown 或图像内容自动确认每份远端资料的归属；Codex 仍须在 参考资料 MCP 检索与读取时核实来源。`--evidence-ready` 记录 Codex 的核验结论，不是程序完成了角色事实核验。

## 文件与恢复

七类模板规定应记录的内容，不是七份都由程序自动填好：`init` 复制 reference、feedback 模板；brief、prompt、review 和后续 rule 由 Codex 按实际内容准备。acceptance 模板用于核对交付信息是否齐全，本工具生成的 `acceptance.md` 通过链接引用原报告和反馈，不能直接用空模板覆盖或手工补写它；详细依据和局限应保存在关联的 review／feedback 中。

`state.json` 是唯一的机器状态源；README、每轮 `execution.md`、验收汇总及 feedback 中明确划出的验收原话区域由它生成。原始 brief、完整 prompt、参考快照、图片和监修正文保存在 Markdown 与图片文件中，状态只引用它们及校验值。用户验收原话的小型副本随状态保存，以便发生中断时重建 Markdown。

- 原始 brief、已准备 prompt、每轮参考快照、输入副本、原图和已登记报告均不覆盖。修改草稿后重新 prepare；重审后登记新报告。`references/reference.md` 是当前可编辑的准备草稿，旧轮次读取自己的快照。
- 工具检查 PNG/JPEG/WebP/GIF 文件头与后缀，但不证明图片可解码或质量正确。实际看图仍是必需步骤。图片哈希检查用于发现文件变动，不是质量评分。
- 普通存档先写入完整文件再原子更新状态；API 响应额外先持久化收据（预期文件、哈希、完整性）再写文件，防止中断丢掉不完整标记。`recover` 会指出缺件或未登记文件，不能据此猜测成功。API 已暂存完整响应时用 `collect` 本地收录；手工产物或交付复制中断可用相同 `finish` / `promote` 收录字节一致的文件，不重新调用生成器。
- 状态写入已成功而预览更新失败时，先 `recover` 修复预览，再看状态决定动作。不要因为命令报错就再次提交生成。状态文件损坏不自动新建或把额度归零，需备份和原始执行依据核对。
- 已交付文件或依据被改动时，预览标出记录失效。用户否决的旧版本保留并明确标注需修改；没有自动删除、悄悄换图或自动再次生成。
- `feedback.md` 中 `anigc:acceptance` 标记之间的区域自动生成，其余区域供 Codex 保存普通评论、理解、规则关联与校准；不要在自动区域手工改写。反馈检索与索引重建程序是下一步，目前按 skill 人工维护索引。

## 验证

在根目录运行 `python3 -m unittest discover -s tests -v`。固定小 PNG 和所有测试任务均位于临时目录，完全离线。测试验证流程约束，不证明生成模型质量、真实角色设定符合程度或 参考资料 MCP 新接口可用。
