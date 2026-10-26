from unittest.mock import patch

import pytest

from app.services import channels as cs
from app.services import publication_queue as q
from app.services import publication_scheduler as scheduler

METADATA = {"title": "Bolo", "caption": "Receita fácil #bolo", "hashtags": ["bolo", "doce"]}


@pytest.fixture(autouse=True)
def mpt_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MPT_HOME", str(tmp_path))
    config_file = tmp_path / "config.toml"
    config_file.write_text('[app]\nupload_post_api_key = "fresh-key"\n', encoding="utf-8")
    monkeypatch.setattr(scheduler.config, "config_file", str(config_file))
    return tmp_path


@pytest.fixture
def due_record(tmp_path):
    video = tmp_path / "final-1.mp4"
    video.write_bytes(b"video")
    channel = cs.save_channel(
        cs.Channel(
            id="receitas",
            name="Receitas",
            upload_post_username="receitas-user",
            platforms=["tiktok", "youtube"],
            youtube_privacy_status="unlisted",
        )
    )
    [publication_id] = q.enqueue_task_videos(
        "task-1", [str(video)], channel, video_subject="Bolo", metadata=METADATA
    )
    q.approve_publication(publication_id, publish_now=True)
    return publication_id


def test_run_once_publishes_with_channel_profile_and_fresh_key(due_record):
    def fake_upload(**kwargs):
        kwargs["on_background_start"]("req-123")
        return {"success": True, "request_id": "req-123", "results": {"tiktok": {"success": True}}}

    with patch.object(scheduler.upload_post, "cross_post_video", side_effect=fake_upload) as upload:
        assert scheduler.run_once(wait=True) == 1

    kwargs = upload.call_args.kwargs
    assert kwargs["account"] == {
        "upload_post_api_key": "fresh-key",
        "upload_post_username": "receitas-user",
        "upload_post_enabled": True,
    }
    assert kwargs["platforms"] == ["tiktok", "youtube"]
    assert kwargs["title"] == "Receita fácil #bolo\n\n#doce"
    assert kwargs["youtube_extra"]["youtube_title"] == "Bolo"
    assert kwargs["youtube_extra"]["privacyStatus"] == "unlisted"
    record = q.get_publication(due_record)
    assert record["status"] == q.STATUS_PUBLISHED
    assert record["request_id"] == "req-123"
    assert scheduler.run_once(wait=True) == 0


def test_upload_failure_marks_record_failed(due_record):
    with patch.object(
        scheduler.upload_post,
        "cross_post_video",
        return_value={"success": False, "error": "token expired"},
    ):
        scheduler.run_once(wait=True)
    record = q.get_publication(due_record)
    assert record["status"] == q.STATUS_FAILED
    assert record["error"] == "token expired"


def test_upload_exception_marks_record_failed(due_record):
    with patch.object(scheduler.upload_post, "cross_post_video", side_effect=RuntimeError("net")):
        scheduler.run_once(wait=True)
    assert q.get_publication(due_record)["error"] == "net"


def test_missing_api_key_fails_without_uploading(due_record, mpt_home):
    (mpt_home / "config.toml").write_text("[app]\n", encoding="utf-8")
    with patch.object(scheduler.upload_post, "cross_post_video") as upload:
        scheduler.run_once(wait=True)
    upload.assert_not_called()
    assert "API key" in q.get_publication(due_record)["error"]


def test_missing_video_fails_without_uploading(due_record):
    import os

    os.remove(q.get_publication(due_record)["video_path"])
    with patch.object(scheduler.upload_post, "cross_post_video") as upload:
        scheduler.run_once(wait=True)
    upload.assert_not_called()
    assert "video file not found" in q.get_publication(due_record)["error"]


def test_post_payload_without_youtube_has_no_extra():
    text, extra = scheduler.build_post_payload(
        {"caption": "Oi", "hashtags": ["a"], "platforms": ["tiktok"]}
    )
    assert text == "Oi\n\n#a"
    assert extra is None
