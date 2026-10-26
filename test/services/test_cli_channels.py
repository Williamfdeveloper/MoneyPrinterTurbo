import json

import pytest

import cli
from app.services import channels as cs


@pytest.fixture(autouse=True)
def channel(tmp_path, monkeypatch):
    monkeypatch.setenv("MPT_HOME", str(tmp_path))
    return cs.save_channel(
        cs.Channel(
            id="receitas",
            name="Receitas",
            publish_mode="manual",
            params={"font_size": 77, "video_aspect": "16:9", "voice_rate": 1.2},
        )
    )


def test_channel_fills_only_options_not_passed():
    args = cli.parse_args(
        ["--video-subject", "Bolo", "--channel", "receitas", "--font-size", "40"]
    )
    params = cli.build_video_params(args)

    assert params.channel_id == "receitas"
    assert params.font_size == 40
    assert params.video_aspect == "16:9"
    assert params.voice_rate == 1.2
    assert params.video_subject == "Bolo"


def test_unknown_channel_is_invalid_input():
    args = cli.parse_args(["--video-subject", "Bolo", "--channel", "ghost"])
    with pytest.raises(ValueError, match="unknown channel"):
        cli.build_video_params(args)


def test_batch_entries_can_target_different_channels(tmp_path):
    cs.save_channel(
        cs.Channel(id="viagem", name="Viagem", publish_mode="manual", params={"font_size": 55})
    )
    manifest = tmp_path / "tasks.json"
    manifest.write_text(
        json.dumps(
            [
                {"video_subject": "Bolo", "channel_id": "receitas"},
                {"video_subject": "Praia", "channel_id": "viagem"},
                {"video_subject": "Sem canal"},
            ]
        ),
        encoding="utf-8",
    )
    args = cli.parse_args(["--batch-file", str(manifest), "--stop-at", "script"])
    tasks = cli._build_batch_tasks(args)

    assert [task.channel_id for task in tasks] == ["receitas", "viagem", ""]
    assert [task.font_size for task in tasks][:2] == [77, 55]
