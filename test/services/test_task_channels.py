from contextlib import ExitStack
from unittest.mock import patch

import pytest

from app.models.schema import VideoParams
from app.services import channels as cs
from app.services import publication_queue as q
from app.services import task as tm
from app.services.state import MemoryState


@pytest.fixture(autouse=True)
def mpt_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MPT_HOME", str(tmp_path))
    return tmp_path


def _run(params, tmp_path, legacy_configured=True):
    video = tmp_path / "final.mp4"
    video.write_bytes(b"video")
    state = MemoryState()
    with ExitStack() as stack:
        stack.enter_context(patch.object(tm, "generate_script", return_value="script"))
        stack.enter_context(patch.object(tm, "generate_terms", return_value=["cake"]))
        stack.enter_context(patch.object(tm, "save_script_data"))
        stack.enter_context(
            patch.object(tm, "generate_audio", return_value=("audio.mp3", 5, object()))
        )
        stack.enter_context(patch.object(tm, "generate_subtitle", return_value="s.srt"))
        stack.enter_context(patch.object(tm, "get_video_materials", return_value=["c.mp4"]))
        stack.enter_context(
            patch.object(
                tm, "generate_final_videos", return_value=([str(video)], [str(video)], [])
            )
        )
        stack.enter_context(
            patch.object(
                tm.upload_post.upload_post_service, "is_configured", return_value=legacy_configured
            )
        )
        stack.enter_context(
            patch.object(
                type(tm.upload_post.upload_post_service),
                "auto_upload",
                new_callable=lambda: property(lambda self: True),
            )
        )
        legacy = stack.enter_context(patch.object(tm, "_schedule_cross_post"))
        stack.enter_context(
            patch(
                "app.services.llm.generate_social_metadata",
                return_value={"title": "T", "caption": "C", "hashtags": ["h"]},
            )
        )
        stack.enter_context(patch.object(tm.sm, "state", state))
        result = tm.start("task-channel", params)
    return result, state, legacy


def test_channel_video_goes_to_queue_instead_of_legacy_publish(tmp_path):
    cs.save_channel(
        cs.Channel(
            id="receitas", name="Receitas", upload_post_username="u", platforms=["tiktok"]
        )
    )
    result, state, legacy = _run(
        VideoParams(video_subject="Bolo", channel_id="receitas"), tmp_path
    )

    legacy.assert_not_called()
    assert result["channel_id"] == "receitas"
    [publication_id] = result["publication_ids"]
    assert state.get_task("task-channel")["publication_ids"] == [publication_id]
    record = q.get_publication(publication_id)
    assert record["status"] == q.STATUS_PENDING_APPROVAL
    assert record["caption"] == "C"


def test_video_without_channel_keeps_legacy_auto_publish(tmp_path):
    result, _, legacy = _run(VideoParams(video_subject="Bolo"), tmp_path)
    legacy.assert_called_once()
    assert "publication_ids" not in result
    assert q.list_publications() == []


def test_unknown_channel_fails_preflight(tmp_path):
    result, state, _ = _run(VideoParams(video_subject="Bolo", channel_id="ghost"), tmp_path)
    assert result["state"] == tm.const.TASK_STATE_FAILED
    assert result["failed_stage"] == "preflight"
    assert "unknown channel" in result["error"]


def test_queue_failure_does_not_fail_the_video(tmp_path):
    cs.save_channel(
        cs.Channel(id="receitas", name="R", upload_post_username="u", platforms=["tiktok"])
    )
    with patch.object(q, "enqueue_task_videos", side_effect=OSError("disk full")):
        result, state, _ = _run(
            VideoParams(video_subject="Bolo", channel_id="receitas"), tmp_path
        )
    assert result["videos"]
    assert result["publication_ids"] == []
    assert state.get_task("task-channel")["publication_error"] == "disk full"
