"""供自动化调用的版本化比较任务接口。"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import logging
import os
from pathlib import Path
import re
import stat
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .batch import BatchBusyError, BatchCapacityError, BatchIdempotencyConflict
from .engine import inspect_pdf


MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_PAIRS = 2000
LOGGER = logging.getLogger(__name__)


class ApiProblem(Exception):
    def __init__(self, status: int, code: str, detail: str, *, retryable: bool = False):
        self.status = status
        self.code = code
        self.detail = detail
        self.retryable = retryable


def problem_response(request: Request, error: ApiProblem) -> JSONResponse:
    return JSONResponse(
        {
            "type": f"/api/v1/problems/{error.code}",
            "title": error.code,
            "status": error.status,
            "detail": error.detail,
            "instance": request.url.path,
            "code": error.code,
            "retryable": error.retryable,
        },
        status_code=error.status,
        media_type="application/problem+json",
    )


async def problem_handler(request: Request, error: ApiProblem):
    return problem_response(request, error)


class ProblemRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request: Request):
            try:
                return await original(request)
            except (ApiProblem, RequestValidationError, StarletteHTTPException):
                raise
            except Exception:
                LOGGER.exception("Versioned comparison API request failed")
                return problem_response(request, ApiProblem(500, "internal_error", "请求处理失败，请检查服务日志。"))

        return handle


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)


class Source(StrictModel):
    path: str | None = Field(default=None, min_length=1, max_length=4096)
    document_id: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def one_source(self):
        if (self.path is None) == (self.document_id is None):
            raise ValueError("每个来源必须且只能提供 path 或 document_id。")
        return self


class PagePair(StrictModel):
    reference_page: int = Field(ge=1)
    candidate_page: int = Field(ge=1)


class PageSelection(StrictModel):
    mode: Literal["all", "pairs"] = "all"
    pairs: list[PagePair] | None = Field(default=None, min_length=1, max_length=MAX_PAIRS)

    @model_validator(mode="after")
    def valid_selection(self):
        if (self.mode == "pairs") != (self.pairs is not None):
            raise ValueError("pairs 模式需要配对列表，all 模式只接受 mode。")
        if self.pairs:
            values = [(pair.reference_page, pair.candidate_page) for pair in self.pairs]
            if len(values) != len(set(values)):
                raise ValueError("页面配对不能重复。")
        return self


class Settings(StrictModel):
    dpi: Literal[96, 150, 200, 300, 600] = 150
    ink_threshold: int = Field(default=220, ge=1, le=254)


class ComparisonRequest(StrictModel):
    reference: Source
    candidate: Source
    pages: PageSelection = Field(default_factory=PageSelection)
    settings: Settings = Field(default_factory=Settings)
    region_mm: list[float] | None = Field(default=None, min_length=4, max_length=4)


def _local_path(path: str) -> Path:
    if any(ord(char) < 32 for char in path) or path.startswith(("\\\\", "//")) or "://" in path:
        raise ApiProblem(403, "source_access_denied", "来源必须为允许目录中的普通本地 PDF 文件。")
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ApiProblem(422, "invalid_request", "文件路径必须为服务所在机器的绝对路径。")
    # Windows 备用数据流和设备名不属于普通文件来源。
    if os.name == "nt":
        check_reserved = getattr(os.path, "isreserved", None)
        reserved = check_reserved(str(candidate)) if check_reserved is not None else candidate.is_reserved()
        if ":" in candidate.as_posix()[2:] or reserved:
            raise ApiProblem(403, "source_access_denied", "来源必须为普通本地 PDF 文件。")
    return candidate


def _inside(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path.is_relative_to(root) for root in roots)


def _file_identity(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _opened_path(stream, fallback: Path) -> Path:
    """核对实际打开的目标，避免重解析路径跨出允许目录。"""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        import msvcrt

        function = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
        function.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        function.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        count = function(msvcrt.get_osfhandle(stream.fileno()), buffer, len(buffer), 0)
        if not count or count >= len(buffer):
            raise OSError("Cannot resolve opened file")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            raise ApiProblem(403, "source_access_denied", "不支持网络文件来源。")
        if value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value)
    descriptor = Path(f"/proc/self/fd/{stream.fileno()}")
    return Path(os.readlink(descriptor)) if descriptor.exists() else fallback.resolve(strict=True)


def read_source(path: str, roots: tuple[Path, ...]) -> tuple[bytes, str]:
    candidate = _local_path(path)
    identified = False
    try:
        resolved = candidate.resolve(strict=True)
        _local_path(str(resolved))
        if not _inside(resolved, roots):
            raise ApiProblem(403, "source_access_denied", "文件实际目标位于允许目录之外。")
        initial = resolved.stat()
        identified = True
        if not stat.S_ISREG(initial.st_mode):
            raise ApiProblem(403, "source_access_denied", "来源必须为普通文件。")
        if initial.st_size > MAX_FILE_BYTES:
            raise ApiProblem(413, "source_too_large", "单个 PDF 最大支持 32 MiB。")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(resolved, flags), "rb") as stream:
            before = os.fstat(stream.fileno())
            actual = _opened_path(stream, resolved)
            _local_path(str(actual))
            if not stat.S_ISREG(before.st_mode) or not _inside(actual, roots):
                raise ApiProblem(403, "source_access_denied", "文件实际目标不在可读范围内。")
            if before.st_size > MAX_FILE_BYTES:
                raise ApiProblem(413, "source_too_large", "单个 PDF 最大支持 32 MiB。")
            data = stream.read(MAX_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(data) > MAX_FILE_BYTES:
            raise ApiProblem(413, "source_too_large", "单个 PDF 最大支持 32 MiB。")
        current = candidate.resolve(strict=True)
        initial_identity = _file_identity(initial)
        unchanged = (
            _file_identity(before) == initial_identity
            and _file_identity(after) == initial_identity
            and _file_identity(current.stat()) == initial_identity
        )
        if current != resolved or not unchanged or len(data) != after.st_size:
            raise ApiProblem(409, "source_changed", "读取期间文件发生变化，请等待生成完成后使用新请求重试。", retryable=True)
        return data, resolved.name
    except ApiProblem:
        raise
    except (FileNotFoundError, NotADirectoryError):
        if identified:
            raise ApiProblem(409, "source_changed", "读取期间文件已被替换或移除，请等待生成完成后重试。", retryable=True) from None
        raise ApiProblem(404, "source_not_found", "找不到指定的 PDF 文件。") from None
    except (OSError, RuntimeError, ValueError):
        raise ApiProblem(403, "source_access_denied", "无法读取指定来源，请检查目录权限和路径。") from None


def _prepare_sources(sources: dict, roots: tuple[Path, ...]) -> tuple[dict, dict, dict]:
    data, files, info = {}, {}, {}
    for side, source in sources.items():
        if isinstance(source, str):
            content, filename = read_source(source, roots)
        else:
            content, filename = source.data, source.metadata["filename"]
        if len(content) > MAX_FILE_BYTES:
            raise ApiProblem(413, "source_too_large", "单个 PDF 最大支持 32 MiB。")
        try:
            if b"%PDF-" not in content[:1024]:
                raise ValueError("Missing PDF header")
            metadata = inspect_pdf(content)
        except Exception:
            raise ApiProblem(422, "invalid_pdf", f"无法解析{'参考' if side == 'reference' else '待比较'} PDF。") from None
        data[side] = content
        info[side] = metadata
        files[side] = {"filename": filename, "sha256": hashlib.sha256(content).hexdigest(),
                       "size_bytes": len(content), "read_at": datetime.now(timezone.utc).isoformat(),
                       "page_count": metadata["page_count"]}
    return data, files, info


def _selection(settings: ComparisonRequest, info: dict) -> tuple[tuple[int, int], ...]:
    reference_count = info["reference"]["page_count"]
    candidate_count = info["candidate"]["page_count"]
    if settings.pages.mode == "all":
        if reference_count != candidate_count:
            raise ApiProblem(422, "page_count_mismatch", "全页比较要求两份 PDF 页数相同，请指定页面配对。")
        pairs = tuple((page, page) for page in range(1, reference_count + 1))
    else:
        pairs = tuple((pair.reference_page, pair.candidate_page) for pair in settings.pages.pairs)
        if any(a > reference_count or b > candidate_count for a, b in pairs):
            raise ApiProblem(422, "invalid_request", "配对页码超出 PDF 页数。")
    region = settings.region_mm
    if region is not None:
        if len(pairs) != 1:
            raise ApiProblem(422, "invalid_request", "局部选区只支持一个页面配对。")
        a, b = pairs[0]
        page_a, page_b = info["reference"]["pages"][a - 1], info["candidate"]["pages"][b - 1]
        width, height = max(page_a["width_mm"], page_b["width_mm"]), max(page_a["height_mm"], page_b["height_mm"])
        x0, y0, x1, y1 = region
        if not (0 <= x0 < x1 <= width + 1e-6 and 0 <= y0 < y1 <= height + 1e-6):
            raise ApiProblem(422, "invalid_request", "选区必须位于共同可见画布内，宽高均大于零。")
    return pairs


def _job(request: Request, key: str):
    try:
        job = request.app.state.batches.get(key)
        if not job.retain_details:
            raise KeyError(key)
        return job
    except KeyError:
        raise ApiProblem(404, "comparison_not_found", "任务不存在、已过期或已被容量淘汰。") from None


def _pair(job, index: int):
    if not 1 <= index <= job.total_pages:
        raise ApiProblem(404, "pair_not_found", "比较项编号超出范围，编号从 1 开始。")
    entry = job.v1_pair(index)
    if entry is None:
        raise ApiProblem(409, "pair_not_ready", "该比较项尚未处理完成。", retryable=not job.terminal)
    return entry


router = APIRouter(prefix="/api/v1", route_class=ProblemRoute)


@router.post("/comparisons", status_code=202)
async def create_comparison(request: Request, settings: ComparisonRequest):
    key = request.headers.get("idempotency-key")
    if key is not None and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key) is None:
        raise ApiProblem(422, "invalid_request", "Idempotency-Key 应为 1 至 128 位字母、数字或 . _ : -。")
    fingerprint = hashlib.sha256(settings.model_dump_json().encode("utf-8")).hexdigest()
    manager = request.app.state.batches
    try:
        existing = manager.idempotent_job(key, fingerprint) if key else None
    except BatchIdempotencyConflict:
        raise ApiProblem(409, "idempotency_conflict", "该幂等键已用于不同的请求内容。") from None
    if existing is not None:
        return JSONResponse(existing.v1_status(), status_code=202, headers={"Location": existing.v1_status()["links"]["self"]})
    try:
        reservation = manager.reserve()
    except BatchBusyError:
        raise ApiProblem(409, "comparison_busy", "已有比较任务正在准备或运行。", retryable=True) from None
    try:
        sources = {}
        for side in ("reference", "candidate"):
            source = getattr(settings, side)
            if source.path is not None:
                sources[side] = source.path
            else:
                try:
                    sources[side] = request.app.state.store.get_document(source.document_id)
                except HTTPException as error:
                    if error.status_code != 404:
                        raise
                    raise ApiProblem(404, "source_not_found", "上传文档不存在或已过期，请重新上传。") from None
        data, files, info = await request.app.state.call_engine(_prepare_sources, sources, request.app.state.allowed_roots)
        pairs = _selection(settings, info)
        job = manager.create(
            reference_data=data["reference"], candidate_data=data["candidate"], total_pages=len(pairs),
            files=files, engine=request.app.state.engine_versions(), dpi=settings.settings.dpi,
            ink_threshold=settings.settings.ink_threshold, page_pairs=pairs,
            region_mm=tuple(settings.region_mm) if settings.region_mm is not None else None,
            retain_details=True, reservation=reservation, idempotency_key=key, request_fingerprint=fingerprint,
        )
    except BatchCapacityError:
        raise ApiProblem(413, "capacity_exceeded", "任务源文件超出当前缓存容量。") from None
    except BatchBusyError:
        raise ApiProblem(409, "comparison_busy", "已有比较任务正在运行。", retryable=True) from None
    finally:
        manager.release_reservation(reservation)
    payload = job.v1_status()
    return JSONResponse(payload, status_code=202, headers={"Location": payload["links"]["self"]})


@router.get("/comparisons/{key}")
async def comparison_status(request: Request, key: str):
    return _job(request, key).v1_status()


@router.get("/comparisons/{key}/report")
async def comparison_report(request: Request, key: str):
    job = _job(request, key)
    if not job.terminal:
        raise ApiProblem(409, "result_not_ready", "任务仍在处理，请查询状态后再读取报告。", retryable=True)
    return job.v1_report()


@router.get("/comparisons/{key}/pairs/{index}")
async def comparison_pair(request: Request, key: str, index: int):
    return _pair(_job(request, key), index)


@router.post("/comparisons/{key}/cancel")
async def cancel_comparison(request: Request, key: str):
    job = _job(request, key)
    request.app.state.batches.cancel(job.id)
    return job.v1_status()


@router.get("/comparisons/{key}/pairs/{index}/images/{kind}")
async def pair_image(request: Request, key: str, index: int, kind: str):
    if kind not in {"reference", "candidate", "overlay", "diff"}:
        raise ApiProblem(404, "image_not_found", "图片类型应为 reference、candidate、overlay 或 diff。")
    async with request.app.state.v1_image_lock:
        job = _job(request, key)
        entry = _pair(job, index)
        if entry["status"] != "done":
            raise ApiProblem(409, "pair_comparison_failed", "该比较项处理失败，请读取错误信息后创建新任务。")
        cache = request.app.state.v1_image_cache
        cache_key = (key, index)
        result = None
        token = cache.pop(cache_key, None)
        if token is not None:
            try:
                result = request.app.state.store.get_result(token)
            except HTTPException as error:
                if error.status_code != 404:
                    raise
        if result is None:
            try:
                result = await request.app.state.render_pair(job, index, check_cancel=False)
            except Exception:
                raise ApiProblem(422, "image_render_failed", "图片生成失败，请降低 DPI 后创建新任务。") from None
            token = request.app.state.store.add_result(result)
        cache[cache_key] = token
        while len(cache) > 4:
            cache.popitem(last=False)
        return Response(result.images[kind], media_type="image/png")
