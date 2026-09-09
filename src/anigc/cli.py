"""Local task records plus explicit, budgeted backend execution."""

import argparse
from pathlib import Path
import sys

from .task_store import DEFAULT_REQUEST_LIMIT, StoreError, TaskStore, raster
from .backends import BACKENDS, make_backend, resolve_model
from .backends.types import BackendError, ImageInput, ImageRequest
from .generation import MIME_BY_SUFFIX, check_request, generate_round
from .config import DOTENV


def read_md(path):
    p = Path(path)
    if p.suffix.lower() != ".md":
        raise StoreError("文字输入使用 Markdown 文件；不要传入 .env 或凭据")
    return p.read_text(encoding="utf-8")


def parser():
    p = argparse.ArgumentParser(description="aniGC 图片工作流；只有 generate --execute 会调用生成 API")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="建立一个新任务；已有任务使用 recover")
    init.add_argument("task")
    init.add_argument("--title", required=True)
    init.add_argument("--brief-file", required=True)
    init.add_argument("--mode", choices=["offline", "generation"], default="offline")
    init.add_argument("--authorization-file")
    init.add_argument("--limit", type=int, default=DEFAULT_REQUEST_LIMIT, help="本批请求上限，默认 6；不会修改已有任务")
    prep = sub.add_parser("prepare", help="保存完整输入和本轮参考快照；不占次数")
    prep.add_argument("task")
    prep.add_argument("--prompt-file", required=True)
    prep.add_argument("--reference-file", required=True)
    prep.add_argument("--backend", required=True)
    prep.add_argument("--model")
    prep.add_argument("--api-text-file", "--submission-file", dest="api_text_file", help="精确实际提交文字；与包含说明的 prompt 记录分开")
    prep.add_argument("--operation", choices=["generate", "edit"], default="generate")
    prep.add_argument("--aspect-ratio")
    prep.add_argument("--image-size")
    prep.add_argument("--pixel-size", help="GPT 像素尺寸，如 1024x1024；默认 auto")
    prep.add_argument("--quality", help="GPT 质量，默认 auto")
    prep.add_argument("--env-file", default=str(DOTENV))
    prep.add_argument("--input", nargs=2, action="append", metavar=("ROLE", "PATH"), default=[])
    prep.add_argument("--parent", help="本任务中所选父图的相对路径")
    reserve = sub.add_parser("reserve", help="在调用前占用一次额度；本命令本身不会调用后端")
    reserve.add_argument("task")
    reserve.add_argument("round")
    reserve.add_argument("--evidence-ready", action="store_true")
    finish = sub.add_parser("finish", help="登记该次请求结果并保存所有产物")
    finish.add_argument("task")
    finish.add_argument("round")
    finish.add_argument("--status", required=True, choices=["succeeded", "failed", "unknown", "not_sent"])
    finish.add_argument("--output", action="append", default=[])
    finish.add_argument("--note-file", required=True)
    finish.add_argument("--locator", default="")
    derive = sub.add_parser("derive", help="登记已在本地处理的派生图及来源；不执行转换、不增加调用次数")
    derive.add_argument("task")
    derive.add_argument("round")
    derive.add_argument("candidate", help="来源候选文件名，如 output-01.png")
    derive.add_argument("--image", required=True, help="已经保存的派生图片")
    derive.add_argument("--note-file", required=True, help="说明工具、转换参数、尺寸及保留内容的 Markdown")
    review = sub.add_parser("review", help="登记实际看图结论；程序不判断视觉质量")
    review.add_argument("task")
    review.add_argument("round")
    review.add_argument("candidate")
    review.add_argument("--report-file", required=True)
    review.add_argument("--verdict", required=True, choices=["pass", "fail", "unverified"])
    review.add_argument("--blocker", action="append", default=[])
    review.add_argument("--unknown", action="append", default=[])
    promote = sub.add_parser("promote", help="复制准确通过版本到 final_output")
    promote.add_argument("task")
    promote.add_argument("round")
    promote.add_argument("candidate")
    accept = sub.add_parser("accept", help="登记用户对具体交付版本的验收原话")
    accept.add_argument("task")
    accept.add_argument("final_path")
    accept.add_argument("--status", required=True, choices=["accepted", "changes_requested"])
    accept.add_argument("--comment-file", required=True)
    batch = sub.add_parser("new-batch", help="有明确继续生成指令时新增批次；保留历史次数")
    batch.add_argument("task")
    batch.add_argument("--authorization-file", required=True)
    batch.add_argument("--limit", type=int, default=DEFAULT_REQUEST_LIMIT, help="新批次请求上限，默认 6；历史批次保持原值")
    status = sub.add_parser("status", help="记录暂停、阻塞、取消等执行状态")
    status.add_argument("task")
    status.add_argument("value", choices=["active", "paused", "blocked", "cancelled", "complete"])
    status.add_argument("--note-file", required=True)
    for command in ["recover", "preview"]:
        action = sub.add_parser(command, help="核对已有记录并更新本地 Markdown，不重发请求")
        action.add_argument("task")
    sub.add_parser("backends", help="列出已实现与预留的制作路线；不读取密钥")
    check = sub.add_parser("check-request", help="离线核对实际提交输入；不占次数、不读 key、不联网")
    check.add_argument("task")
    check.add_argument("round")
    execute = sub.add_parser("generate", help="经验证与计数后，向冻结的本轮后端发送一次请求")
    execute.add_argument("task")
    execute.add_argument("round")
    execute.add_argument("--execute", action="store_true")
    execute.add_argument("--evidence-ready", action="store_true")
    execute.add_argument("--env-file", default=str(DOTENV))
    execute.add_argument("--timeout", type=float, default=120.0)
    collect = sub.add_parser("collect", help="恢复已暂存响应的本地收录；不读取密钥、不重发")
    collect.add_argument("task")
    collect.add_argument("round")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "backends":
            for spec in BACKENDS.values():
                print(f"{spec.name} [{spec.route}] {'已实现' if spec.factory else '预留'}：{spec.description}")
            return 0
        if args.command == "init":
            TaskStore.create(args.task, brief=read_md(args.brief_file), title=args.title, mode=args.mode,
                             authorization=read_md(args.authorization_file) if args.authorization_file else "", limit=args.limit)
        else:
            task = TaskStore(args.task)
            if args.command == "prepare":
                model = args.model
                extra = {}
                if args.backend == "codex-image" and not args.api_text_file:
                    raise StoreError("codex-image 需要 --submission-file 冻结完整工具输入")
                if args.api_text_file:
                    model = resolve_model(args.backend, model, env_file=args.env_file)
                    submission = read_md(args.api_text_file)
                    images = []
                    for role, source in args.input:
                        data, suffix = raster(source)
                        images.append(ImageInput(role, data, MIME_BY_SUFFIX[suffix]))
                    selected = make_backend(args.backend, live=False, env_file=args.env_file)
                    selected.validate(ImageRequest(model, submission, tuple(images), args.aspect_ratio, args.image_size, args.operation, args.pixel_size, args.quality))
                    extra = dict(submission=submission, operation=args.operation, aspect_ratio=args.aspect_ratio, image_size=args.image_size, pixel_size=args.pixel_size, quality=args.quality)
                elif args.aspect_ratio or args.image_size or args.pixel_size or args.quality or args.operation != "generate":
                    raise StoreError("API 设置需要 --api-text-file，不能只写参数却没有实际提交文字")
                if not model:
                    raise StoreError("普通手动轮次须提供 --model；API 轮次可读取独立图像模型配置")
                number = task.prepare(read_md(args.prompt_file), read_md(args.reference_file), args.backend,
                                      model, inputs=args.input, parent=args.parent, **extra)
                print(f"第 {number} 轮输入已保存；尚未发送请求。")
            elif args.command == "check-request":
                saved, request = check_request(task, args.round)
                print(f"输入检查通过：{saved['backend']} / {request.model}，{len(request.inputs)} 张输入；未发送、未占次数。")
            elif args.command == "generate":
                result = generate_round(task, args.round, execute=args.execute, evidence_ready=args.evidence_ready,
                                        env_file=args.env_file, timeout=args.timeout)
                print(f"已保存 {len(result.images)} 张图片；尚未监修。")
            elif args.command == "collect":
                task.collect(args.round)
                print("已完成本地收录；没有新发生成请求。")
            elif args.command == "reserve":
                snapshot = task.snapshot()
                attempt = next((a for a in snapshot["attempts"] if a["id"] == args.round), None)
                if attempt and attempt["backend"] == "codex-image":
                    check_request(task, args.round)
                task.reserve(args.round, args.evidence_ready)
                print("已保守占用一次额度。实际请求仍须由 Agent 调用工具；结果不明时不能重发。")
            elif args.command == "finish":
                task.finish(args.round, args.status, args.output, read_md(args.note_file), args.locator)
            elif args.command == "derive":
                candidate = task.derive(args.round, args.candidate, args.image, read_md(args.note_file))
                print(f"本地派生候选已登记：{candidate}；需要独立监修，未增加生成次数。")
            elif args.command == "review":
                task.review(args.round, args.candidate, read_md(args.report_file), args.verdict, args.blocker, args.unknown)
            elif args.command == "promote":
                print(task.path / task.promote(args.round, args.candidate))
            elif args.command == "accept":
                task.accept(args.final_path, args.status, read_md(args.comment_file))
            elif args.command == "new-batch":
                task.new_batch(read_md(args.authorization_file), args.limit)
            elif args.command == "status":
                task.set_status(args.value, read_md(args.note_file))
            else:
                recovered = task.recover()
                print(f"本批剩余 {recovered['remaining']} 次；未决轮次：{', '.join(recovered['unresolved']) or '无'}。")
                for warning in recovered["warnings"]:
                    print(f"需核对：{warning}")
        print(f"任务预览：{Path(args.task).resolve() / 'README.md'}")
        return 0
    except BackendError as exc:
        print(f"请求状态：{exc.status}。{exc.public_message} 没有自动重试；先核对任务记录。", file=sys.stderr)
        return 2
    except (StoreError, OSError) as exc:
        print(f"操作未完成：{exc}；如涉及发送，请先 recover 核对状态，勿直接重发。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
