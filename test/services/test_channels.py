from datetime import datetime, timezone

import pytest

from app.models.schema import VideoParams
from app.services import channels as cs


@pytest.fixture(autouse=True)
def mpt_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MPT_HOME", str(tmp_path))
    return tmp_path


def _channel(**overrides):
    values = {
        "id": "receitas",
        "name": "Receitas",
        "upload_post_username": "receitas-user",
        "platforms": ["tiktok", "youtube"],
        "publish_mode": "approval",
        "posting_times": ["18:00", "09:00"],
        "timezone": "America/Sao_Paulo",
    }
    values.update(overrides)
    return cs.Channel(**values)


def test_save_and_load_round_trip(mpt_home):
    saved = cs.save_channel(
        _channel(id="Receitas", params={"voice_name": "pt-BR-FranciscaNeural-Female"})
    )

    assert saved.id == "receitas"
    assert saved.posting_times == ["09:00", "18:00"]
    assert (mpt_home / "channels.toml").is_file()
    loaded = cs.load_channels()
    assert list(loaded) == ["receitas"]
    assert loaded["receitas"].params == {"voice_name": "pt-BR-FranciscaNeural-Female"}
    assert cs.get_channel("RECEITAS").name == "Receitas"


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"id": "Bad Id!"}, "channel id"),
        ({"platforms": ["myspace"]}, "unsupported platform"),
        ({"posting_times": ["25:00"]}, "HH:MM"),
        ({"timezone": "Mars/Base"}, "unknown timezone"),
        ({"publish_mode": "sometimes"}, "publish_mode"),
        ({"upload_post_username": ""}, "upload_post_username"),
        ({"platforms": []}, "at least one platform"),
        ({"youtube_privacy_status": "secret"}, "youtube_privacy_status"),
        ({"params": {"not_a_field": 1}}, "unknown video params"),
    ],
)
def test_invalid_channels_are_rejected(overrides, message):
    with pytest.raises(cs.ChannelError, match=message):
        cs.save_channel(_channel(**overrides))


def test_manual_channel_needs_no_upload_profile():
    channel = cs.save_channel(
        _channel(publish_mode="manual", upload_post_username="", platforms=[])
    )
    assert channel.publish_mode == "manual"


def test_params_drop_content_and_validate_values():
    channel = cs.save_channel(
        _channel(
            params={
                "video_subject": "should not be stored",
                "video_script": "nor this",
                "video_aspect": "9:16",
                "font_size": 72,
            }
        )
    )
    assert channel.params == {"video_aspect": "9:16", "font_size": 72}

    with pytest.raises(cs.ChannelError, match="font_size"):
        cs.save_channel(_channel(params={"font_size": 0}))


def test_invalid_channel_in_file_does_not_hide_valid_ones(mpt_home):
    (mpt_home / "channels.toml").write_text(
        '[channels.ok]\nname = "OK"\npublish_mode = "manual"\n\n'
        '[channels.broken]\npublish_mode = "approval"\n',
        encoding="utf-8",
    )
    assert list(cs.load_channels()) == ["ok"]


def test_load_picks_up_external_edits(mpt_home):
    cs.save_channel(_channel())
    assert cs.get_channel("receitas").name == "Receitas"
    text = (mpt_home / "channels.toml").read_text(encoding="utf-8")
    (mpt_home / "channels.toml").write_text(
        text.replace('name = "Receitas"', 'name = "Receitas da Vó"'), encoding="utf-8"
    )
    assert cs.get_channel("receitas").name == "Receitas da Vó"


def test_rename_and_delete():
    cs.save_channel(_channel())
    cs.save_channel(_channel(id="cozinha"), previous_id="receitas")
    assert list(cs.load_channels()) == ["cozinha"]

    cs.save_channel(_channel(id="outro"))
    with pytest.raises(cs.ChannelError, match="already exists"):
        cs.save_channel(_channel(id="outro"), previous_id="cozinha")

    assert cs.delete_channel("cozinha") is True
    assert cs.delete_channel("cozinha") is False
    with pytest.raises(cs.ChannelError, match="unknown channel"):
        cs.require_channel("cozinha")


def test_apply_channel_defaults_keeps_explicit_values():
    channel = cs.save_channel(
        _channel(params={"voice_name": "pt-BR-AntonioNeural-Male", "font_size": 80})
    )
    params = VideoParams(video_subject="Bolo", font_size=50)

    applied = cs.apply_channel_defaults(params, channel)

    assert applied.font_size == 50
    assert applied.voice_name == "pt-BR-AntonioNeural-Male"
    assert applied.video_subject == "Bolo"
    assert applied.channel_id == "receitas"

    forced = cs.apply_channel_defaults(params, channel, explicit_fields={"video_subject"})
    assert forced.font_size == 80


def test_next_slot_without_times_publishes_now():
    assert cs.next_posting_slot(_channel(posting_times=[])) is None


def test_next_slot_uses_channel_timezone_and_skips_taken():
    channel = cs.validate_channel(_channel())
    # 08:00 in São Paulo (UTC-3) -> next slot is 09:00 local = 12:00 UTC.
    now = datetime(2026, 10, 26, 11, 0, tzinfo=timezone.utc)

    first = cs.next_posting_slot(channel, now)
    assert first == datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)

    second = cs.next_posting_slot(channel, now, taken=[first.isoformat()])
    assert second == datetime(2026, 10, 26, 21, 0, tzinfo=timezone.utc)

    third = cs.next_posting_slot(channel, now, taken=[first, second])
    assert third == datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def test_next_slot_skips_a_time_that_is_about_to_pass():
    channel = cs.validate_channel(_channel(posting_times=["09:00"]))
    now = datetime(2026, 10, 26, 11, 59, tzinfo=timezone.utc)  # 08:59 local

    assert cs.next_posting_slot(channel, now) == datetime(
        2026, 10, 27, 12, 0, tzinfo=timezone.utc
    )
