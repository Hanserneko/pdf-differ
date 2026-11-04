"""逐页比较任务只保留摘要和源文件引用，不缓存整本页面图像。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import secrets
import time
from typing import Awaitable, Callable

from .engine import ComparisonResult


MAX_BATCHES = 4
MAX_BATCH_REPORT_BYTES = 64 * 1024 * 1024
TERMINAL_STATUSES = {"completed", "cancelled", "failed"}


class BatchBusyError(Exception):
    pass


class BatchCancellationRequested(Exception):
    pass


class BatchReportTooLarge(Exception):
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
    status: str = "queued"
    current_page: int | None = None
    pages: list[dict] = field(default_factory=list)
    error: str | None = None
    cancel_requested: bool = False
    generated_at: str = field(default_factory=_now)
    touched: float = field(default_factory=time.monotonic)
    report_bytes: int = 0
    task: asyncio.Task | None = field(default=None, repr=False)

    def touch(self):
        self.touched = time.monotonic()

    def changed(self):
        self.touch()
        self.generated_at = _now()

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def report(self) -> dict:
        successful = [page for page in self.pages if page["status"] == "done"]
        return {
            "schema_version": 1,
            "kind": "document",
            "status": self.status,
            "total_pages": self.total_pages,
            "completed_pages": len(self.pages),
            "summary": {
                "compared_pages": len(successful),
                "changed_pages": sum(page["has_visual_difference"] is True for page in successful),
                "unchanged_pages": sum(page["has_visual_difference"] is False for page in successful),
                "error_pages": len(self.pages) - len(successful),
            },
            "pages": list(self.pages),
            "settings": {"dpi": self.dpi, "ink_threshold": self.ink_threshold, "region_mm": None},
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


class BatchManager:
    """所有状态只由应用事件循环访问，渲染回调负责共用单线程执行器。"""

    def __init__(self, render_page: Callable[[BatchJob, int], Awaitable[ComparisonResult]], ttl_seconds: int):
        self.jobs: dict[str, BatchJob] = {}
        self.render_page = render_page
        self.ttl_seconds = ttl_seconds
        self.closed = False

    def cleanup(self):
        cutoff = time.monotonic() - self.ttl_seconds
        for key, job in list(self.jobs.items()):
            # 运行中任务可能很慢，不能因为过期或 LRU 淘汰释放其源文件。
            if job.terminal and job.touched < cutoff:
                del self.jobs[key]

    def get(self, key: str) -> BatchJob:
        self.cleanup()
        job = self.jobs[key]
        job.touch()
        return job

    def create(self, *, reference_data: bytes, candidate_data: bytes, total_pages: int, files: dict, engine: dict, dpi: int, ink_threshold: int) -> BatchJob:
        self.cleanup()
        if self.closed or any(not job.terminal for job in self.jobs.values()):
            raise BatchBusyError
        while len(self.jobs) >= MAX_BATCHES:
            removable = [job for job in self.jobs.values() if job.terminal]
            if not removable:
                raise BatchBusyError
            oldest = min(removable, key=lambda job: job.touched)
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
        )
        # 预留状态、字段分隔和最终错误消息的空间，再累计各页序列化体积。
        job.report_bytes = len(json.dumps(job.report(), ensure_ascii=False, allow_nan=False).encode("utf-8")) + 4096
        self.jobs[job.id] = job
        job.task = asyncio.create_task(self._run(job), name=f"pdf-batch-{job.id}")
        return job

    def cancel(self, key: str) -> BatchJob:
        job = self.get(key)
        if not job.terminal:
            job.cancel_requested = True
            job.changed()
        return job

    def _append_page(self, job: BatchJob, page: dict):
        size = len(json.dumps(page, ensure_ascii=False, allow_nan=False).encode("utf-8")) + 2
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
                    summary = deepcopy({key: value for key, value in result.report.items() if key != "line_differences"})
                    summary["detail_level"] = "summary"
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
            job.error = "批量摘要报告超过 64 MiB，任务已停止并保留已完成的页面结果。"
        except asyncio.CancelledError:
            job.cancel_requested = True
            job.status = "cancelled"
            raise
        except Exception:
            job.status = "failed"
            job.error = "批量任务未能继续，已保留完成的页面结果。"
        finally:
            job.current_page = None
            job.changed()

    async def shutdown(self):
        self.closed = True
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
