from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.asgi import exception_handler
from app.controllers.v1 import channels as controller
from app.models.exception import HttpException
from app.services import channels as cs
from app.services import publication_queue as q

CHANNEL = {
    "name": "Receitas",
    "upload_post_username": "receitas-user",
    "platforms": ["tiktok"],
    "publish_mode": "approval",
    "posting_times": ["09:00"],
    "timezone": "America/Sao_Paulo",
    "params": {"video_aspect": "9:16"},
}


@pytest.fixture(autouse=True)
def mpt_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MPT_HOME", str(tmp_path))
    monkeypatch.setitem(controller.base.config.app, "api_key", "")
    return tmp_path


@pytest.fixture
def client():
    app = FastAPI()
    app.add_exception_handler(HttpException, exception_handler)
    app.include_router(controller.router)
    return TestClient(app)


def test_channel_crud(client):
    response = client.put("/api/v1/channels/receitas", json=CHANNEL)
    assert response.status_code == 200
    assert response.json()["data"]["params"] == {"video_aspect": "9:16"}

    listed = client.get("/api/v1/channels").json()["data"]["channels"]
    assert [channel["id"] for channel in listed] == ["receitas"]
    assert client.get("/api/v1/channels/receitas").status_code == 200
    assert client.get("/api/v1/channels/nope").status_code == 404

    bad = client.put("/api/v1/channels/receitas", json={**CHANNEL, "platforms": ["orkut"]})
    assert bad.status_code == 400
    assert "unsupported platform" in bad.json()["message"]

    assert client.delete("/api/v1/channels/receitas").status_code == 200
    assert client.delete("/api/v1/channels/receitas").status_code == 404


def test_publication_review_flow(client, tmp_path):
    channel = cs.save_channel(cs.Channel(id="receitas", **CHANNEL))
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    [publication_id] = q.enqueue_task_videos(
        "task-1", [str(video)], channel, metadata={"title": "T", "caption": "C", "hashtags": []}
    )

    listed = client.get("/api/v1/publications", params={"status": "pending_approval"}).json()["data"]
    assert [record["id"] for record in listed["publications"]] == [publication_id]
    assert listed["counts"]["pending_approval"] == 1
    assert client.get("/api/v1/publications", params={"status": "weird"}).status_code == 400

    edited = client.patch(
        f"/api/v1/publications/{publication_id}", json={"title": "Novo", "hashtags": ["#a"]}
    ).json()["data"]
    assert edited["title"] == "Novo"
    assert edited["hashtags"] == ["a"]

    approved = client.post(
        f"/api/v1/publications/{publication_id}/approve",
        json={"scheduled_at": "2030-01-01T12:00:00Z"},
    ).json()["data"]
    assert approved["status"] == "scheduled"
    assert approved["scheduled_at"].startswith("2030-01-01T12:00:00")

    rejected = client.post(f"/api/v1/publications/{publication_id}/reject")
    assert rejected.json()["data"]["status"] == "rejected"
    conflict = client.post(f"/api/v1/publications/{publication_id}/approve")
    assert conflict.status_code == 409
    assert client.post("/api/v1/publications/missing/reject").status_code == 404

    assert client.delete(f"/api/v1/publications/{publication_id}").status_code == 200
    assert client.get(f"/api/v1/publications/{publication_id}").status_code == 404


def test_api_key_protects_channel_routes(client, monkeypatch):
    monkeypatch.setitem(controller.base.config.app, "api_key", "secret")
    assert client.get("/api/v1/channels").status_code == 401
    assert client.get("/api/v1/channels", headers={"x-api-key": "secret"}).status_code == 200


def test_video_task_applies_channel_defaults(mpt_home):
    from app.controllers.v1 import video as video_controller
    from app.models.schema import TaskVideoRequest

    cs.save_channel(cs.Channel(id="receitas", **{**CHANNEL, "params": {"font_size": 77}}))
    captured = {}

    def add_task(func, task_id, params, stop_at):
        captured["params"] = params

    with patch.object(video_controller.task_manager, "add_task", side_effect=add_task):
        video_controller.create_task(
            _request(),
            TaskVideoRequest(video_subject="Bolo", channel_id="receitas"),
            stop_at="video",
        )
        with pytest.raises(HttpException) as error:
            video_controller.create_task(
                _request(),
                TaskVideoRequest(video_subject="Bolo", channel_id="ghost"),
                stop_at="video",
            )

    assert captured["params"].font_size == 77
    assert captured["params"].channel_id == "receitas"
    assert error.value.status_code == 400


def _request():
    from starlette.requests import Request

    return Request({"type": "http", "headers": [], "method": "POST", "path": "/"})
