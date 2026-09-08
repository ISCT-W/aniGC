"""Durable local workflow records, with explicit request reservations.

This module cannot determine authorization or image quality. The caller records
those judgments. It never sends a request, retries one, or reads credentials.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid


TEMPLATES = Path(__file__).resolve().parents[2] / ".agents/skills/image-workflow/assets/templates"
DEFAULT_REQUEST_LIMIT = 6
UNRESOLVED = {"reserved", "unknown"}
UNCHARGED = {"prepared", "not_sent"}


class StoreError(ValueError):
    """A local workflow invariant would be violated."""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def require_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise StoreError(f"{name}不能为空")
    return value


def positive_limit(value):
    if type(value) is not int or value < 1:
        raise StoreError("调用上限必须为正整数")


def atomic_write(path, data):
    """Replace a derived file or state; never used for original artifacts."""
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def immutable_write(path, data, adopt=False):
    """Publish a complete file without replacing a prior version.

    An identical orphan from an interrupted operation can be adopted explicitly.
    Atomic linking avoids leaving a partially written original at its final name.
    """
    if path.is_symlink():
        raise StoreError(f"不写入符号链接：{path.name}")
    if path.exists():
        if adopt and path.read_bytes() == data:
            return
        raise StoreError(f"文件已存在，禁止覆盖：{path.name}")
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(name, path)
        except FileExistsError as exc:
            raise StoreError(f"文件已存在，禁止覆盖：{path.name}") from exc
        sync_dir(path.parent)
    finally:
        os.unlink(name)


def raster(path):
    path = Path(path)
    return raster_bytes(path.read_bytes(), path.suffix.lower())


def raster_bytes(data, kind):
    if kind not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        raise StoreError("图片只接受 PNG、JPEG、WebP、GIF 文件")
    valid = (
        kind == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n")
        or kind in {".jpg", ".jpeg"} and data.startswith(b"\xff\xd8\xff")
        or kind == ".webp" and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
        or kind == ".gif" and data[:6] in {b"GIF87a", b"GIF89a"}
    )
    if not valid:
        raise StoreError("文件头与图片扩展名不一致；这不是图像解码或质量验证")
    return data, kind


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", "<br>")


class TaskStore:
    def __init__(self, path):
        original = Path(path).absolute()
        if original.is_symlink():
            raise StoreError("任务目录不能是符号链接")
        self.path = original.resolve()

    def _path(self, relative):
        part = Path(relative)
        if part.is_absolute() or ".." in part.parts:
            raise StoreError("必须使用任务内相对路径")
        result = self.path
        for component in part.parts:
            result = result / component
            if result.is_symlink():
                raise StoreError(f"任务文件不能是符号链接：{relative}")
        return result

    @contextmanager
    def _locked(self):
        if not self.path.is_dir():
            raise StoreError("任务目录不存在")
        with self._path(".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self):
        try:
            state = json.loads(self._path("state.json").read_text())
        except (OSError, ValueError) as exc:
            raise StoreError("状态缺失或损坏；不要新建任务重置预算，请从备份核对") from exc
        from .config import reference_project

        if state.get("schema") != 1 or state.get("project_id") != reference_project(state.get("mode")):
            raise StoreError("不支持的状态版本或参考项目")
        return state

    def _save(self, state):
        atomic_write(self._path("state.json"), (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode())
        self._render(state)

    def _file(self, relative, data, adopt=False):
        path = self._path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        immutable_write(path, data, adopt=adopt)
        return {"path": relative, "sha256": digest(data)}

    def _check(self, record):
        for item in self._provenance(record):
            path = self._path(item["path"])
            if not path.is_file() or digest(path.read_bytes()) != item["sha256"]:
                raise StoreError(f"文件缺失或内容已改变：{item['path']}")

    @staticmethod
    def _provenance(record):
        """A local derivative remains bound to its source and transformation."""
        yield record
        for key in ("derived_from", "transformation"):
            if key in record:
                yield from TaskStore._provenance(record[key])

    @classmethod
    def create(cls, path, brief, title, mode="offline", authorization="", limit=DEFAULT_REQUEST_LIMIT, project_id=None):
        require_text(brief, "用户原始请求")
        require_text(title, "任务名")
        positive_limit(limit)
        from .config import reference_project

        allowed_project = reference_project(mode)
        project_id = allowed_project if project_id is None else project_id
        if project_id != allowed_project:
            raise StoreError("参考项目必须与本地配置一致；离线任务仅使用模拟项目")
        if mode == "generation":
            require_text(authorization, "明确生成授权原话")
        template_data = {}
        for name in ("reference.md", "feedback.md"):
            source = TEMPLATES / name
            if not source.is_file():
                raise StoreError(f"缺少已安装的 skill 模板：{name}")
            template_data[name] = source.read_bytes()
        task = cls(path)
        try:
            task.path.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise StoreError("目录已存在；使用 recover 恢复，不覆盖或重置") from exc
        with task._locked():
            brief_record = task._file("brief.md", brief.encode())
            auth = task._file("authorization-001.md", (authorization or "离线模拟；不构成生成授权。").encode())
            for name, dest in [("reference.md", "references/reference.md"), ("feedback.md", "feedback.md")]:
                task._file(dest, template_data[name])
            state = {
                "schema": 1, "id": str(uuid.uuid4()), "title": title, "mode": mode,
                "project_id": project_id, "created_at": now(), "brief": brief_record,
                "batches": [{"id": 1, "limit": limit, "authorization": auth, "created_at": now()}],
                "attempts": [], "finals": [], "status": "active", "status_note": "",
            }
            task._save(state)
        return task

    @staticmethod
    def _attempt(state, round_id):
        for item in state["attempts"]:
            if item["id"] == round_id:
                return item
        raise StoreError("找不到该轮次")

    @staticmethod
    def _remaining(state):
        batch = state["batches"][-1]
        used = sum(a["batch"] == batch["id"] and a["status"] not in UNCHARGED for a in state["attempts"])
        return batch["limit"] - used

    def _check_inputs(self, state, attempt):
        self._check(state["brief"])
        self._check(state["batches"][attempt["batch"] - 1]["authorization"])
        for record in [attempt["prompt"], attempt["reference"], *attempt["inputs"]]:
            self._check(record)
        if attempt.get("parent"):
            self._check(attempt["parent"])
        if attempt.get("request"):
            self._check(attempt["request"]["text"])
            self._check(attempt["request"]["settings"])
            if digest(self._request_settings(attempt).encode()) != attempt["request"]["settings"]["sha256"]:
                raise StoreError("请求模型、参数或输入顺序与冻结的设置不一致")

    @staticmethod
    def _request_settings(attempt):
        request = attempt["request"]
        lines = ["# 实际请求设置", "", f"后端：{attempt['backend']}", f"模型：{attempt['model']}",
                 f"操作：{request['operation']}", f"比例：{request['aspect_ratio'] or '未指定'}",
                 f"输出尺寸档位：{request['image_size'] or '未指定'}", "",
                 f"实际文字：{request['text']['path']}；SHA-256：{request['text']['sha256']}", "",
                 "## 图片输入（按实际顺序）", ""]
        for record in attempt["inputs"]:
            lines.append(f"- {record['role']}：{record['path']}；SHA-256：{record['sha256']}")
        return "\n".join(lines) + "\n"

    def prepare(self, prompt, reference, backend, model, inputs=(), parent=None,
                submission=None, operation="generate", aspect_ratio=None, image_size=None):
        for value, label in [(prompt, "完整 prompt"), (reference, "参考基线"), (backend, "后端"), (model, "准确模型标识或离线标识")]:
            require_text(value, label)
        if submission is not None:
            require_text(submission, "实际提交文字")
        # Finish all input reads before creating a round or consuming any budget.
        source_images = []
        for role, source in inputs:
            if role not in {"reference", "base", "mask"}:
                raise StoreError("输入用途必须为 reference/base/mask")
            data, suffix = raster(source)
            source_images.append((role, data, suffix))
        with self._locked():
            state = self._read()
            if state["status"] != "active":
                raise StoreError("任务已暂停、结束或取消；先核对恢复范围")
            self._check(state["brief"])
            parent_record = None
            if parent:
                for attempt in state["attempts"]:
                    for output in attempt["outputs"]:
                        if output["path"] == parent:
                            parent_record = output.copy()
                if not parent_record:
                    raise StoreError("父图必须是本任务已登记的候选路径")
                self._check(parent_record)
            rounds = self._path("rounds")
            rounds.mkdir(exist_ok=True)
            existing = [int(p.name) for p in rounds.iterdir() if p.name.isdigit()]
            number = max(existing + [0]) + 1
            round_id = f"{number:03d}"
            folder = f"rounds/{round_id}"
            self._path(folder).mkdir()
            input_records = []
            for i, (role, data, suffix) in enumerate(source_images, 1):
                record = self._file(f"{folder}/inputs/{i:02d}-{role}{suffix}", data)
                record["role"] = role
                input_records.append(record)
            attempt = {
                "id": round_id, "batch": state["batches"][-1]["id"], "status": "prepared",
                "backend": backend, "model": model, "created_at": now(),
                "prompt": self._file(f"{folder}/prompt.md", prompt.encode()),
                "reference": self._file(f"{folder}/reference.md", reference.encode()),
                "inputs": input_records, "parent": parent_record, "outputs": [], "reviews": [], "events": [],
            }
            if submission is not None:
                attempt["request"] = {
                    "text": self._file(f"{folder}/submission.md", submission.encode()),
                    "operation": operation, "aspect_ratio": aspect_ratio, "image_size": image_size,
                }
                attempt["request"]["settings"] = self._file(f"{folder}/settings.md", self._request_settings(attempt).encode())
            state["attempts"].append(attempt)
            self._save(state)
            return round_id

    def prepared_request(self, round_id):
        """Return verified in-memory input copies; no credentials or networking."""
        with self._locked():
            state = self._read()
            attempt = self._attempt(state, round_id)
            if attempt["status"] != "prepared":
                raise StoreError("本轮已预留或提交；先 recover/collect，不可再次发送")
            if not attempt.get("request"):
                raise StoreError("本轮未准备实际提交文字和请求设置；使用 --api-text-file 创建新轮次")
            self._check_inputs(state, attempt)
            request = attempt["request"]
            return {
                "mode": state["mode"], "backend": attempt["backend"], "model": attempt["model"],
                "prompt": self._path(request["text"]["path"]).read_text(),
                "operation": request["operation"], "aspect_ratio": request["aspect_ratio"], "image_size": request["image_size"],
                "inputs": [(r["role"], self._path(r["path"]).read_bytes(), Path(r["path"]).suffix) for r in attempt["inputs"]],
            }

    def reserve(self, round_id, evidence_ready=False):
        with self._locked():
            state = self._read()
            attempt = self._attempt(state, round_id)
            if state["status"] != "active" or attempt["status"] != "prepared":
                raise StoreError("只有进行中任务的未提交轮次才能预留请求")
            if attempt["batch"] != state["batches"][-1]["id"]:
                raise StoreError("不能提交旧批次的草稿")
            if not evidence_ready:
                raise StoreError("需要 Agent 确认资料足够；文件存在不代表证据足够")
            if any(a["status"] in UNRESOLVED for a in state["attempts"]):
                raise StoreError("已有未决请求；先核实它，禁止自动重复提交")
            if self._remaining(state) < 1:
                raise StoreError("已达到本批调用上限")
            self._check_inputs(state, attempt)
            attempt["status"] = "reserved"
            attempt["events"].append({"at": now(), "status": "reserved", "note": "调用前预留，尚不能推断远端是否收到"})
            self._save(state)
            return attempt

    def finish(self, round_id, status, outputs=(), note="", locator=""):
        if status not in {"succeeded", "failed", "unknown", "not_sent"}:
            raise StoreError("执行状态无效")
        require_text(note, "执行结果或核对依据")
        if locator and ("://" in locator or "?" in locator or len(locator) > 200):
            raise StoreError("仅保存不敏感的稳定请求 ID，不保存 URL 或签名")
        images = [raster(source) for source in outputs]
        if status == "succeeded" and not images:
            raise StoreError("成功请求必须保存所有返回图片")
        if status != "succeeded" and images:
            raise StoreError("有图片时按成功取回记录；局部返回情况写入结果说明")
        with self._locked():
            state = self._read()
            attempt = self._attempt(state, round_id)
            if attempt["status"] not in UNRESOLVED:
                raise StoreError("本轮不处于未决状态，不能重复登记或覆盖结果")
            if status == "not_sent" and attempt["status"] != "reserved":
                raise StoreError("未知的发送结果不能退款；只能核实已提交请求")
            if status in {"not_sent", "failed"} and any(self._path(f"rounds/{round_id}").glob("output-*")):
                raise StoreError("本轮已有未登记产物；先核对并恢复成功取回记录，不能改成未发送或失败")
            receipt = attempt.get("receipt")
            if receipt and status == "succeeded":
                for record in [receipt["report"], *receipt["outputs"]]:
                    self._check(record)
                incoming = [{"path": f"rounds/{round_id}/output-{i:02d}{suffix}", "sha256": digest(data)}
                            for i, (data, suffix) in enumerate(images, 1)]
                if incoming != receipt["outputs"]:
                    raise StoreError("恢复收录必须使用已暂存的全部原图及原始顺序")
            output_records = []
            for i, (data, suffix) in enumerate(images, 1):
                output_records.append(self._file(f"rounds/{round_id}/output-{i:02d}{suffix}", data, adopt=True))
            attempt["status"] = status
            attempt["outputs"] = output_records
            attempt["events"].append({"at": now(), "status": status, "note": note, "locator": locator})
            self._save(state)
            return attempt

    def stage_result(self, round_id, images, report, locator="", complete=True):
        """Persist a response before completion so collect never needs a resend."""
        require_text(report, "后端执行报告")
        validated = [raster_bytes(data, suffix) for data, suffix in images]
        if not validated:
            raise StoreError("没有可保存的响应图片")
        with self._locked():
            state = self._read()
            attempt = self._attempt(state, round_id)
            if attempt["status"] not in UNRESOLVED:
                raise StoreError("只有未决请求能暂存返回产物")
            records = [{"path": f"rounds/{round_id}/output-{i:02d}{suffix}", "sha256": digest(data)}
                       for i, (data, suffix) in enumerate(validated, 1)]
            note = {"path": f"rounds/{round_id}/response.md", "sha256": digest(report.encode())}
            receipt = {"outputs": records, "report": note, "locator": locator, "complete": complete}
            if attempt.get("receipt") and attempt["receipt"] != receipt:
                raise StoreError("已有另一份返回记录，不可覆盖")
            # Record the response manifest (especially completeness) before any
            # output becomes available. A crash cannot orphan an image while
            # losing the information that prevents an incomplete delivery.
            attempt["receipt"] = receipt
            self._save(state)
            for record, (data, _) in zip(records, validated):
                self._file(record["path"], data, adopt=True)
            self._file(note["path"], report.encode(), adopt=True)
            self._render(state)

    def collect(self, round_id):
        """Finalize already persisted response files, without invoking a backend."""
        with self._locked():
            state = self._read()
            attempt = self._attempt(state, round_id)
            receipt = attempt.get("receipt")
            if not receipt:
                raise StoreError("没有完整暂存的响应；先核对未决请求和已有文件，不要重发")
            for record in [receipt["report"], *receipt["outputs"]]:
                self._check(record)
            if attempt["status"] == "succeeded":
                return attempt
            paths = [self._path(record["path"]) for record in receipt["outputs"]]
            locator = receipt["locator"]
        return self.finish(round_id, "succeeded", paths, note="已收录原请求返回的可读图片。是否完整及执行信息见 response.md；尚未进行视觉监修。", locator=locator)

    def _output(self, attempt, candidate):
        if Path(candidate).name != candidate:
            raise StoreError("候选参数使用该轮的文件名，如 output-01.png")
        for output in attempt["outputs"]:
            if Path(output["path"]).name == candidate:
                return output
        raise StoreError("找不到已保存的候选图片")

    def derive(self, round_id, candidate, image, note):
        """Register an already-created local derivative, without a model call.

        Original API files and their receipt stay intact. The new candidate has
        its own review history and immutable source/transformation provenance.
        """
        require_text(note, "本地转换说明")
        data, suffix = raster(image)
        with self._locked():
            state = self._read()
            attempt = self._attempt(state, round_id)
            if attempt["status"] != "succeeded":
                raise StoreError("只有已成功取回图片的轮次能登记本地派生候选")
            self._check_inputs(state, attempt)
            source = self._output(attempt, candidate)
            self._check(source)
            explanation = (
                "# 本地派生处理\n\n"
                f"来源：[{candidate}]({candidate})\n\n"
                f"来源 SHA-256：`{source['sha256']}`\n\n"
                "这里只登记本地处理后的图片，不执行转换、不调用模型、不增加生成次数。\n\n"
                f"## 处理说明\n\n{note}\n"
            ).encode()
            existing = [output for output in attempt["outputs"] if "derived_from" in output]
            for output in existing:
                if (output["derived_from"] == source and output["sha256"] == digest(data)
                        and output["transformation"]["sha256"] == digest(explanation)):
                    self._check(output)
                    return Path(output["path"]).name
            name = f"derived-{len(existing) + 1:03d}"
            folder = f"rounds/{round_id}"
            transformation = self._file(f"{folder}/{name}.md", explanation, adopt=True)
            output = self._file(f"{folder}/{name}{suffix}", data, adopt=True)
            output["derived_from"] = source.copy()
            output["transformation"] = transformation
            attempt["outputs"].append(output)
            attempt["events"].append({
                "at": now(), "status": "derived",
                "note": f"登记本地派生候选 {name}{suffix}，来源 {candidate}；需要独立监修，未调用生成后端。",
            })
            self._save(state)
            return f"{name}{suffix}"

    def review(self, round_id, candidate, report, verdict, blockers=(), unknowns=()):
        require_text(report, "实际看图的监修报告")
        if verdict not in {"pass", "fail", "unverified"}:
            raise StoreError("监修结论须为 pass/fail/unverified")
        if verdict == "pass" and (blockers or unknowns):
            raise StoreError("仍有阻断问题或关键待核验项，不能通过")
        with self._locked():
            state = self._read()
            attempt = self._attempt(state, round_id)
            if attempt["status"] != "succeeded":
                raise StoreError("先保存成功取回的图片才能监修")
            self._check_inputs(state, attempt)
            output = self._output(attempt, candidate)
            self._check(output)
            n = len(attempt["reviews"]) + 1
            name = "review.md" if n == 1 else f"review-v{n:03d}.md"
            result = {
                "candidate": candidate, "image": output.copy(), "verdict": verdict,
                "blockers": list(blockers), "unknowns": list(unknowns), "at": now(),
                "report": self._file(f"rounds/{round_id}/{name}", report.encode(), adopt=True),
            }
            attempt["reviews"].append(result)
            self._save(state)
            return result

    def _latest_review(self, attempt, candidate):
        matches = [r for r in attempt["reviews"] if r["candidate"] == candidate]
        if not matches:
            raise StoreError("该候选尚未登记监修")
        return matches[-1]

    def _valid_pass(self, state, attempt, candidate):
        self._check_inputs(state, attempt)
        if attempt.get("receipt") and not attempt["receipt"].get("complete", True):
            raise StoreError("本轮响应未完整取得；候选可检查，但不能将其作为完整交付")
        if attempt.get("receipt"):
            for record in [attempt["receipt"]["report"], *attempt["receipt"]["outputs"]]:
                self._check(record)
        review = self._latest_review(attempt, candidate)
        if review["verdict"] != "pass" or review["blockers"] or review["unknowns"]:
            raise StoreError("只有无阻断、无关键待核验项的通过版本才能交付或接受")
        self._check(self._output(attempt, candidate))
        self._check(review["image"])
        self._check(review["report"])
        return review

    def promote(self, round_id, candidate):
        with self._locked():
            state = self._read()
            attempt = self._attempt(state, round_id)
            output = self._output(attempt, candidate)
            review = self._valid_pass(state, attempt, candidate)
            for item in state["finals"]:
                if item["source"] == output and item["review"] == review["report"]:
                    self._check(item["file"])
                    return item["file"]["path"]
            number = len(state["finals"]) + 1
            path = f"final_output/final-v{number:03d}-01{Path(candidate).suffix}"
            data = self._path(output["path"]).read_bytes()
            delivered = self._file(path, data, adopt=True)
            self._check(delivered)
            state["finals"].append({
                "file": delivered, "source": output.copy(), "round": round_id,
                "review": review["report"].copy(), "at": now(), "acceptance": "pending",
                "feedback": [],
            })
            self._save(state)
            return path

    def accept(self, final_path, status, comment):
        if status not in {"accepted", "changes_requested"}:
            raise StoreError("用户验收须为 accepted/changes_requested；没有回复保持 pending")
        require_text(comment, "用户验收原话")
        with self._locked():
            state = self._read()
            item = next((v for v in state["finals"] if v["file"]["path"] == final_path), None)
            if item is None:
                raise StoreError("找不到该交付版本")
            self._check(item["file"])
            if status == "accepted":
                self._check(item["source"])
                self._check(item["review"])
                attempt = self._attempt(state, item["round"])
                self._valid_pass(state, attempt, Path(item["source"]["path"]).name)
            fid = f"F{sum(len(v['feedback']) for v in state['finals']) + 1:03d}"
            record = {"id": fid, "at": now(), "status": status, "comment": comment}
            item["feedback"].append(record)
            item["acceptance"] = status
            self._save(state)

    def new_batch(self, authorization, limit=DEFAULT_REQUEST_LIMIT):
        require_text(authorization, "继续生成的新授权原话")
        positive_limit(limit)
        with self._locked():
            state = self._read()
            if any(a["status"] in UNRESOLVED for a in state["attempts"]):
                raise StoreError("先核实未决请求，不能用新批次绕过")
            number = len(state["batches"]) + 1
            auth = self._file(f"authorization-{number:03d}.md", authorization.encode(), adopt=True)
            state["batches"].append({"id": number, "limit": limit, "authorization": auth, "created_at": now()})
            state["status"] = "active"
            state["status_note"] = ""
            self._save(state)

    def set_status(self, status, note):
        if status not in {"active", "paused", "blocked", "cancelled", "complete"}:
            raise StoreError("执行状态无效")
        require_text(note, "状态变更依据与下一步")
        with self._locked():
            state = self._read()
            state["status"] = status
            state["status_note"] = note
            self._save(state)

    def snapshot(self):
        with self._locked():
            return self._read()

    def _warnings(self, state):
        warnings = []
        records = [state["brief"], *(b["authorization"] for b in state["batches"])]
        for attempt in state["attempts"]:
            records += [attempt["prompt"], attempt["reference"], *attempt["inputs"], *attempt["outputs"]]
            records += [r["report"] for r in attempt["reviews"]]
            if attempt.get("request"):
                records += [attempt["request"]["text"], attempt["request"]["settings"]]
                try:
                    self._check_inputs(state, attempt)
                except StoreError as exc:
                    warnings.append(str(exc))
            if attempt.get("receipt"):
                records += [attempt["receipt"]["report"], *attempt["receipt"]["outputs"]]
        records += [f["file"] for f in state["finals"]]
        for record in records:
            try:
                self._check(record)
            except StoreError as exc:
                warnings.append(str(exc))
        known = {item["path"] for record in records for item in self._provenance(record)}
        # Orphan files indicate an interrupted multi-file operation, never success.
        for folder in ("rounds", "final_output"):
            root = self._path(folder)
            if root.exists():
                for file in root.rglob("*"):
                    relative = file.relative_to(self.path).as_posix()
                    if file.is_symlink():
                        warnings.append(f"发现符号链接，需核查：{relative}")
                    elif file.is_file() and relative not in known and file.name not in {"execution.md", "acceptance.md"}:
                        warnings.append(f"未登记文件，需核对中断/本地补充资料：{relative}")
        for attempt in state["attempts"]:
            if attempt["status"] in UNRESOLVED:
                if attempt.get("receipt"):
                    warnings.append(f"第 {attempt['id']} 轮已暂存响应，使用 collect 完成本地收录；禁止重新生图")
                else:
                    warnings.append(f"第 {attempt['id']} 轮请求未决，先核实远端状态；禁止自动重发")
            if attempt["status"] == "succeeded":
                reviewed = {r["candidate"] for r in attempt["reviews"]}
                for output in attempt["outputs"]:
                    if Path(output["path"]).name not in reviewed:
                        warnings.append(f"图片尚未监修：{output['path']}")
        return warnings

    def recover(self):
        with self._locked():
            state = self._read()
            warnings = self._warnings(state)
            self._render(state, warnings)
            return {"state": state, "warnings": warnings, "remaining": self._remaining(state),
                    "unresolved": [a["id"] for a in state["attempts"] if a["status"] in UNRESOLVED]}

    def preview(self):
        self.recover()
        return self._path("README.md")

    def _render(self, state, warnings=None):
        warnings = self._warnings(state) if warnings is None else warnings
        mode = "离线模拟（不代表真实生成或角色监修）" if state["mode"] == "offline" else "正式图片任务"
        batch = state["batches"][-1]
        lines = [f"# {state['title']}", "", mode, "",
                 f"参考项目：{state['project_id']}；创建时间：{state['created_at']}（UTC）。", "",
                 f"执行状态：{state['status']}。{state['status_note']}", "",
                 f"当前批次 {batch['id']}：已占用 {batch['limit'] - self._remaining(state)} / {batch['limit']} 次；预留及未知请求保守计入。", "",
                 "此页自动汇总；不要手工改计数。原始请求见 [brief.md](brief.md)，补充目标另存文件并在新轮 prompt 引用。", "",
                 "## 用户原始请求", "", self._path(state["brief"]["path"]).read_text() if self._path(state["brief"]["path"]).is_file() else "原始请求文件缺失。", "",
                 "## 参考与反馈", "", "[当前参考草稿](references/reference.md) · [用户反馈](feedback.md)", "",
                 "## 轮次", "", "| 轮次 / 批次 | 执行 | 候选预览与监修 | 输入与修改 |", "| --- | --- | --- | --- |"]
        for a in state["attempts"]:
            views = []
            for output in a["outputs"]:
                name = Path(output["path"]).name
                try:
                    r = self._latest_review(a, name)
                    label = r["verdict"]
                    try:
                        self._check_inputs(state, a)
                        self._check(r["report"])
                        self._check(output)
                    except StoreError:
                        label += "（记录变动，需复核）"
                    if a.get("receipt") and not a["receipt"].get("complete", True):
                        label += "（响应不完整，不能交付）"
                    label += f"；阻断 {len(r['blockers'])}；待核验 {len(r['unknowns'])}"
                    if r["blockers"] or r["unknowns"]:
                        label += "：" + cell(", ".join(r["blockers"] + r["unknowns"]))
                    label = f"[{label}]({r['report']['path']})"
                except StoreError:
                    label = "尚未监修"
                if output.get("derived_from"):
                    label += (f"<br>本地派生 · [来源]({output['derived_from']['path']})"
                              f" · [处理说明]({output['transformation']['path']})")
                views.append(f"![{name}]({output['path']})<br>{label}")
            lines.append(f"| {a['id']} / {a['batch']} | [{a['status']}](rounds/{a['id']}/execution.md) | {'<br>'.join(views) or '尚无图片'} | [完整 prompt 与修改要求]({a['prompt']['path']}) · [参考快照]({a['reference']['path']}) |")
            execution = [f"# 第 {a['id']} 轮执行记录", "", f"后端：{a['backend']}；模型：{a['model']}；状态：{a['status']}。", "",
                         "此页自动生成。完整实际文字、可获得的设置/耗时/成本见 prompt 或执行说明；未提供项为未知。", "",
                         f"父图：{a['parent']['path'] if a['parent'] else '无'}", "", "## 实际输入（按顺序）", ""]
            if a.get("request"):
                execution += ["[实际提交文字](submission.md) · [准确模型与设置](settings.md)", ""]
            if a.get("receipt"):
                execution += ["[后端执行报告与返回信息](response.md)", ""]
                if not a["receipt"].get("complete", True):
                    execution += ["响应含未能取得的产物，本轮不满足完整交付条件。", ""]
            for record in a["inputs"]:
                execution.append(f"- {record['role']}：`{record['path']}`；SHA-256 `{record['sha256']}`")
            derived = [output for output in a["outputs"] if output.get("derived_from")]
            if derived:
                execution += ["", "## 本地派生候选（不增加生成次数）", ""]
                for output in derived:
                    execution.append(
                        f"- [{Path(output['path']).name}]({Path(output['path']).name})"
                        f"；来源 [{Path(output['derived_from']['path']).name}]({Path(output['derived_from']['path']).name})"
                        f"；[处理说明]({Path(output['transformation']['path']).name})；须独立监修。"
                    )
            for event in a["events"]:
                execution += ["", f"### {event['at']} — {event['status']}", "", event["note"]]
                if event.get("locator"):
                    execution += ["", f"稳定请求 ID：`{event['locator']}`"]
            atomic_write(self._path(f"rounds/{a['id']}/execution.md"), ("\n".join(execution) + "\n").encode())
        lines += ["", "## 交付版本", "", "final_output 只收录登记过 Agent 通过的准确版本；用户结论独立记录。", ""]
        acceptance = ["# 交付与用户验收", "", mode, "", "此表由本地状态生成，原始报告和评论另有链接；历史通过不等于当前仍有效。", "",
                      "[用户验收原话与反馈](../feedback.md)。检查范围与局限以所链接的监修报告为准。", "",
                      "| 版本 | Agent 依据 | 用户状态 | 完整性 |", "| --- | --- | --- | --- |"]
        for item in state["finals"]:
            labels = {"pending": "待用户验收", "accepted": "用户验收通过", "changes_requested": "用户要求修改"}
            integrity = "文件一致"
            try:
                for rec in [item["file"], item["source"], item["review"]]:
                    self._check(rec)
                a = self._attempt(state, item["round"])
                self._valid_pass(state, a, Path(item["source"]["path"]).name)
            except StoreError:
                integrity = "文件变动或后续监修无效，旧通过记录不可沿用"
            p = item["file"]["path"]
            lines += [f"- [{Path(p).name}]({p})：{labels[item['acceptance']]}；{integrity}。"]
            acceptance.append(f"| [{Path(p).name}]({Path(p).name}) | [第 {item['round']} 轮报告](../{item['review']['path']}) | {labels[item['acceptance']]} | {integrity} |")
        if state["finals"]:
            lines += ["", "[逐版本验收记录](final_output/acceptance.md)"]
            for item in state["finals"]:
                acceptance += ["", f"## {Path(item['file']['path']).name}", "",
                               f"来源：[准确候选](../{item['source']['path']})；交付时间：{item['at']}。", "",
                               f"图片 SHA-256：`{item['file']['sha256']}`。", "",
                               f"交付依据报告 SHA-256：`{item['review']['sha256']}`。"]
            atomic_write(self._path("final_output/acceptance.md"), ("\n".join(acceptance) + "\n").encode())
        if warnings:
            lines += ["", "## 恢复核对", ""] + [f"- {w}" for w in warnings]
        atomic_write(self._path("README.md"), ("\n".join(lines) + "\n").encode())
        self._render_feedback(state)

    def _render_feedback(self, state):
        # Only this delimited section is derived. The agent's raw comments,
        # interpretation, calibration rules and links outside it remain intact.
        start, end = "<!-- anigc:acceptance:start -->", "<!-- anigc:acceptance:end -->"
        path = self._path("feedback.md")
        original = path.read_text()
        body = [start, "", "## 已登记的用户验收原话", ""]
        for item in state["finals"]:
            for f in item["feedback"]:
                body += [f"### {f['id']} · {f['at']}", "", f"图片：[{Path(item['file']['path']).name}]({item['file']['path']})；状态：{f['status']}。", "",
                         "原话：", "", *["> " + line for line in f["comment"].splitlines()], ""]
        body += [end]
        section = "\n".join(body)
        if start in original and end in original:
            before, rest = original.split(start, 1)
            _, after = rest.split(end, 1)
            result = before + section + after
        else:
            result = original.rstrip() + "\n\n" + section + "\n"
        atomic_write(path, result.encode())
