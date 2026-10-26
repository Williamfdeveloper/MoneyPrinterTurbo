"""Channel manager screen: list, create, edit and delete channel profiles.

Every channel keeps its publishing settings (Upload-Post profile, platforms,
mode, posting times) and an optional video style. A style field left empty
means "use whatever is set on the main page", so channels only override what
really differs between them.
"""

import os

import streamlit as st
from loguru import logger

from app.services import channels as channel_service
from app.services import publication_queue as queue
from app.utils import utils

MANAGER_OPEN_KEY = "channel_manager_open"
EDITING_KEY = "channel_manager_editing"
DELETE_KEY = "channel_manager_delete"
EXTRA_PARAMS_KEY = "channel_manager_extra_params"
NEW_CHANNEL = "__new__"
INHERIT = "__inherit__"
FIELD_PREFIX = "chm_"

COMMON_TIMEZONES = [
    "America/Sao_Paulo",
    "America/Manaus",
    "America/Fortaleza",
    "America/Recife",
    "America/Belem",
    "America/Cuiaba",
    "America/Porto_Velho",
    "America/Rio_Branco",
    "America/Noronha",
    "UTC",
    "America/New_York",
    "America/Los_Angeles",
    "Europe/Lisbon",
    "Europe/London",
    "Europe/Madrid",
    "Asia/Shanghai",
    "Asia/Tokyo",
]
SCRIPT_LANGUAGES = [
    "pt-BR", "pt-PT", "en-US", "es-ES", "fr-FR", "de-DE", "it-IT",
    "ja-JP", "ko-KR", "zh-CN", "ru-RU", "tr-TR", "vi-VN",
]
# Publishing fields of the Channel dataclass, edited in the form.
_PUBLISH_FIELDS = (
    "id", "name", "upload_post_username", "platforms", "publish_mode",
    "posting_times", "timezone", "youtube_privacy_status", "youtube_made_for_kids",
)


@st.cache_data(ttl=300, show_spinner=False)
def _voices() -> list[str]:
    try:
        from app.services import voice

        return voice.get_all_azure_voices()
    except Exception as exc:  # a missing voice list must not break the screen
        logger.warning(f"could not load the voice list: {exc}")
        return []


@st.cache_data(ttl=30, show_spinner=False)
def _fonts() -> list[str]:
    font_dir = utils.font_dir()
    fonts = []
    for root, _dirs, files in os.walk(font_dir):
        for file in files:
            if file.endswith((".ttf", ".ttc")):
                relative = os.path.relpath(os.path.join(root, file), font_dir)
                fonts.append(relative.replace(os.sep, "/"))
    return sorted(fonts)


def _style_fields(tr) -> list[dict]:
    """Video style fields a channel can override (VideoParams names)."""
    return [
        {"name": "video_language", "kind": "select", "options": SCRIPT_LANGUAGES},
        {"name": "voice_name", "kind": "select", "options": _voices()},
        {"name": "voice_rate", "kind": "float", "min": 0.5, "max": 2.0, "step": 0.05},
        {
            "name": "video_aspect", "kind": "select", "options": ["9:16", "16:9", "1:1"],
            "labels": {
                "9:16": tr("channels.aspect.portrait"),
                "16:9": tr("channels.aspect.landscape"),
                "1:1": tr("channels.aspect.square"),
            },
        },
        {"name": "video_source", "kind": "select", "options": ["pixabay", "pexels", "coverr"]},
        {"name": "video_clip_duration", "kind": "int", "min": 1, "max": 15, "step": 1},
        {
            "name": "bgm_type", "kind": "select", "options": ["random", ""],
            "labels": {"random": tr("channels.bgm.random"), "": tr("channels.bgm.none")},
        },
        {"name": "bgm_volume", "kind": "float", "min": 0.0, "max": 1.0, "step": 0.05},
        {
            "name": "subtitle_enabled", "kind": "select", "options": [True, False],
            "labels": {True: tr("channels.yes"), False: tr("channels.no")},
        },
        {
            "name": "subtitle_position", "kind": "select",
            "options": ["bottom", "center", "top", "two_thirds_bottom"],
            "labels": {
                "bottom": tr("channels.position.bottom"),
                "center": tr("channels.position.center"),
                "top": tr("channels.position.top"),
                "two_thirds_bottom": tr("channels.position.two_thirds_bottom"),
            },
        },
        {"name": "font_name", "kind": "select", "options": _fonts()},
        {"name": "font_size", "kind": "int", "min": 20, "max": 150, "step": 2},
        {"name": "text_fore_color", "kind": "color"},
        {"name": "paragraph_number", "kind": "int", "min": 1, "max": 10, "step": 1},
        {"name": "video_script_prompt", "kind": "text"},
    ]


def _key(name: str) -> str:
    return f"{FIELD_PREFIX}{name}"


def _load_editor_state(channel: channel_service.Channel | None) -> None:
    """Copy a channel (or blank defaults) into the editor widgets' state."""
    channel = channel or channel_service.Channel(
        id="", name="", publish_mode="approval", timezone="America/Sao_Paulo",
        posting_times=["09:00", "18:00"],
    )
    st.session_state[_key("id")] = channel.id
    st.session_state[_key("name")] = channel.name
    st.session_state[_key("upload_post_username")] = channel.upload_post_username
    st.session_state[_key("platforms")] = list(channel.platforms)
    st.session_state[_key("publish_mode")] = channel.publish_mode
    st.session_state[_key("posting_times")] = ", ".join(channel.posting_times)
    st.session_state[_key("timezone")] = channel.timezone
    st.session_state[_key("youtube_privacy_status")] = channel.youtube_privacy_status
    st.session_state[_key("youtube_made_for_kids")] = channel.youtube_made_for_kids
    _load_style_state(channel.params)
    st.session_state[EXTRA_PARAMS_KEY] = {}


def _load_style_state(params: dict) -> None:
    for field in _style_fields(lambda key: key):
        value = params.get(field["name"])
        if field["kind"] == "select":
            # Values outside the option list (custom voice, preset song) are
            # kept; the selectbox appends them so they survive an edit.
            st.session_state[_key(field["name"])] = INHERIT if value is None else value
        elif field["kind"] in {"text", "color"}:
            st.session_state[_key(field["name"])] = str(value or "")
        else:
            st.session_state[_key(field["name"])] = value


def open_manager() -> None:
    st.session_state[MANAGER_OPEN_KEY] = True
    st.session_state[EDITING_KEY] = None
    st.session_state[DELETE_KEY] = None


def _close_manager() -> None:
    st.session_state[MANAGER_OPEN_KEY] = False
    st.session_state[EDITING_KEY] = None


def _start_new() -> None:
    _load_editor_state(None)
    st.session_state[EDITING_KEY] = NEW_CHANNEL


def _start_edit(channel_id: str) -> None:
    channel = channel_service.get_channel(channel_id)
    if channel is None:
        return
    _load_editor_state(channel)
    st.session_state[EDITING_KEY] = channel.id
    st.session_state[DELETE_KEY] = None


def _cancel_edit() -> None:
    st.session_state[EDITING_KEY] = None


def _copy_page_style(current_params: dict) -> None:
    """Fill the style fields from the main page and keep the rest for saving."""
    # Blank page values (e.g. an empty script prompt) never erase what the
    # channel already has; only real page settings are copied over.
    portable = {
        key: value
        for key, value in (current_params or {}).items()
        if key not in channel_service.EXCLUDED_PARAM_KEYS
        and key not in {"video_materials", "custom_audio_file", "bgm_file"}
        and value is not None
        and value != ""
    }
    edited = {field["name"] for field in _style_fields(lambda key: key)}
    current_style = {
        name: value
        for name in edited
        if (value := st.session_state.get(_key(name))) not in (None, "", INHERIT)
    }
    _load_style_state({**current_style, **portable})
    st.session_state[EXTRA_PARAMS_KEY] = {
        key: value for key, value in portable.items() if key not in edited
    }


def _ask_delete(channel_id: str) -> None:
    st.session_state[DELETE_KEY] = channel_id


def _cancel_delete() -> None:
    st.session_state[DELETE_KEY] = None


def _confirm_delete(channel_id: str) -> None:
    from webui import channels_ui

    channel_service.delete_channel(channel_id)
    if st.session_state.get(channels_ui.SELECTOR_KEY) == channel_id:
        st.session_state[channels_ui.SELECTOR_KEY] = ""
    st.session_state[DELETE_KEY] = None
    st.session_state["channel_manager_message"] = ("deleted", channel_id)


def _use_channel(channel_id: str) -> None:
    from webui import channels_ui

    st.session_state[channels_ui.SELECTOR_KEY] = channel_id
    channels_ui._apply_channel_preset()
    st.session_state[MANAGER_OPEN_KEY] = False


def _style_params_from_state(channel: channel_service.Channel | None) -> dict:
    params = dict(channel.params) if channel else {}
    params.update(st.session_state.get(EXTRA_PARAMS_KEY) or {})
    for field in _style_fields(lambda key: key):
        name = field["name"]
        value = st.session_state.get(_key(name))
        if field["kind"] == "select":
            empty = value == INHERIT
        elif field["kind"] in {"text", "color"}:
            value = str(value or "").strip()
            empty = not value
        else:
            empty = value is None
        if empty:
            params.pop(name, None)
        else:
            params[name] = value
    return params


def _save_from_state(tr) -> bool:
    editing = st.session_state.get(EDITING_KEY)
    original = None if editing == NEW_CHANNEL else channel_service.get_channel(editing)
    posting_times = str(st.session_state.get(_key("posting_times")) or "")
    try:
        saved = channel_service.save_channel(
            channel_service.Channel(
                id=st.session_state.get(_key("id"), ""),
                name=st.session_state.get(_key("name"), ""),
                upload_post_username=st.session_state.get(_key("upload_post_username"), ""),
                platforms=list(st.session_state.get(_key("platforms")) or []),
                publish_mode=st.session_state.get(_key("publish_mode"), "approval"),
                posting_times=[
                    part.strip() for part in posting_times.replace(";", ",").split(",")
                ],
                timezone=st.session_state.get(_key("timezone"), "UTC"),
                youtube_privacy_status=st.session_state.get(
                    _key("youtube_privacy_status"), "public"
                ),
                youtube_made_for_kids=bool(st.session_state.get(_key("youtube_made_for_kids"))),
                params=_style_params_from_state(original),
            ),
            previous_id=None if original is None else original.id,
        )
    except channel_service.ChannelError as exc:
        st.error(tr("channels.save_failed").format(error=exc))
        return False
    st.session_state[EDITING_KEY] = None
    st.session_state["channel_manager_message"] = ("saved", saved.name)
    return True


def _mode_label(tr, mode: str) -> str:
    return tr(f"channels.mode.{mode}")


def _render_style_field(tr, field: dict, container) -> None:
    name = field["name"]
    label = tr(f"channels.field.{name}")
    key = _key(name)
    if field["kind"] == "select":
        options = [INHERIT] + list(field["options"])
        current = st.session_state.get(key, INHERIT)
        if current not in options:
            # Keep a stored value that is no longer listed (custom voice/font).
            options.append(current)
        labels = field.get("labels", {})
        container.selectbox(
            label,
            options=options,
            key=key,
            format_func=lambda value: tr("channels.inherit")
            if value == INHERIT
            else labels.get(value, str(value)),
        )
    elif field["kind"] == "text":
        container.text_area(label, key=key, height=90, placeholder=tr("channels.inherit"))
    elif field["kind"] == "color":
        container.text_input(label, key=key, placeholder="#FFFFFF  ·  " + tr("channels.inherit"))
    else:
        is_int = field["kind"] == "int"
        container.number_input(
            label,
            min_value=field["min"],
            max_value=field["max"],
            step=field["step"],
            key=key,
            placeholder=tr("channels.inherit"),
            format="%d" if is_int else "%.2f",
        )


def _render_editor(tr, current_params: dict) -> None:
    editing = st.session_state.get(EDITING_KEY)
    is_new = editing == NEW_CHANNEL
    st.markdown(f"#### {tr('channels.new') if is_new else tr('channels.edit_title').format(name=st.session_state.get(_key('name')) or editing)}")
    with st.form("channel_manager_form", border=False):
        # A submit button keeps what was typed in the form; a plain button
        # outside the form would discard uncommitted edits on rerun.
        st.form_submit_button(
            tr("channels.copy_page_style"),
            key="chm_copy_page_style",
            icon=":material/content_copy:",
            on_click=_copy_page_style,
            args=(current_params,),
            help=tr("channels.copy_page_style_help"),
        )
        st.markdown(f"**{tr('channels.section.identity')}**")
        id_col, name_col = st.columns(2)
        id_col.text_input(tr("channels.id"), key=_key("id"), max_chars=48, help=tr("channels.id_help"))
        name_col.text_input(tr("channels.name"), key=_key("name"), max_chars=100)

        st.markdown(f"**{tr('channels.section.publishing')}**")
        user_col, platform_col = st.columns(2)
        user_col.text_input(
            tr("channels.upload_post_username"), key=_key("upload_post_username"),
            help=tr("channels.upload_post_username_help"),
        )
        platform_col.multiselect(
            tr("channels.platforms"), options=list(channel_service.SUPPORTED_PLATFORMS),
            key=_key("platforms"),
        )
        st.radio(
            tr("channels.publish_mode"),
            options=list(channel_service.PUBLISH_MODES),
            format_func=lambda mode: _mode_label(tr, mode),
            horizontal=True,
            key=_key("publish_mode"),
            help=tr("channels.publish_mode_help"),
        )
        times_col, tz_col, privacy_col = st.columns(3)
        times_col.text_input(
            tr("channels.posting_times"), key=_key("posting_times"),
            help=tr("channels.posting_times_help"),
        )
        timezones = list(COMMON_TIMEZONES)
        if st.session_state.get(_key("timezone")) not in timezones:
            timezones.insert(0, st.session_state.get(_key("timezone")) or "UTC")
        tz_col.selectbox(tr("channels.timezone"), options=timezones, key=_key("timezone"))
        privacy_col.selectbox(
            tr("channels.youtube_privacy"),
            options=list(channel_service.YOUTUBE_PRIVACY_STATUSES),
            key=_key("youtube_privacy_status"),
        )
        st.checkbox(tr("channels.made_for_kids"), key=_key("youtube_made_for_kids"))

        st.markdown(f"**{tr('channels.section.style')}**")
        st.caption(tr("channels.section.style_help"))
        columns = st.columns(3)
        for index, field in enumerate(_style_fields(tr)):
            if field["kind"] == "text":
                continue
            _render_style_field(tr, field, columns[index % 3])
        for field in _style_fields(tr):
            if field["kind"] == "text":
                _render_style_field(tr, field, st)
        extra = st.session_state.get(EXTRA_PARAMS_KEY) or {}
        if extra:
            st.caption(tr("channels.extra_params").format(count=len(extra)))

        save_col, cancel_col, _ = st.columns([1, 1, 4])
        saved = save_col.form_submit_button(
            tr("channels.create") if is_new else tr("channels.save"),
            type="primary",
            use_container_width=True,
        )
        cancel_col.form_submit_button(
            tr("channels.cancel"), on_click=_cancel_edit, use_container_width=True
        )
    if saved and _save_from_state(tr):
        st.rerun()


def _render_channel_row(tr, channel: channel_service.Channel, pending: int) -> None:
    with st.container(border=True, key=f"channel_row_{channel.id}"):
        info, publishing, actions = st.columns([2.2, 3, 2.6], vertical_alignment="center")
        info.markdown(f"**{channel.name}**  \n`{channel.id}` · {_mode_label(tr, channel.publish_mode)}")
        details = [
            tr("channels.row.platforms").format(value=", ".join(channel.platforms) or "-"),
            tr("channels.row.profile").format(value=channel.upload_post_username or "-"),
            tr("channels.row.times").format(
                value=", ".join(channel.posting_times) or tr("channels.row.right_away"),
                timezone=channel.timezone,
            ),
            tr("channels.row.style").format(count=len(channel.params)),
        ]
        if pending:
            details.append(tr("channels.row.pending").format(count=pending))
        publishing.caption("  \n".join(details))
        if st.session_state.get(DELETE_KEY) == channel.id:
            actions.warning(tr("channels.delete_confirm").format(name=channel.name))
            yes, no = actions.columns(2)
            yes.button(
                tr("channels.delete"), key=f"chm_confirm_delete_{channel.id}",
                type="primary", on_click=_confirm_delete, args=(channel.id,),
                use_container_width=True,
            )
            no.button(
                tr("channels.cancel"), key=f"chm_cancel_delete_{channel.id}",
                on_click=_cancel_delete, use_container_width=True,
            )
            return
        edit_col, use_col, delete_col = actions.columns(3)
        edit_col.button(
            tr("channels.edit"), key=f"chm_edit_{channel.id}", icon=":material/edit:",
            on_click=_start_edit, args=(channel.id,), use_container_width=True,
        )
        use_col.button(
            tr("channels.use"), key=f"chm_use_{channel.id}", icon=":material/check:",
            on_click=_use_channel, args=(channel.id,), use_container_width=True,
            help=tr("channels.use_help"),
        )
        delete_col.button(
            tr("channels.delete_short"), key=f"chm_delete_{channel.id}", icon=":material/delete:",
            on_click=_ask_delete, args=(channel.id,), use_container_width=True,
            help=tr("channels.delete_help"),
        )


def _render_list(tr) -> None:
    channels = channel_service.load_channels()
    st.button(
        tr("channels.new"), key="chm_new", type="primary", icon=":material/add:",
        on_click=_start_new,
    )
    if not channels:
        st.info(tr("channels.empty"))
        return
    pending_by_channel: dict[str, int] = {}
    for record in queue.list_publications(status=queue.STATUS_PENDING_APPROVAL):
        channel_id = record.get("channel_id")
        pending_by_channel[channel_id] = pending_by_channel.get(channel_id, 0) + 1
    for channel in channels.values():
        _render_channel_row(tr, channel, pending_by_channel.get(channel.id, 0))


def render_channel_manager(tr, current_params: dict | None = None) -> None:
    """Full-width channel manager, opened from the top bar or Settings."""
    if not st.session_state.get(MANAGER_OPEN_KEY):
        return
    with st.container(border=True, key="channel_manager_panel"):
        head, close = st.columns([8, 1], vertical_alignment="center")
        head.subheader(tr("channels.manager_title"))
        close.button("✕", key="chm_close", on_click=_close_manager, use_container_width=True)
        st.caption(tr("channels.settings_intro"))
        message = st.session_state.pop("channel_manager_message", None)
        if message:
            kind, name = message
            st.success(tr(f"channels.{kind}").format(name=name))
        if st.session_state.get(EDITING_KEY):
            _render_editor(tr, current_params or {})
        else:
            _render_list(tr)
        st.caption(
            tr("channels.settings_upload_post_hint")
            + "  \n"
            + tr("channels.file_hint").format(path=channel_service.channels_file())
        )
