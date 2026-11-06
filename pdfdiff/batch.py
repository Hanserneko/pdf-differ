"""共享逐页任务、源文件预算与可查询的比较报告，不缓存整本页面图像。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import secrets
import time
from typing import Awaitable, Callable

from .engine import ComparisonResult


MAX_BATCHES = 4
MAX_BATCH_REPORT_BYTES = 64 * 1024 * 1024
MAX_BATCH_SOURCE_BYTES = 128 * 1024 * 1024
TERMINAL_STATUSES = {"completed", "cancelled", "failed"}


class BatchBusyError(Exception):
    pass


class BatchCancellationRequested(Exception):
    pass


class BatchReportTooLarge(Exception):
    pass


class BatchCapacityError(Exception):
    pass


class BatchIdempotencyConflict(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _visual_difference(report: dict) -> bool:
    changed_pixels = report.get("visual", {}).get("rgb_changed_pixels")
    if not isinstance(changed_pixels, int) or changed_pixels < 0:
        raise ValueError("缺少可用的 RGB 差异计数。")
    reference, candidate = report["reference"], report["candidate"]
    geometry_changed = any(
        reference.get(key) != candidate.get(key)
        for key in ("width_mm", "height_mm", "cropbox_pt", "rotation")
    )
    return changed_pixels > 0 or geometry_changed


@dataclass
class BatchJob:
    id: str
    reference_data: bytes
    candidate_data: bytes
    total_pages: int
    files: dict
    engine: dict
    dpi: int
    ink_threshold: int
    page_pairs: tuple[tuple[int, int], ...] | None = None
    region_mm: tuple[float, float, float, float] | None = None
    retain_details: bool = False
    idempotency_key: str | None = field(default=None, repr=False)
    request_fingerprint: str | None = field(default=None, repr=False)
    ttl_seconds: int = 60 * 60
    status: str = "queued"
    current_page: int | None = None
    pages: list[dict] = field(default_factory=list)
    error: str | None = None
    cancel_requested: bool = False
    created_at: str = field(default_factory=_now)
    generated_at: str = field(default_factory=_now)
    touched: float = field(default_factory=time.monotonic)
    report_bytes: int = 0
    task: asyncio.Task | None = field(default=None, repr=False)
    error_code: str | None = None

    def touch(self):
        self.touched = time.monotonic()

    def changed(self):
        self.touch()
        self.generated_at = _now()

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def page_pair(self, index: int) -> tuple[int, int]:
        if type(index) is not int or not 1 <= index <= self.total_pages:
            raise ValueError(f"页对序号必须为 1 至 {self.total_pages}。")
        return self.page_pairs[index - 1] if self.page_pairs is not None else (index, index)

    def _summary(self) -> dict:
        successful = [page for page in self.pages if page["status"] == "done"]
        return {
            "compared_pages": len(successful),
            "changed_pages": sum(page["has_visual_difference"] is True for page in successful),
            "unchanged_pages": sum(page["has_visual_difference"] is False for page in successful),
            "error_pages": len(self.pages) - len(successful),
        }

    def report(self) -> dict:
        return {
            "schema_version": 1,
            "kind": "document",
            "status": self.status,
            "total_pages": self.total_pages,
            "completed_pages": len(self.pages),
            "summary": self._summary(),
            "pages": list(self.pages),
            "settings": {"dpi": self.dpi, "ink_threshold": self.ink_threshold, "region_mm": self.region_mm},
            "files": self.files,
            "generated_at": self.generated_at,
            "engine": self.engine,
            "error": self.error,
        }

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            **self.report(),
            "current_page": self.current_page,
            "cancel_requested": self.cancel_requested,
            "exports": {
                "json": f"/api/batches/{self.id}/report.json",
                "html": f"/api/batches/{self.id}/report.html",
                "zip": f"/api/batches/{self.id}/bundle.zip",
            },
        }

    def v1_status(self) -> dict:
        summary = self._summary()
        summaries = [page["report"].get("diagnostics", {}).get("summary", {})
                     for page in self.pages if page["status"] == "done"]
        expires_at = None
        if self.terminal:
            idle_seconds = time.monotonic() - self.touched
            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=max(0, self.ttl_seconds - idle_seconds))).isoformat()
        return {
            "schema_version": 1, "id": self.id, "status": self.status,
            "progress": {"total_pairs": self.total_pages, "processed_pairs": len(self.pages),
                         "succeeded_pairs": summary["compared_pages"], "failed_pairs": summary["error_pages"],
                         "current_pair_index": self.current_page},
            "summary": {"compared_pairs": summary["compared_pages"], "visual_changed_pairs": summary["changed_pages"],
                        "visual_unchanged_pairs": summary["unchanged_pages"], "error_pairs": summary["error_pages"],
                        "pairs_with_findings": sum(item.get("finding_count", 0) > 0 for item in summaries),
                        "finding_count": sum(item.get("finding_count", 0) for item in summaries),
                        "review_count": sum(item.get("review_count", 0) for item in summaries)},
            "cancel_requested": self.cancel_requested, "created_at": self.created_at,
            "updated_at": self.generated_at, "expires_at": expires_at,
            "links": {"self": f"/api/v1/comparisons/{self.id}", "report": f"/api/v1/comparisons/{self.id}/report",
                      "cancel": f"/api/v1/comparisons/{self.id}/cancel"},
            "error": {"code": self.error_code or "comparison_failed", "detail": self.error, "retryable": False} if self.error else None,
        }

    def _v1_entry(self, index: int, page: dict) -> dict:
        reference, candidate = self.page_pair(index)
        entry = {"index": index, "reference_page": reference, "candidate_page": candidate,
                 "status": page["status"], "has_visual_difference": page["has_visual_difference"],
                 "report": page["report"],
                 "error": {"code": "page_comparison_failed", "detail": page["error"], "retryable": False} if page["error"] else None}
        if page["status"] == "done":
            entry["images"] = {kind: f"/api/v1/comparisons/{self.id}/pairs/{index}/images/{kind}"
                               for kind in ("reference", "candidate", "overlay", "diff")}
        return entry

    def v1_pair(self, index: int) -> dict | None:
        if type(index) is not int or not 1 <= index <= len(self.pages):
            return None
        return self._v1_entry(index, self.pages[index - 1])

    def v1_report(self) -> dict:
        successful = [page["report"] for page in self.pages if page["status"] == "done"]
        failed = len(self.pages) - len(successful)
        truncated = sum(report.get("diagnostics", {}).get("truncated", False) is True for report in successful)
        return {
            **self.v1_status(), "files": self.files, "engine": self.engine,
            "settings": {"dpi": self.dpi, "ink_threshold": self.ink_threshold, "region_mm": self.region_mm},
            "page_pairs": [{"reference_page": self.page_pair(index)[0], "candidate_page": self.page_pair(index)[1]}
                           for index in range(1, self.total_pages + 1)],
            "coverage": {
                "requested_pairs": self.total_pages, "processed_pairs": len(self.pages),
                "compared_pairs": len(successful), "failed_pairs": failed,
                "unprocessed_pairs": self.total_pages - len(self.pages),
                "all_pairs_compared": len(successful) == self.total_pages,
                "diagnostics_truncated": truncated > 0, "diagnostics_truncated_pairs": truncated,
                "text_position_unavailable_pairs": sum(report.get("text", {}).get("position_measurement_available") is not True for report in successful),
                "content_comparison_incomplete_pairs": sum(report.get("diagnostics", {}).get("coverage", {}).get("content_comparison") != "completed" for report in successful),
                "truncated": truncated > 0 or self.error_code == "report_limit_exceeded",
            },
            "pairs": [self.v1_pair(index) for index in range(1, len(self.pages) + 1)],
        }


class BatchManager:
    """所有状态只由应用事件循环访问，渲染回调负责共用单线程执行器。"""

    def __init__(self, render_page: Callable[[BatchJob, int], Awaitable[ComparisonResult]], ttl_seconds: int):
        self.jobs: dict[str, BatchJob] = {}
        self.render_page = render_page
        self.ttl_seconds = ttl_seconds
        self.closed = False
        self.reservation: str | None = None

    def cleanup(self):
        cutoff = time.monotonic() - self.ttl_seconds
        for key, job in list(self.jobs.items()):
            # 运行中任务可能很慢，不能因为过期或 LRU 淘汰释放其源文件。
            if job.terminal and job.touched < cutoff:
                del self.jobs[key]

    def get(self, key: str) -> BatchJob:
        self.cleanup()
        # Windows 时钟可能让连续访问具有相同时间值，字典顺序同时记录访问先后。
        job = self.jobs.pop(key)
        job.touch()
        self.jobs[key] = job
        return job

    def reserve(self) -> str:
        self.cleanup()
        if self.closed or self.reservation is not None or any(not job.terminal for job in self.jobs.values()):
            raise BatchBusyError
        self.reservation = secrets.token_urlsafe(24)
        return self.reservation

    def release_reservation(self, token: str):
        if self.reservation == token:
            self.reservation = None

    def idempotent_job(self, key: str | None, fingerprint: str | None) -> BatchJob | None:
        self.cleanup()
        if key is None:
            return None
        for job in self.jobs.values():
            if job.idempotency_key == key:
                if job.request_fingerprint != fingerprint:
                    raise BatchIdempotencyConflict
                return self.get(job.id)
        return None

    def create(self, *, reference_data: bytes, candidate_data: bytes, total_pages: int, files: dict, engine: dict,
               dpi: int, ink_threshold: int, reservation: str | None = None,
               page_pairs: tuple[tuple[int, int], ...] | None = None,
               region_mm: tuple[float, float, float, float] | None = None,
               retain_details: bool = False, idempotency_key: str | None = None,
               request_fingerprint: str | None = None) -> BatchJob:
        self.cleanup()
        existing = self.idempotent_job(idempotency_key, request_fingerprint)
        if existing is not None:
            if reservation is not None:
                self.release_reservation(reservation)
            return existing
        if (self.closed or any(not job.terminal for job in self.jobs.values())
                or self.reservation != reservation):
            raise BatchBusyError
        if type(total_pages) is not int or total_pages < 1:
            raise ValueError("比较页对数量必须大于零。")
        if page_pairs is not None:
            page_pairs = tuple(tuple(pair) for pair in page_pairs)
            if len(page_pairs) != total_pages or any(len(pair) != 2 or any(type(page) is not int or page < 1 for page in pair) for pair in page_pairs):
                raise ValueError("页对映射必须包含指定数量的正整数页码。")
        if region_mm is not None and total_pages != 1:
            raise ValueError("局部选区仅支持单个页对。")
        if idempotency_key is not None and request_fingerprint is None:
            raise ValueError("幂等请求必须提供请求指纹。")
        source_bytes = len(reference_data) + len(candidate_data)
        if source_bytes > MAX_BATCH_SOURCE_BYTES:
            raise BatchCapacityError
        used_bytes = sum(len(job.reference_data) + len(job.candidate_data) for job in self.jobs.values())
        while len(self.jobs) >= MAX_BATCHES or used_bytes + source_bytes > MAX_BATCH_SOURCE_BYTES:
            removable = [job for job in self.jobs.values() if job.terminal]
            if not removable:
                raise BatchCapacityError
            oldest = min(removable, key=lambda job: job.touched)
            used_bytes -= len(oldest.reference_data) + len(oldest.candidate_data)
            del self.jobs[oldest.id]
        job = BatchJob(
            id=secrets.token_urlsafe(24),
            reference_data=reference_data,
            candidate_data=candidate_data,
            total_pages=total_pages,
            files=deepcopy(files),
            engine=deepcopy(engine),
            dpi=dpi,
            ink_threshold=ink_threshold,
            page_pairs=page_pairs,
            region_mm=tuple(region_mm) if region_mm is not None else None,
            retain_details=retain_details,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            ttl_seconds=self.ttl_seconds,
        )
        # 预留状态、字段分隔和最终错误消息的空间，再累计各页序列化体积。
        header = job.v1_report() if retain_details else job.report()
        job.report_bytes = len(json.dumps(header, ensure_ascii=False, allow_nan=False).encode("utf-8")) + 4096
        self.jobs[job.id] = job
        job.task = asyncio.create_task(self._run(job), name=f"pdf-batch-{job.id}")
        if reservation is not None:
            self.release_reservation(reservation)
        return job

    def cancel(self, key: str) -> BatchJob:
        job = self.get(key)
        if not job.terminal:
            job.cancel_requested = True
            job.changed()
        return job

    def _append_page(self, job: BatchJob, page: dict):
        entry = job._v1_entry(len(job.pages) + 1, page) if job.retain_details else page
        size = len(json.dumps(entry, ensure_ascii=False, allow_nan=False).encode("utf-8")) + 2
        if job.report_bytes + size > MAX_BATCH_REPORT_BYTES:
            raise BatchReportTooLarge
        job.pages.append(page)
        job.report_bytes += size
        job.changed()

    async def _run(self, job: BatchJob):
        try:
            job.status = "running"
            job.changed()
            for page_number in range(1, job.total_pages + 1):
                if job.cancel_requested:
                    break
                job.current_page = page_number
                job.changed()
                result = None
                try:
                    result = await self.render_page(job, page_number)
                    summary = deepcopy({key: value for key, value in result.report.items()
                                        if job.retain_details or key != "line_differences"})
                    summary["detail_level"] = "full" if job.retain_details else "summary"
                    entry = {
                        "page": page_number,
                        "status": "done",
                        "has_visual_difference": _visual_difference(summary),
                        "report": summary,
                        "error": None,
                    }
                    # 当前页 PNG 到此即可释放，整本摘要不持有 ComparisonResult。
                except BatchCancellationRequested:
                    break
                except MemoryError:
                    entry = {"page": page_number, "status": "error", "has_visual_difference": None, "report": None, "error": "该页比较超出可用内存，请降低 DPI 后重试。"}
                except Exception:
                    # 外部库错误可能包含路径和解析细节，不将其直接返回给浏览器。
                    entry = {"page": page_number, "status": "error", "has_visual_difference": None, "report": None, "error": "该页比较失败，请检查页面内容、尺寸或降低 DPI 后重试。"}
                finally:
                    result = None
                self._append_page(job, entry)
                # 每页释放 gate 后主动让出事件循环，允许详情请求和取消及时排队。
                await asyncio.sleep(0)
            job.status = "cancelled" if job.cancel_requested else "completed"
        except BatchReportTooLarge:
            job.status = "failed"
            job.error = "比较报告超过 64 MiB，任务已停止并保留已完成的页面结果。"
            job.error_code = "report_limit_exceeded"
        except asyncio.CancelledError:
            job.cancel_requested = True
            job.status = "cancelled"
            raise
        except Exception:
            job.status = "failed"
            job.error = "批量任务未能继续，已保留完成的页面结果。"
            job.error_code = "comparison_failed"
        finally:
            job.current_page = None
            job.changed()

    async def shutdown(self):
        self.closed = True
        self.reservation = None
        tasks = []
        for job in self.jobs.values():
            if not job.terminal:
                job.cancel_requested = True
            if job.task is not None:
                tasks.append(job.task)
        if tasks:
            # 协作取消会等当前渲染结束，避免线程仍在使用源文件时关闭执行器。
            await asyncio.gather(*tasks, return_exceptions=True)
        self.jobs.clear()
