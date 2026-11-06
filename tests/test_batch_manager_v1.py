"""验证共享任务管理器的页对、容量、预约与报告完整性。"""

import asyncio
from datetime import datetime, timezone
import json
import time

import pytest

from pdfdiff import batch
from pdfdiff.batch import BatchBusyError, BatchCapacityError, BatchIdempotencyConflict, BatchManager
from pdfdiff.engine import ComparisonResult


def result(reference=1, candidate=1, *, changed=True, truncated=False, text_available=True):
    page = {"width_mm": 100, "height_mm": 100, "cropbox_pt": [0, 0, 100, 100], "rotation": 0}
    report = {
        "reference": {**page, "page": reference}, "candidate": {**page, "page": candidate},
        "visual": {"rgb_changed_pixels": 1 if changed else 0},
        "text": {"position_measurement_available": text_available},
        "line_differences": [{"text": "A complete matched line", "dx_mm": 1}],
        "diagnostics": {"truncated": truncated,
                        "summary": {"finding_count": 2 if changed else 0, "review_count": 1 if changed else 0},
                        "coverage": {"content_comparison": "completed" if text_available else "no_extractable_text"},
                        "findings": [{"id": "example", "observation": "A detailed observation"}] if changed else []},
    }
    return ComparisonResult(report, {"reference": b"large temporary pixels"})


def settings(**changes):
    return {"reference_data": b"ref", "candidate_data": b"candidate", "total_pages": 1,
            "files": {"reference": {"filename": "reference.pdf"}, "candidate": {"filename": "candidate.pdf"}},
            "engine": {"application": "0.1.0"}, "dpi": 96, "ink_threshold": 220, **changes}


async def render(job, index):
    return result(*job.page_pair(index))


async def finish(job):
    await asyncio.wait_for(job.task, 2)


def test_explicit_page_pairs_keep_complete_report_and_light_status():
    async def scenario():
        calls = []

        async def record(job, index):
            calls.append((index, job.page_pair(index), job.region_mm))
            return result(*job.page_pair(index))

        manager = BatchManager(record, 3600)
        job = manager.create(**settings(total_pages=2, page_pairs=((3, 1), (1, 4)), retain_details=True))
        assert job.v1_status()["expires_at"] is None
        await finish(job)
        assert calls == [(1, (3, 1), None), (2, (1, 4), None)]
        status = job.v1_status()
        assert not {"pairs", "pages", "files", "page_pairs"} & status.keys()
        assert status["progress"] == {"total_pairs": 2, "processed_pairs": 2, "succeeded_pairs": 2,
                                      "failed_pairs": 0, "current_pair_index": None}
        assert status["summary"]["finding_count"] == 4
        assert status["summary"]["review_count"] == 2
        assert status["summary"]["pairs_with_findings"] == 2
        assert datetime.fromisoformat(status["expires_at"]) > datetime.now(timezone.utc)
        report = job.v1_report()
        assert report["page_pairs"] == [{"reference_page": 3, "candidate_page": 1}, {"reference_page": 1, "candidate_page": 4}]
        assert report["coverage"]["all_pairs_compared"]
        for index, pair in enumerate(report["pairs"], 1):
            assert pair == job.v1_pair(index)
            assert pair["report"]["detail_level"] == "full"
            assert pair["report"]["line_differences"][0]["text"] == "A complete matched line"
            assert set(pair["images"]) == {"reference", "candidate", "overlay", "diff"}
            assert f"/pairs/{index}/images/reference" in pair["images"]["reference"]
            assert "images" not in job.pages[index - 1]
        assert len(json.dumps(report, ensure_ascii=False, allow_nan=False).encode("utf-8")) <= job.report_bytes
        assert job.v1_pair(0) is None and job.v1_pair(3) is None and job.v1_pair(True) is None
        await manager.shutdown()
    asyncio.run(scenario())


def test_single_pair_region_and_original_browser_report_remain_usable():
    async def scenario():
        manager = BatchManager(render, 20)
        legacy = manager.create(**settings(total_pages=2))
        await finish(legacy)
        assert legacy.page_pair(2) == (2, 2)
        assert legacy.report()["settings"]["region_mm"] is None
        assert legacy.snapshot()["summary"] == {"compared_pages": 2, "changed_pages": 2, "unchanged_pages": 0, "error_pages": 0}
        assert all("line_differences" not in page["report"] and page["report"]["detail_level"] == "summary" for page in legacy.pages)
        roi = manager.create(**settings(page_pairs=((4, 2),), region_mm=(10, 10, 20, 20), retain_details=True))
        await finish(roi)
        assert roi.page_pair(1) == (4, 2)
        assert roi.v1_report()["settings"]["region_mm"] == (10, 10, 20, 20)
        with pytest.raises(ValueError):
            manager.create(**settings(total_pages=2, region_mm=(10, 10, 20, 20)))
        await manager.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [
    {"total_pages": 0}, {"total_pages": True},
    {"total_pages": 2, "page_pairs": ((1, 2),)},
    {"page_pairs": ((0, 1),)}, {"page_pairs": ((1, True),)},
    {"page_pairs": ((1, 2, 3),)}, {"idempotency_key": "key"},
])
def test_invalid_internal_job_settings_do_not_create_records(changes):
    async def scenario():
        manager = BatchManager(render, 20)
        with pytest.raises(ValueError):
            manager.create(**settings(**changes))
        assert manager.jobs == {}
        await manager.shutdown()
    asyncio.run(scenario())


def test_creation_reservation_excludes_both_entry_points_and_only_owner_can_release():
    async def scenario():
        manager = BatchManager(render, 20)
        reservation = manager.reserve()
        with pytest.raises(BatchBusyError):
            manager.reserve()
        with pytest.raises(BatchBusyError):
            manager.create(**settings())
        manager.release_reservation("unrelated-token")
        assert manager.reservation == reservation
        with pytest.raises(BatchBusyError):
            manager.create(**settings(reservation="unrelated-token"))
        job = manager.create(**settings(reservation=reservation))
        assert manager.reservation is None
        with pytest.raises(BatchBusyError):
            manager.reserve()
        await finish(job)
        second = manager.reserve()
        manager.release_reservation(second)
        assert manager.reservation is None
        await manager.shutdown()
        with pytest.raises(BatchBusyError):
            manager.reserve()
    asyncio.run(scenario())


def test_idempotency_replays_active_and_finished_jobs_and_expires_with_job():
    async def scenario():
        manager = BatchManager(render, 20)
        job = manager.create(**settings(idempotency_key="retry-key", request_fingerprint="same-settings"))
        assert manager.idempotent_job("retry-key", "same-settings") is job
        assert manager.create(**settings(idempotency_key="retry-key", request_fingerprint="same-settings")) is job
        with pytest.raises(BatchIdempotencyConflict):
            manager.idempotent_job("retry-key", "different-settings")
        await finish(job)
        reservation = manager.reserve()
        assert manager.create(**settings(idempotency_key="retry-key", request_fingerprint="same-settings", reservation=reservation)) is job
        assert manager.reservation is None and len(manager.jobs) == 1
        job.touched = time.monotonic() - 21
        manager.cleanup()
        assert manager.idempotent_job("retry-key", "same-settings") is None
        replacement = manager.create(**settings(idempotency_key="retry-key", request_fingerprint="same-settings"))
        assert replacement.id != job.id
        await finish(replacement)
        await manager.shutdown()
    asyncio.run(scenario())


def test_source_byte_budget_evicts_terminal_lru_and_rejects_unfulfillable_job(monkeypatch):
    async def scenario():
        monkeypatch.setattr(batch, "MAX_BATCH_SOURCE_BYTES", 24)
        manager = BatchManager(render, 3600)
        jobs = []
        for _ in range(3):
            job = manager.create(**settings(reference_data=b"aaaa", candidate_data=b"bbbb"))
            await finish(job)
            jobs.append(job)
        manager.get(jobs[0].id)
        newest = manager.create(**settings(reference_data=b"cccc", candidate_data=b"dddd"))
        await finish(newest)
        assert set(manager.jobs) == {jobs[0].id, jobs[2].id, newest.id}
        assert sum(len(job.reference_data) + len(job.candidate_data) for job in manager.jobs.values()) == 24
        retained = set(manager.jobs)
        with pytest.raises(BatchCapacityError):
            manager.create(**settings(reference_data=b"a" * 25))
        assert set(manager.jobs) == retained
        await manager.shutdown()
    asyncio.run(scenario())


def test_four_job_capacity_still_applies_and_evicted_key_can_be_reused():
    async def scenario():
        manager = BatchManager(render, 3600)
        first = manager.create(**settings(idempotency_key="old", request_fingerprint="fp"))
        await finish(first)
        for _ in range(4):
            job = manager.create(**settings())
            await finish(job)
        assert len(manager.jobs) == 4 and first.id not in manager.jobs
        assert manager.idempotent_job("old", "fp") is None
        await manager.shutdown()
    asyncio.run(scenario())


def test_partial_errors_and_incomplete_diagnostics_have_explicit_coverage():
    async def scenario():
        async def partial(job, index):
            if index == 2:
                raise ValueError("E:/private/internal/filename.pdf")
            return result(*job.page_pair(index), changed=index == 1, truncated=index == 1, text_available=index == 1)

        manager = BatchManager(partial, 3600)
        job = manager.create(**settings(total_pages=3, page_pairs=((2, 1), (4, 3), (1, 5)), retain_details=True))
        await finish(job)
        report = job.v1_report()
        assert report["status"] == "completed"
        assert report["summary"] == {"compared_pairs": 2, "visual_changed_pairs": 1, "visual_unchanged_pairs": 1,
                                     "error_pairs": 1, "pairs_with_findings": 1, "finding_count": 2, "review_count": 1}
        assert report["coverage"] == {
            "requested_pairs": 3, "processed_pairs": 3, "compared_pairs": 2, "failed_pairs": 1,
            "unprocessed_pairs": 0, "all_pairs_compared": False, "diagnostics_truncated": True,
            "diagnostics_truncated_pairs": 1, "text_position_unavailable_pairs": 1,
            "content_comparison_incomplete_pairs": 1, "truncated": True,
        }
        error = job.v1_pair(2)
        assert error["error"]["code"] == "page_comparison_failed"
        assert error["error"]["retryable"] is False
        assert error["report"] is None and "images" not in error
        assert "private" not in json.dumps(report)
        assert job.pages[1]["error"] == error["error"]["detail"]
        await manager.shutdown()
    asyncio.run(scenario())


def test_cancellation_waits_for_current_pair_and_never_compares_remaining_pairs():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def controlled(job, index):
            calls.append(index)
            entered.set()
            await release.wait()
            return result(*job.page_pair(index))

        manager = BatchManager(controlled, 20)
        job = manager.create(**settings(total_pages=3, retain_details=True))
        await asyncio.wait_for(entered.wait(), 2)
        manager.cancel(job.id)
        assert not job.task.done()
        release.set()
        await finish(job)
        assert calls == [1] and job.status == "cancelled"
        report = job.v1_report()
        assert report["progress"]["processed_pairs"] == 1
        assert report["coverage"]["unprocessed_pairs"] == 2
        assert not report["coverage"]["all_pairs_compared"]
        assert job.v1_pair(2) is None
        await manager.shutdown()
    asyncio.run(scenario())


def test_report_budget_retains_previous_pairs_and_exposes_failure_code(monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def controlled(job, index):
            if index == 2:
                entered.set()
                await release.wait()
            return result(*job.page_pair(index))

        manager = BatchManager(controlled, 3600)
        job = manager.create(**settings(total_pages=2, retain_details=True))
        await asyncio.wait_for(entered.wait(), 2)
        assert len(job.pages) == 1
        monkeypatch.setattr(batch, "MAX_BATCH_REPORT_BYTES", job.report_bytes + 8)
        release.set()
        await finish(job)
        report = job.v1_report()
        assert report["status"] == "failed"
        assert report["error"]["code"] == "report_limit_exceeded"
        assert len(report["pairs"]) == 1
        assert report["coverage"]["truncated"]
        assert report["coverage"]["unprocessed_pairs"] == 1
        assert report["pairs"][0]["report"]["line_differences"]
        await manager.shutdown()
    asyncio.run(scenario())


def test_active_jobs_do_not_expire_and_shutdown_clears_reservations():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def controlled(job, index):
            entered.set()
            await release.wait()
            return result()

        manager = BatchManager(controlled, 1)
        job = manager.create(**settings())
        await asyncio.wait_for(entered.wait(), 2)
        job.touched = time.monotonic() - 100
        manager.cleanup()
        assert job.id in manager.jobs and job.v1_status()["expires_at"] is None
        release.set()
        await finish(job)
        job.touched = time.monotonic() - 100
        manager.cleanup()
        assert job.id not in manager.jobs
        manager.reserve()
        await manager.shutdown()
        assert manager.jobs == {} and manager.reservation is None
    asyncio.run(scenario())
