"""只在本机提供文件、比较结果和静态界面，不持久化原始 PDF。"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import secrets
import time
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from .batch import BatchBusyError, BatchCancellationRequested, BatchCapacityError, BatchJob, BatchManager
from .api_v1 import ApiProblem, problem_handler, problem_response, router as v1_router
from .demo import make_demo_pair
from .engine import ComparisonResult, compare_pages, inspect_pdf
from .reporting import build_bundle, build_html


MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_DOCUMENT_BYTES = 128 * 1024 * 1024
MAX_DOCUMENTS = 12
MAX_RESULTS = 4
TTL_SECONDS = 60 * 60
STATIC = Path(__file__).parent / "static"


@dataclass
class Document:
    data: bytes
    metadata: dict
    touched: float


@dataclass
class StoredComparison:
    result: ComparisonResult
    touched: float


class MemoryStore:
    """由事件循环串行访问；PDF 解析与渲染统一交给单线程执行器。"""

    def __init__(self):
        self.documents: dict[str, Document] = {}
        self.results: dict[str, StoredComparison] = {}

    def cleanup(self):
        cutoff = time.monotonic() - TTL_SECONDS
        for mapping in (self.documents, self.results):
            for key in list(mapping):
                if mapping[key].touched < cutoff:
                    del mapping[key]

    def add_document(self, data: bytes, filename: str, info: dict) -> dict:
        self.cleanup()
        used = sum(len(document.data) for document in self.documents.values())
        while self.documents and (
            len(self.documents) >= MAX_DOCUMENTS or used + len(data) > MAX_DOCUMENT_BYTES
        ):
            oldest = min(self.documents, key=lambda key: self.documents[key].touched)
            used -= len(self.documents.pop(oldest).data)
        key = secrets.token_urlsafe(24)
        metadata = {**info, "id": key, "filename": filename}
        self.documents[key] = Document(data, metadata, time.monotonic())
        return metadata

    def get_document(self, key: str) -> Document:
        self.cleanup()
        document = self.documents.get(key)
        if document is None:
            raise HTTPException(404, "文件已过期或不存在，请重新选择 PDF。")
        document.touched = time.monotonic()
        return document

    def add_result(self, result: ComparisonResult) -> str:
        self.cleanup()
        while len(self.results) >= MAX_RESULTS:
            oldest = min(self.results, key=lambda key: self.results[key].touched)
            del self.results[oldest]
        key = secrets.token_urlsafe(24)
        self.results[key] = StoredComparison(result, time.monotonic())
        return key

    def get_result(self, key: str) -> ComparisonResult:
        self.cleanup()
        stored = self.results.get(key)
        if stored is None:
            raise HTTPException(404, "比较结果已过期，请重新比较。")
        stored.touched = time.monotonic()
        return stored.result


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.store = MemoryStore()
    configured_roots = getattr(application.state, "configured_roots", (Path.cwd(),))
    application.state.allowed_roots = tuple(Path(root).resolve() for root in configured_roots)
    application.state.v1_image_lock = asyncio.Lock()
    application.state.v1_image_cache = OrderedDict()
    # MuPDF 不适合多个线程同时调用；所有相关操作固定在同一个工作线程。
    application.state.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdf-engine")
    application.state.comparison_gate = asyncio.Semaphore(1)

    async def call_engine(function, *args, **kwargs):
        return await application_engine_call(application, function, *args, **kwargs)

    application.state.call_engine = call_engine
    application.state.engine_versions = engine_versions

    async def render_batch_page(job: BatchJob, page: int, *, check_cancel: bool = True):
        async with application.state.comparison_gate:
            if check_cancel and job.cancel_requested:
                raise BatchCancellationRequested
            reference_page, candidate_page = job.page_pair(page)
            return await application_engine_call(
                application,
                compare_pages,
                job.reference_data,
                job.candidate_data,
                reference_page=reference_page,
                candidate_page=candidate_page,
                dpi=job.dpi,
                ink_threshold=job.ink_threshold,
                region_mm=job.region_mm,
            )

    application.state.render_pair = render_batch_page
    application.state.batches = BatchManager(render_batch_page, TTL_SECONDS)

    async def expire_cache():
        while True:
            await asyncio.sleep(60)
            application.state.store.cleanup()
            application.state.batches.cleanup()

    cleanup_task = asyncio.create_task(expire_cache())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        await application.state.batches.shutdown()
        application.state.worker.shutdown(wait=True, cancel_futures=True)
        application.state.store.documents.clear()
        application.state.store.results.clear()
        application.state.v1_image_cache.clear()


app = FastAPI(title="PDF Layout Diff", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(
    TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"]
)
app.add_exception_handler(ApiProblem, problem_handler)
app.include_router(v1_router)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, error: StarletteHTTPException):
    if request.url.path.startswith("/api/v1/"):
        code = {404: "route_not_found", 405: "method_not_allowed"}.get(error.status_code, "http_error")
        response = problem_response(request, ApiProblem(error.status_code, code, str(error.detail)))
        if error.headers:
            response.headers.update(error.headers)
        return response
    return await http_exception_handler(request, error)


@app.exception_handler(RequestValidationError)
async def invalid_parameters(request: Request, error: RequestValidationError):
    if request.url.path.startswith("/api/v1/"):
        return problem_response(request, ApiProblem(422, "invalid_request", "请求格式或参数无效，请检查来源、页面配对与测量参数。"))
    # 不回显输入中的 NaN、二进制或任意大数据，保持错误响应本身也可安全序列化。
    fields = ", ".join(dict.fromkeys(str(item["loc"][-1]) for item in error.errors()))
    return JSONResponse({"detail": f"参数无效：{fields}。请检查页码、DPI 和选区坐标。"}, status_code=422)


@app.middleware("http")
async def local_access(request: Request, call_next):
    # 防止外部网页借助本机服务读取结果或提交运算；本地界面使用同源请求。
    if request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") == "cross-site" or (
            origin and origin != f"{request.url.scheme}://{request.headers.get('host')}"
        ):
            if request.url.path.startswith("/api/v1/"):
                return problem_response(request, ApiProblem(403, "origin_not_allowed", "请求来源不受允许。"))
            return Response(
                json.dumps({"detail": "仅允许本机界面发起同源请求。"}, ensure_ascii=False),
                status_code=403,
                media_type="application/json",
            )
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def safe_filename(filename: str) -> str:
    cleaned = filename.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(character for character in cleaned if ord(character) >= 32)
    return cleaned[:180] or "document.pdf"


async def engine_call(request: Request, function, *args, **kwargs):
    return await application_engine_call(request.app, function, *args, **kwargs)


async def application_engine_call(application: FastAPI, function, *args, **kwargs):
    from functools import partial

    return await asyncio.get_running_loop().run_in_executor(
        application.state.worker, partial(function, *args, **kwargs)
    )


def engine_versions() -> dict:
    return {
        "application": "0.1.0",
        "pymupdf": version("PyMuPDF"),
        "numpy": version("numpy"),
        "opencv": version("opencv-python-headless"),
    }


def files_metadata(reference: Document, candidate: Document) -> dict:
    return {
        "reference": {"filename": reference.metadata["filename"], "sha256": hashlib.sha256(reference.data).hexdigest()},
        "candidate": {"filename": candidate.metadata["filename"], "sha256": hashlib.sha256(candidate.data).hexdigest()},
    }


def comparison_payload(store: MemoryStore, result: ComparisonResult, files: dict) -> dict:
    result.report["files"] = files
    result.report["generated_at"] = datetime.now(timezone.utc).isoformat()
    result.report["engine"] = engine_versions()
    key = store.add_result(result)
    return {
        "id": key,
        "report": result.report,
        "images": {kind: f"/api/comparisons/{key}/images/{kind}" for kind in result.images},
        "exports": {
            "json": f"/api/comparisons/{key}/report.json",
            "html": f"/api/comparisons/{key}/report.html",
            "zip": f"/api/comparisons/{key}/bundle.zip",
        },
    }


async def render_comparison(request: Request, reference_data: bytes, candidate_data: bytes, files: dict, **settings) -> dict:
    async with request.app.state.comparison_gate:
        try:
            result = await engine_call(request, compare_pages, reference_data, candidate_data, **settings)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        except MemoryError as error:
            raise HTTPException(400, "本次比较超出可用内存，请降低 DPI 或使用更小的页面。") from error
        except Exception as error:
            raise HTTPException(400, "比较失败，请检查文件或降低 DPI 后重试。") from error
        return comparison_payload(request.app.state.store, result, files)


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


@app.post("/api/documents", status_code=201)
async def upload_document(request: Request, filename: str = "document.pdf"):
    header_length = request.headers.get("content-length")
    if header_length:
        try:
            length = int(header_length)
        except ValueError as error:
            raise HTTPException(400, "文件大小无效。") from error
        if length > MAX_FILE_BYTES:
            raise HTTPException(413, "单个 PDF 最大支持 32 MB。")
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_FILE_BYTES:
            raise HTTPException(413, "单个 PDF 最大支持 32 MB。")
    data = bytes(content)
    if b"%PDF-" not in data[:1024]:
        raise HTTPException(400, "这不是有效的 PDF 文件。")
    try:
        info = await engine_call(request, inspect_pdf, data)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except Exception as error:
        raise HTTPException(400, "无法读取 PDF，文件可能损坏或不受支持。") from error
    return request.app.state.store.add_document(data, safe_filename(filename), info)


@app.post("/api/demo")
async def demo(request: Request):
    def prepare():
        reference, candidate = make_demo_pair()
        return reference, candidate, inspect_pdf(reference), inspect_pdf(candidate)

    reference, candidate, reference_info, candidate_info = await engine_call(request, prepare)
    store = request.app.state.store
    return {
        "reference": store.add_document(reference, "示例_参考文档.pdf", reference_info),
        "candidate": store.add_document(candidate, "示例_待比较文档.pdf", candidate_info),
    }


class CompareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    reference_id: str = Field(min_length=1, max_length=100)
    candidate_id: str = Field(min_length=1, max_length=100)
    reference_page: int = Field(default=1, ge=1)
    candidate_page: int = Field(default=1, ge=1)
    dpi: Literal[96, 150, 200, 300, 600] = 150
    ink_threshold: int = Field(default=220, ge=1, le=254)
    region_mm: tuple[float, float, float, float] | None = None


@app.post("/api/compare")
async def compare(request: Request, settings: CompareRequest):
    store = request.app.state.store
    reference = store.get_document(settings.reference_id)
    candidate = store.get_document(settings.candidate_id)
    return await render_comparison(
        request,
        reference.data,
        candidate.data,
        files_metadata(reference, candidate),
        reference_page=settings.reference_page,
        candidate_page=settings.candidate_page,
        dpi=settings.dpi,
        ink_threshold=settings.ink_threshold,
        region_mm=settings.region_mm,
    )


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    reference_id: str = Field(min_length=1, max_length=100)
    candidate_id: str = Field(min_length=1, max_length=100)
    dpi: Literal[96, 150, 200, 300, 600] = 150
    ink_threshold: int = Field(default=220, ge=1, le=254)


def get_batch(request: Request, key: str) -> BatchJob:
    try:
        return request.app.state.batches.get(key)
    except KeyError as error:
        raise HTTPException(404, "批量任务已过期或不存在，请重新比较。") from error


@app.post("/api/batches", status_code=202)
async def start_batch(request: Request, settings: BatchRequest):
    reference = request.app.state.store.get_document(settings.reference_id)
    candidate = request.app.state.store.get_document(settings.candidate_id)
    if reference.metadata["page_count"] != candidate.metadata["page_count"]:
        raise HTTPException(400, "全页比较要求两份 PDF 页数相同；页数不同时请使用单页比较。")
    try:
        job = request.app.state.batches.create(
            reference_data=reference.data,
            candidate_data=candidate.data,
            total_pages=reference.metadata["page_count"],
            files=files_metadata(reference, candidate),
            engine=engine_versions(),
            dpi=settings.dpi,
            ink_threshold=settings.ink_threshold,
        )
    except BatchBusyError as error:
        raise HTTPException(409, "已有全页比较任务正在运行，请等待完成或先取消该任务。") from error
    except BatchCapacityError as error:
        raise HTTPException(413, "源文件引用超出任务缓存容量，请减少文件大小。") from error
    return job.snapshot()


@app.get("/api/batches/{key}")
async def batch_status(request: Request, key: str):
    return get_batch(request, key).snapshot()


@app.delete("/api/batches/{key}")
async def cancel_batch(request: Request, key: str):
    get_batch(request, key)
    return request.app.state.batches.cancel(key).snapshot()


@app.post("/api/batches/{key}/pages/{page}/compare")
async def compare_batch_page(request: Request, key: str, page: int):
    job = get_batch(request, key)
    if not 1 <= page <= job.total_pages:
        raise HTTPException(400, f"页码必须为 1 至 {job.total_pages}。")
    entry = next((item for item in job.pages if item["page"] == page), None)
    if entry is None:
        raise HTTPException(409, "该页尚未完成比较，请等待进度更新。")
    if entry["status"] != "done":
        raise HTTPException(400, "该页在全页比较中未成功，暂时无法打开详情；可使用单页比较重试。")
    reference_page, candidate_page = job.page_pair(page)
    return await render_comparison(
        request,
        job.reference_data,
        job.candidate_data,
        job.files,
        reference_page=reference_page,
        candidate_page=candidate_page,
        dpi=job.dpi,
        ink_threshold=job.ink_threshold,
        region_mm=job.region_mm,
    )


@app.get("/api/batches/{key}/report.json")
async def batch_json(request: Request, key: str):
    report = get_batch(request, key).report()
    return Response(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="pdf-diff-document-report.json"'},
    )


@app.get("/api/batches/{key}/report.html")
async def batch_html(request: Request, key: str):
    job = get_batch(request, key)
    if not job.terminal:
        raise HTTPException(409, "任务仍在运行，请完成或取消后导出 HTML；当前进度可以导出 JSON。")
    from .batch_reporting import build_batch_html

    payload = await engine_call(request, build_batch_html, job.report())
    return Response(
        payload,
        media_type="text/html",
        headers={"Content-Disposition": 'attachment; filename="pdf-diff-document-report.html"'},
    )


@app.get("/api/batches/{key}/bundle.zip")
async def batch_bundle(request: Request, key: str):
    job = get_batch(request, key)
    if not job.terminal:
        raise HTTPException(409, "任务仍在运行，请完成或取消后导出 ZIP；当前进度可以导出 JSON。")
    from .batch_reporting import build_batch_bundle

    payload = await engine_call(request, build_batch_bundle, job.report())
    return Response(
        payload,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="pdf-diff-document-report.zip"'},
    )


@app.get("/api/comparisons/{key}/images/{kind}")
async def comparison_image(request: Request, key: str, kind: str):
    result = request.app.state.store.get_result(key)
    if kind not in result.images:
        raise HTTPException(404, "图像不存在。")
    return Response(
        result.images[kind],
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="{kind}.png"'},
    )


@app.get("/api/comparisons/{key}/report.json")
async def report_json(request: Request, key: str):
    result = request.app.state.store.get_result(key)
    return Response(
        json.dumps(result.report, ensure_ascii=False, indent=2, allow_nan=False),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="pdf-diff-report.json"'},
    )


@app.get("/api/comparisons/{key}/report.html")
async def report_html(request: Request, key: str):
    result = request.app.state.store.get_result(key)
    return Response(
        build_html(result.report, result.images),
        media_type="text/html",
        headers={"Content-Disposition": 'attachment; filename="pdf-diff-report.html"'},
    )


@app.get("/api/comparisons/{key}/bundle.zip")
async def report_bundle(request: Request, key: str):
    result = request.app.state.store.get_result(key)
    payload = await engine_call(request, build_bundle, result.report, result.images)
    return Response(
        payload,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="pdf-diff-report.zip"'},
    )


app.mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="static")
