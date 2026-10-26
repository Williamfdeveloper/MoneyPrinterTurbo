"""WebUI for channel profiles and the publication queue.

Kept outside Main.py so the fork's additions stay easy to merge with upstream.
Main.py passes its ``tr`` function in; nothing here imports Main.
"""

from datetime import datetime, time as dt_time, timezone
from zoneinfo import ZoneInfo

import streamlit as st
from loguru import logger

from app.models.schema import VideoParams
from app.services import channels as channel_service
from app.services import publication_queue as queue
from app.services import publication_scheduler

SELECTOR_KEY = "mpt_channel_selector"
CURRENT_PARAMS_KEY = "mpt_current_params"
QUEUE_OPEN_KEY = "publication_queue_open"
# Fields the channel switch must never overwrite on the page.
_CONTENT_KEYS = ("video_subject", "video_script", "video_terms")
_PRESET_EXCLUDED = ("video_materials", "custom_audio_file", "bgm_file")
_STATUS_TABS = (
    queue.STATUS_PENDING_APPROVAL,
    queue.STATUS_SCHEDULED,
    queue.STATUS_PUBLISHED,
    queue.STATUS_FAILED,
    queue.STATUS_REJECTED,
)

# The WebUI process publishes scheduled posts as well (see the scheduler).
publication_scheduler.ensure_started()


def selected_channel_id() -> str:
    channel_id = st.session_state.get(SELECTOR_KEY) or ""
    if channel_id and channel_service.get_channel(channel_id) is None:
        return ""
    return channel_id


def remember_current_params(params: VideoParams) -> None:
    """Main.py calls this every run so a channel switch keeps the page content."""
    st.session_state[CURRENT_PARAMS_KEY] = params.model_dump(mode="json")


def _apply_channel_preset():
    channel = channel_service.get_channel(st.session_state.get(SELECTOR_KEY) or "")
    if channel is None or not channel.params:
        return
    current = dict(st.session_state.get(CURRENT_PARAMS_KEY) or {})
    merged = {**current, **channel.params}
    for key in _CONTENT_KEYS:
        merged[key] = current.get(key) or ("" if key != "video_terms" else None)
    for key in _PRESET_EXCLUDED:
        merged.pop(key, None)
    merged["channel_id"] = channel.id
    try:
        payload = VideoParams.model_validate(merged).model_dump(mode="json")
    except Exception as exc:
        logger.warning(f"could not apply channel preset {channel.id}: {exc}")
        return
    # Main.py applies this payload to the widgets before rendering them.
    st.session_state["settings_preset_payload"] = payload
    logger.info(f"applied channel preset: {channel.id}")


def _toggle_queue():
    st.session_state[QUEUE_OPEN_KEY] = not st.session_state.get(QUEUE_OPEN_KEY, False)


def render_top_bar_controls(tr) -> None:
    """Channel selector and publication-queue button for the top bar."""
    channels = channel_service.load_channels()
    if channels:
        options = [""] + list(channels)
        if st.session_state.get(SELECTOR_KEY) not in options:
            st.session_state[SELECTOR_KEY] = ""
        st.selectbox(
            tr("channels.selector"),
            options=options,
            format_func=lambda value: channels[value].name if value else tr("channels.none"),
            key=SELECTOR_KEY,
            on_change=_apply_channel_preset,
            label_visibility="collapsed",
            width=200,
            help=tr("channels.selector_help"),
        )
    from webui import channel_manager_ui

    st.button(
        tr("channels.manager_button"),
        key="channel_manager_button",
        icon=":material/hub:",
        width="content",
        on_click=channel_manager_ui.open_manager,
    )
    pending = queue.count_by_status().get(queue.STATUS_PENDING_APPROVAL, 0)
    label = tr("publication.queue_button")
    if pending:
        label = f"{label} ({pending})"
    st.button(
        label,
        key="publication_queue_button",
        icon=":material/schedule_send:",
        type="primary" if pending else "secondary",
        width="content",
        on_click=_toggle_queue,
    )


def render_task_publication_note(tr, task) -> None:
    """Tell the user where a finished channel video went."""
    channel_id = task.get("channel_id")
    if not channel_id:
        return
    channel = channel_service.get_channel(channel_id)
    name = channel.name if channel else channel_id
    if task.get("publication_error"):
        st.error(tr("publication.queue_failed").format(error=task["publication_error"]))
    elif task.get("publication_ids"):
        st.info(tr("publication.queued_note").format(channel=name))
    else:
        st.caption(tr("publication.manual_note").format(channel=name))


def _channel_zone(record) -> ZoneInfo:
    channel = channel_service.get_channel(record.get("channel_id"))
    try:
        return ZoneInfo(channel.timezone if channel else "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _format_time(value, zone: ZoneInfo) -> str:
    moment = queue.parse_time(value)
    if moment is None:
        return "-"
    return moment.astimezone(zone).strftime("%d/%m/%Y %H:%M")


def _run_action(tr, action, *args, success_key: str = "", **kwargs) -> None:
    try:
        action(*args, **kwargs)
    except (queue.PublicationError, channel_service.ChannelError) as exc:
        st.error(tr("publication.action_failed").format(error=exc))
        return
    if success_key:
        st.toast(tr(success_key))
    # A full rerun also refreshes the pending count on the top-bar button.
    st.rerun()


def _render_record_editor(tr, record, disabled: bool) -> tuple[str, str, str]:
    record_id = record["id"]
    title = st.text_input(
        tr("publication.title"),
        value=record.get("title", ""),
        max_chars=queue.MAX_TITLE_LENGTH,
        key=f"pub_title_{record_id}",
        disabled=disabled,
    )
    caption = st.text_area(
        tr("publication.caption"),
        value=record.get("caption", ""),
        max_chars=queue.MAX_CAPTION_LENGTH,
        height=110,
        key=f"pub_caption_{record_id}",
        disabled=disabled,
    )
    hashtags = st.text_input(
        tr("publication.hashtags"),
        value=" ".join(f"#{tag}" for tag in record.get("hashtags") or []),
        key=f"pub_hashtags_{record_id}",
        disabled=disabled,
        help=tr("publication.hashtags_help"),
    )
    return title, caption, hashtags


def _save_edits(record, title, caption, hashtags) -> None:
    if (
        title != record.get("title", "")
        or caption != record.get("caption", "")
        or queue._normalize_hashtags(hashtags) != (record.get("hashtags") or [])
    ):
        queue.update_publication(record["id"], title=title, caption=caption, hashtags=hashtags)


def _render_schedule_inputs(tr, record, zone: ZoneInfo):
    record_id = record["id"]
    current = queue.parse_time(record.get("scheduled_at"))
    default = (current or datetime.now(timezone.utc)).astimezone(zone)
    date_col, time_col = st.columns(2)
    chosen_date = date_col.date_input(
        tr("publication.date"),
        value=default.date(),
        key=f"pub_date_{record_id}",
        format="DD/MM/YYYY",
    )
    chosen_time = time_col.time_input(
        tr("publication.time"),
        value=dt_time(default.hour, default.minute),
        step=300,
        key=f"pub_time_{record_id}",
    )
    return datetime.combine(chosen_date, chosen_time, tzinfo=zone)


def _render_record(tr, record) -> None:
    record_id = record["id"]
    status = record.get("status")
    zone = _channel_zone(record)
    with st.container(border=True, key=f"publication_{record_id}"):
        header = f"**{record.get('channel_name') or record.get('channel_id')}** · " + ", ".join(
            record.get("platforms") or []
        )
        st.markdown(header)
        st.caption(
            tr("publication.meta").format(
                subject=record.get("video_subject") or "-",
                created=_format_time(record.get("created_at"), zone),
                timezone=zone.key,
            )
        )
        video_col, form_col = st.columns([1, 2])
        with video_col:
            video_path = record.get("video_path", "")
            try:
                st.video(video_path)
            except Exception:
                st.warning(tr("publication.video_missing"))
        with form_col:
            editable = status in queue.EDITABLE_STATUSES
            title, caption, hashtags = _render_record_editor(tr, record, not editable)
            if status == queue.STATUS_SCHEDULED:
                st.info(
                    tr("publication.scheduled_for").format(
                        time=_format_time(record.get("scheduled_at"), zone)
                    )
                )
            if status == queue.STATUS_PUBLISHED:
                st.success(
                    tr("publication.published_at").format(
                        time=_format_time(record.get("published_at"), zone)
                    )
                )
            if record.get("error"):
                st.error(record["error"])
            if record.get("request_id"):
                st.caption(f"Upload-Post request_id: {record['request_id']}")

            if status in (queue.STATUS_PENDING_APPROVAL, queue.STATUS_SCHEDULED):
                scheduled_at = _render_schedule_inputs(tr, record, zone)
                buttons = st.columns(4)
                if buttons[0].button(
                    tr("publication.approve_next_slot"),
                    key=f"pub_approve_{record_id}",
                    type="primary",
                    use_container_width=True,
                    help=tr("publication.approve_next_slot_help"),
                ):
                    _save_edits(record, title, caption, hashtags)
                    _run_action(
                        tr, queue.approve_publication, record_id,
                        success_key="publication.approved",
                    )
                if buttons[1].button(
                    tr("publication.schedule_chosen"),
                    key=f"pub_schedule_{record_id}",
                    use_container_width=True,
                ):
                    _save_edits(record, title, caption, hashtags)
                    _run_action(
                        tr, queue.approve_publication, record_id,
                        scheduled_at=scheduled_at, success_key="publication.approved",
                    )
                if buttons[2].button(
                    tr("publication.publish_now"),
                    key=f"pub_now_{record_id}",
                    use_container_width=True,
                ):
                    _save_edits(record, title, caption, hashtags)
                    _run_action(
                        tr, queue.approve_publication, record_id,
                        publish_now=True, success_key="publication.approved",
                    )
                if buttons[3].button(
                    tr("publication.reject"),
                    key=f"pub_reject_{record_id}",
                    use_container_width=True,
                ):
                    _run_action(
                        tr, queue.reject_publication, record_id,
                        success_key="publication.rejected",
                    )
            elif status == queue.STATUS_FAILED:
                buttons = st.columns(3)
                if buttons[0].button(
                    tr("publication.retry"),
                    key=f"pub_retry_{record_id}",
                    type="primary",
                    use_container_width=True,
                    help=tr("publication.retry_help"),
                ):
                    _save_edits(record, title, caption, hashtags)
                    _run_action(
                        tr, queue.retry_publication, record_id,
                        success_key="publication.approved",
                    )
                if buttons[1].button(
                    tr("publication.reject"),
                    key=f"pub_reject_{record_id}",
                    use_container_width=True,
                ):
                    _run_action(tr, queue.reject_publication, record_id)
                if buttons[2].button(
                    tr("publication.remove"),
                    key=f"pub_remove_{record_id}",
                    use_container_width=True,
                ):
                    _run_action(tr, queue.delete_publication, record_id)
            elif status in (queue.STATUS_PUBLISHED, queue.STATUS_REJECTED):
                if st.button(
                    tr("publication.remove"),
                    key=f"pub_remove_{record_id}",
                    help=tr("publication.remove_help"),
                ):
                    _run_action(tr, queue.delete_publication, record_id)


@st.fragment
def _render_queue_body(tr) -> None:
    counts = queue.count_by_status()
    labels = [f"{tr(f'publication.status.{status}')} ({counts.get(status, 0)})" for status in _STATUS_TABS]
    tabs = st.tabs(labels, key="publication_status_tabs")
    for status, tab in zip(_STATUS_TABS, tabs):
        with tab:
            records = queue.list_publications(status=status)
            if status == queue.STATUS_SCHEDULED:
                records.sort(key=lambda record: record.get("scheduled_at") or "")
            if not records:
                st.info(tr("publication.empty"))
            for record in records[:50]:
                _render_record(tr, record)


def render_publication_queue(tr) -> None:
    if not st.session_state.get(QUEUE_OPEN_KEY):
        return
    with st.container(border=True, key="publication_queue_panel"):
        head, refresh, close = st.columns([6, 1, 1], vertical_alignment="center")
        head.subheader(tr("publication.queue_title"))
        if refresh.button(tr("publication.refresh"), key="publication_queue_refresh", use_container_width=True):
            st.rerun()
        if close.button("✕", key="publication_queue_close", use_container_width=True):
            st.session_state[QUEUE_OPEN_KEY] = False
            st.rerun()
        st.caption(tr("publication.queue_help"))
        if not publication_scheduler.scheduler_enabled():
            st.warning(tr("publication.scheduler_disabled"))
        _render_queue_body(tr)


def _open_manager_from_settings():
    from webui import channel_manager_ui

    channel_manager_ui.open_manager()
    st.session_state["settings_dialog_open"] = False


def render_channel_settings(tr) -> None:
    """Settings tab: short intro and a shortcut to the channel manager screen."""
    st.write(tr("channels.settings_intro"))
    channels = channel_service.load_channels()
    st.caption(tr("channels.settings_count").format(count=len(channels)))
    st.button(
        tr("channels.open_manager"),
        key="settings_open_channel_manager",
        type="primary",
        icon=":material/hub:",
        on_click=_open_manager_from_settings,
    )
