"""Streamlit tests for the channel, publication-queue and login screens."""

import pytest
from streamlit.testing.v1 import AppTest

from app.services import channels as cs
from app.services import publication_queue as q


@pytest.fixture(autouse=True)
def mpt_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MPT_HOME", str(tmp_path))
    monkeypatch.delenv("MPT_WEBUI_PASSWORD", raising=False)
    return tmp_path


def _channels_settings_page():
    from webui import channels_ui

    channels_ui.render_channel_settings(lambda key: key)


def _queue_page():
    import streamlit as st

    from webui import channels_ui

    st.session_state[channels_ui.QUEUE_OPEN_KEY] = True
    channels_ui.render_top_bar_controls(lambda key: key)
    channels_ui.render_publication_queue(lambda key: key)


def _login_page():
    import streamlit as st

    from webui import access_ui

    if access_ui.require_login(lambda key: key):
        st.write("inside")
        access_ui.render_logout_button(lambda key: key)


def _access_settings_page():
    from app.config import config
    from webui import access_ui

    def set_value(section, key, value):
        config.ui[key] = value

    def delete_value(section, key):
        config.ui.pop(key, None)

    access_ui.render_access_settings(lambda key: key, set_value, delete_value, lambda: None)


def test_create_channel_from_settings_form(mpt_home):
    app = AppTest.from_function(_channels_settings_page, default_timeout=30)
    app.run()

    app.text_input(key="channel_form_new_id").set_value("Receitas")
    app.text_input(key="channel_form_new_name").set_value("Receitas da Vó")
    app.text_input(key="channel_form_new_user").set_value("receitas-user")
    app.multiselect(key="channel_form_new_platforms").set_value(["tiktok"])
    app.button[0].click().run()

    assert not app.exception
    channel = cs.get_channel("receitas")
    assert channel.name == "Receitas da Vó"
    assert channel.posting_times == ["09:00", "18:00"]
    assert channel.timezone == "America/Sao_Paulo"
    assert any("channels.saved" in item.value for item in app.success)


def test_invalid_channel_form_shows_error(mpt_home):
    app = AppTest.from_function(_channels_settings_page, default_timeout=30)
    app.run()
    app.text_input(key="channel_form_new_id").set_value("bad id!")
    app.button[0].click().run()

    assert not app.exception
    assert any("channels.save_failed" in item.value for item in app.error)
    assert cs.load_channels() == {}


def test_queue_approve_and_reject(mpt_home, tmp_path):
    channel = cs.save_channel(
        cs.Channel(id="receitas", name="Receitas", upload_post_username="u", platforms=["tiktok"])
    )
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    first, second = q.enqueue_task_videos(
        "task-1", [str(video), str(video)], channel,
        metadata={"title": "T", "caption": "C", "hashtags": ["h"]},
    )

    app = AppTest.from_function(_queue_page, default_timeout=30)
    app.run()
    assert not app.exception
    assert "(2)" in app.button(key="publication_queue_button").label

    app.text_input(key=f"pub_title_{first}").set_value("Título novo")
    app.button(key=f"pub_approve_{first}").click().run()
    app.button(key=f"pub_reject_{second}").click().run()

    assert not app.exception
    assert q.get_publication(first)["status"] == q.STATUS_SCHEDULED
    assert q.get_publication(first)["title"] == "Título novo"
    assert q.get_publication(second)["status"] == q.STATUS_REJECTED


def test_login_gate_blocks_until_valid_password(monkeypatch):
    monkeypatch.setenv("MPT_WEBUI_PASSWORD", "segredo123")
    from app.services import webui_auth

    monkeypatch.setattr(webui_auth, "_failures", [])
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    app = AppTest.from_function(_login_page, default_timeout=30)
    app.run()
    assert not [item for item in app.markdown if item.value == "inside"]

    app.text_input[1].set_value("errada")
    app.button[0].click().run()
    assert any("access.invalid" in item.value for item in app.error)

    app.text_input[1].set_value("segredo123")
    app.button[0].click().run()
    assert any(item.value == "inside" for item in app.markdown)
    assert app.button(key="webui_logout_button")


def test_access_settings_set_and_remove_password(monkeypatch):
    from app.config import config
    from app.services import webui_auth

    monkeypatch.delitem(config.ui, "webui_password_hash", raising=False)
    try:
        app = AppTest.from_function(_access_settings_page, default_timeout=30)
        app.run()
        app.text_input[1].set_value("curta")
        app.text_input[2].set_value("curta")
        app.button[0].click().run()
        assert any("access.error.too_short" in item.value for item in app.error)

        app.text_input[1].set_value("senha-forte")
        app.text_input[2].set_value("senha-forte")
        app.button[0].click().run()
        assert webui_auth.verify_hash("senha-forte", config.ui["webui_password_hash"])

        app.run()
        app.button(key="webui_remove_password").click().run()
        assert "webui_password_hash" not in config.ui
    finally:
        config.ui.pop("webui_password_hash", None)
        config.ui.pop("webui_username", None)
