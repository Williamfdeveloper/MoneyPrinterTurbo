from unittest.mock import patch

import pytest

from app.services import material_fallback as mf


@pytest.fixture
def keys(monkeypatch):
    values = {"pexels_api_keys": [], "pixabay_api_keys": ["px"], "coverr_api_keys": "cv"}

    def configure(**overrides):
        values.update(overrides)
        for key, value in values.items():
            monkeypatch.setitem(mf.config.app, key, value)

    configure()
    monkeypatch.setitem(mf.config.app, "video_source_fallbacks", ["pexels", "pixabay", "coverr"])
    return configure


def test_candidates_include_only_sources_with_keys(keys):
    assert mf.candidate_sources("pexels") == ["pexels", "pixabay", "coverr"]
    keys(coverr_api_keys="")
    assert mf.candidate_sources("pixabay") == ["pixabay"]


def test_paid_sources_never_fall_back(keys):
    assert mf.candidate_sources("volcengine_seedance") == ["volcengine_seedance"]


def test_fallback_can_be_disabled(keys, monkeypatch):
    monkeypatch.setitem(mf.config.app, "video_source_fallbacks", [])
    assert mf.candidate_sources("pexels") == ["pexels"]


def test_empty_result_moves_to_next_source(keys):
    calls = []

    def download(source):
        calls.append(source)
        return ["clip.mp4"] if source == "coverr" else []

    assert mf.download_with_fallback("pexels", download) == (["clip.mp4"], "coverr")
    assert calls == ["pexels", "pixabay", "coverr"]


def test_primary_error_falls_back_then_reraises_when_all_fail(keys):
    def download(source):
        if source == "pexels":
            raise ValueError("pexels_api_keys is not set")
        return ["clip.mp4"]

    assert mf.download_with_fallback("pexels", download) == (["clip.mp4"], "pixabay")

    def broken(source):
        raise ValueError(f"{source} down")

    with pytest.raises(ValueError, match="pexels down"):
        mf.download_with_fallback("pexels", broken)


def test_single_source_keeps_original_behavior(keys):
    keys(pixabay_api_keys=[], coverr_api_keys="")
    with pytest.raises(RuntimeError):
        mf.download_with_fallback("pexels", lambda source: (_ for _ in ()).throw(RuntimeError("x")))
    assert mf.download_with_fallback("pexels", lambda source: []) == ([], "pexels")


def test_task_materials_use_fallback(keys):
    from app.models.schema import VideoParams
    from app.services import task as tm

    params = VideoParams(video_subject="Café", video_source="pexels")
    with patch.object(
        tm.material, "download_videos", side_effect=lambda **kw: [] if kw["source"] == "pexels" else ["a.mp4"]
    ) as download:
        result = tm.get_video_materials("task-fallback", params, ["coffee"], 10)

    assert result == ["a.mp4"]
    assert [call.kwargs["source"] for call in download.call_args_list] == ["pexels", "pixabay"]
