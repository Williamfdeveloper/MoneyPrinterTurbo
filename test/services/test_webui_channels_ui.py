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


def _manager_page():
    # AppTest runs only this function's body, so the page params live here.
    import streamlit as st

    from webui import channel_manager_ui, channels_ui

    page_params = {
        "video_subject": "never stored",
        "voice_name": "pt-BR-AntonioNeural-Male",
        "video_aspect": "16:9",
        "font_size": 64,
        "stroke_width": 2.5,
    }
    st.session_state.setdefault(channel_manager_ui.MANAGER_OPEN_KEY, True)
    channels_ui.render_top_bar_controls(lambda key: key)
    channel_manager_ui.render_channel_manager(lambda key: key, page_params)


def _save_channel_button(app):
    return next(button for button in app.button if button.label in {"channels.create", "channels.save"})


def test_settings_tab_opens_manager(mpt_home):
    app = AppTest.from_function(_channels_settings_page, default_timeout=30)
    app.session_state["settings_dialog_open"] = True
    app.run()
    app.button(key="settings_open_channel_manager").click().run()

    assert not app.exception
    assert app.session_state["channel_manager_open"] is True
    assert app.session_state["settings_dialog_open"] is False


def test_manager_creates_channel_with_video_style(mpt_home):
    app = AppTest.from_function(_manager_page, default_timeout=30)
    app.run()
    assert any("channels.empty" in item.value for item in app.info)

    app.button(key="chm_new").click().run()
    app.text_input(key="chm_id").set_value("Receitas")
    app.text_input(key="chm_name").set_value("Receitas da Vó")
    app.text_input(key="chm_upload_post_username").set_value("receitas-user")
    app.multiselect(key="chm_platforms").set_value(["tiktok", "youtube"])
    app.selectbox(key="chm_video_aspect").set_value("9:16")
    app.number_input(key="chm_font_size").set_value(80)
    app.text_area(key="chm_video_script_prompt").set_value("Tom leve.")
    _save_channel_button(app).click().run()

    assert not app.exception
    channel = cs.get_channel("receitas")
    assert channel.name == "Receitas da Vó"
    assert channel.platforms == ["tiktok", "youtube"]
    assert channel.posting_times == ["09:00", "18:00"]
    assert channel.params == {
        "video_aspect": "9:16",
        "font_size": 80,
        "video_script_prompt": "Tom leve.",
    }
    assert any("channels.saved" in item.value for item in app.success)
    assert app.button(key="chm_edit_receitas")


def test_manager_shows_validation_errors(mpt_home):
    app = AppTest.from_function(_manager_page, default_timeout=30)
    app.run()
    app.button(key="chm_new").click().run()
    app.text_input(key="chm_id").set_value("bad id!")
    _save_channel_button(app).click().run()

    assert not app.exception
    assert any("channels.save_failed" in item.value for item in app.error)
    assert cs.load_channels() == {}


def test_manager_edit_copy_page_style_and_inherit(mpt_home):
    cs.save_channel(
        cs.Channel(
            id="receitas", name="Receitas", upload_post_username="u", platforms=["tiktok"],
            params={"font_size": 90, "bgm_volume": 0.3, "video_script_prompt": "Tom leve."},
        )
    )
    app = AppTest.from_function(_manager_page, default_timeout=30)
    app.run()
    app.button(key="chm_edit_receitas").click().run()
    assert app.number_input(key="chm_font_size").value == 90

    # Typed but unsaved edits must survive the "copy page style" click.
    app.text_input(key="chm_name").set_value("Receitas Novas")
    app.button(key="chm_copy_page_style").click().run()
    assert app.text_input(key="chm_name").value == "Receitas Novas"
    assert app.selectbox(key="chm_voice_name").value == "pt-BR-AntonioNeural-Male"
    assert app.number_input(key="chm_font_size").value == 64
    _save_channel_button(app).click().run()

    channel = cs.get_channel("receitas")
    assert channel.name == "Receitas Novas"
    assert channel.params["voice_name"] == "pt-BR-AntonioNeural-Male"
    assert channel.params["video_aspect"] == "16:9"
    assert channel.params["font_size"] == 64
    # Page-only settings without an editor field are kept as extras.
    assert channel.params["stroke_width"] == 2.5
    # Values the page does not set keep the channel's own setting.
    assert channel.params["bgm_volume"] == 0.3
    assert channel.params["video_script_prompt"] == "Tom leve."
    assert "video_subject" not in channel.params


def test_manager_rename_keeps_single_channel(mpt_home):
    cs.save_channel(cs.Channel(id="receitas", name="R", publish_mode="manual"))
    app = AppTest.from_function(_manager_page, default_timeout=30)
    app.run()
    app.button(key="chm_edit_receitas").click().run()
    app.text_input(key="chm_id").set_value("cozinha")
    _save_channel_button(app).click().run()

    assert list(cs.load_channels()) == ["cozinha"]


def test_manager_delete_needs_confirmation(mpt_home):
    cs.save_channel(cs.Channel(id="receitas", name="Receitas", publish_mode="manual"))
    app = AppTest.from_function(_manager_page, default_timeout=30)
    app.run()

    app.button(key="chm_delete_receitas").click().run()
    assert cs.get_channel("receitas") is not None
    app.button(key="chm_cancel_delete_receitas").click().run()
    assert app.button(key="chm_delete_receitas")

    app.button(key="chm_delete_receitas").click().run()
    app.button(key="chm_confirm_delete_receitas").click().run()
    assert not app.exception
    assert cs.load_channels() == {}


def test_manager_use_selects_channel_and_closes(mpt_home):
    cs.save_channel(
        cs.Channel(id="receitas", name="Receitas", publish_mode="manual", params={"font_size": 70})
    )
    app = AppTest.from_function(_manager_page, default_timeout=30)
    app.run()
    app.button(key="chm_use_receitas").click().run()

    assert not app.exception
    assert app.session_state["mpt_channel_selector"] == "receitas"
    assert app.session_state["channel_manager_open"] is False
    assert app.session_state["settings_preset_payload"]["font_size"] == 70


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
