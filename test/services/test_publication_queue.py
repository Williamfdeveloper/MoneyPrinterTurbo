import multiprocessing
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.services import channels as cs
from app.services import publication_queue as q

METADATA = {"title": "Bolo de cenoura", "caption": "Receita fácil", "hashtags": ["bolo", "receita"]}


@pytest.fixture(autouse=True)
def mpt_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MPT_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "final-1.mp4"
    path.write_bytes(b"fake video")
    return str(path)


def _channel(**overrides):
    values = {
        "id": "receitas",
        "name": "Receitas",
        "upload_post_username": "receitas-user",
        "platforms": ["tiktok", "youtube"],
        "publish_mode": "approval",
        "posting_times": [],
        "timezone": "America/Sao_Paulo",
    }
    values.update(overrides)
    return cs.save_channel(cs.Channel(**values))


def _enqueue(channel, video, count=1):
    return q.enqueue_task_videos(
        "task-1", [video] * count, channel, video_subject="Bolo", metadata=METADATA
    )


def test_approval_channel_waits_for_review(video):
    [publication_id] = _enqueue(_channel(), video)

    record = q.get_publication(publication_id)
    assert record["status"] == q.STATUS_PENDING_APPROVAL
    assert record["upload_post_username"] == "receitas-user"
    assert record["platforms"] == ["tiktok", "youtube"]
    assert record["title"] == "Bolo de cenoura"
    assert record["video_path"] == os.path.abspath(video)
    assert q.count_by_status()[q.STATUS_PENDING_APPROVAL] == 1


def test_manual_channel_queues_nothing(video):
    channel = _channel(publish_mode="manual")
    assert _enqueue(channel, video) == []
    assert q.list_publications() == []


def test_auto_channel_schedules_each_video_in_its_own_slot(video):
    channel = _channel(publish_mode="auto", posting_times=["09:00", "18:00"])

    ids = _enqueue(channel, video, count=3)

    slots = [q.get_publication(i)["scheduled_at"] for i in ids]
    assert all(q.get_publication(i)["status"] == q.STATUS_SCHEDULED for i in ids)
    assert len(set(slots)) == 3


def test_auto_channel_without_times_publishes_immediately(video):
    channel = _channel(publish_mode="auto")
    [publication_id] = _enqueue(channel, video)
    record = q.get_publication(publication_id)
    assert record["status"] == q.STATUS_SCHEDULED
    assert q.parse_time(record["scheduled_at"]) <= datetime.now(timezone.utc)


def test_edit_and_approve_variants(video):
    channel = _channel(posting_times=["09:00"])
    [first, second] = _enqueue(channel, video, count=2)

    edited = q.update_publication(first, title="Novo", hashtags="#a, b #a")
    assert edited["title"] == "Novo"
    assert edited["hashtags"] == ["a", "b"]

    approved = q.approve_publication(first)
    other = q.approve_publication(second)
    assert approved["status"] == q.STATUS_SCHEDULED
    assert approved["scheduled_at"] != other["scheduled_at"]

    chosen = datetime(2030, 1, 2, 15, 30, tzinfo=timezone.utc)
    rescheduled = q.approve_publication(first, scheduled_at=chosen.isoformat())
    assert q.parse_time(rescheduled["scheduled_at"]) == chosen

    now = q.approve_publication(second, publish_now=True)
    assert q.parse_time(now["scheduled_at"]) <= datetime.now(timezone.utc)


def test_invalid_transitions_raise(video):
    [publication_id] = _enqueue(_channel(), video)
    q.reject_publication(publication_id)

    with pytest.raises(q.PublicationError):
        q.approve_publication(publication_id)
    with pytest.raises(q.PublicationError):
        q.update_publication(publication_id, title="x")
    with pytest.raises(q.PublicationError):
        q.retry_publication(publication_id)
    with pytest.raises(q.PublicationError, match="not found"):
        q.approve_publication("missing")
    with pytest.raises(q.PublicationError, match="invalid publication id"):
        q.get_publication("../etc/passwd")

    assert q.delete_publication(publication_id) is True
    assert q.get_publication(publication_id) is None


def test_active_publication_cannot_be_deleted(video):
    [publication_id] = _enqueue(_channel(), video)
    with pytest.raises(q.PublicationError, match="reject it first"):
        q.delete_publication(publication_id)


def test_claim_due_only_takes_due_records_once(video):
    channel = _channel()
    [due, later] = _enqueue(channel, video, count=2)
    q.approve_publication(due, publish_now=True)
    q.approve_publication(
        later, scheduled_at=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    )

    claimed = q.claim_due(5)
    assert [record["id"] for record in claimed] == [due]
    assert q.get_publication(due)["status"] == q.STATUS_PUBLISHING
    assert q.get_publication(due)["attempts"] == 1
    assert q.claim_due(5) == []


def _claim_in_child(home, results):
    os.environ["MPT_HOME"] = home
    results.extend([record["id"] for record in q.claim_due(10)])


@pytest.mark.skipif(os.name == "nt", reason="fork-based multiprocessing")
def test_claim_is_exclusive_across_processes(video, mpt_home):
    ids = _enqueue(_channel(), video, count=6)
    for publication_id in ids:
        q.approve_publication(publication_id, publish_now=True)

    context = multiprocessing.get_context("fork")
    with context.Manager() as manager:
        results = manager.list()
        workers = [
            context.Process(target=_claim_in_child, args=(str(mpt_home), results))
            for _ in range(4)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(30)
        claimed = list(results)

    assert sorted(claimed) == sorted(ids)


def test_finish_retry_and_remove(video):
    [publication_id] = _enqueue(_channel(), video)
    q.approve_publication(publication_id, publish_now=True)
    q.claim_due(1)

    failed = q.finish_publication(publication_id, False, error="boom", request_id="req-1")
    assert failed["status"] == q.STATUS_FAILED
    assert failed["error"] == "boom"
    assert failed["request_id"] == "req-1"

    retried = q.retry_publication(publication_id)
    assert retried["status"] == q.STATUS_SCHEDULED
    q.claim_due(1)
    published = q.finish_publication(publication_id, True, results={"tiktok": {"success": True}})
    assert published["status"] == q.STATUS_PUBLISHED
    assert published["published_at"]
    assert q.delete_publication(publication_id) is True


def test_recover_stale_fails_abandoned_publishing(video):
    [stale, fresh] = _enqueue(_channel(), video, count=2)
    for publication_id in (stale, fresh):
        q.approve_publication(publication_id, publish_now=True)
    q.claim_due(2)
    q.record_request_id(stale, "req-9")

    later = datetime.now(timezone.utc) + q.STALE_PUBLISHING_AFTER + timedelta(minutes=1)
    q.heartbeat(fresh)
    with patch.object(q, "_now", return_value=later):
        q.heartbeat(fresh)
    assert q.recover_stale(now=later) == 1

    stale_record = q.get_publication(stale)
    assert stale_record["status"] == q.STATUS_FAILED
    assert "req-9" in stale_record["error"]
    assert q.get_publication(fresh)["status"] == q.STATUS_PUBLISHING


def test_metadata_falls_back_to_subject_when_llm_fails():
    with patch("app.services.llm.generate_social_metadata", side_effect=RuntimeError("down")):
        metadata = q.generate_metadata("Bolo", "script", "pt-BR", ["tiktok"])
    assert metadata == {"title": "Bolo", "caption": "Bolo", "hashtags": []}


def test_metadata_uses_platform_specific_prompt():
    with patch(
        "app.services.llm.generate_social_metadata",
        return_value={"title": "T", "caption": "C", "hashtags": ["#x", "y"]},
    ) as generate:
        metadata = q.generate_metadata("Bolo", "script", "pt-BR", ["instagram"])
    assert generate.call_args.kwargs["platform"] == "instagram_reels"
    assert metadata["hashtags"] == ["x", "y"]
