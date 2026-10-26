"""Channel profiles: per-channel publishing and generation defaults.

Channels live in ``<MPT_HOME>/channels.toml`` instead of ``config.toml`` so the
WebUI and API processes always read the same, current definitions, and so the
upstream configuration format stays untouched. AI providers, voices and API
keys remain global; a channel only stores what differs between channels.

Example::

    [channels.receitas]
    name = "Receitas Rápidas"
    upload_post_username = "receitas"
    platforms = ["tiktok", "youtube"]
    publish_mode = "approval"        # manual | approval | auto
    posting_times = ["09:00", "18:30"]
    timezone = "America/Sao_Paulo"

    [channels.receitas.params]       # any VideoParams field except content
    voice_name = "pt-BR-FranciscaNeural-Female"
    video_aspect = "9:16"
"""

import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import toml
from loguru import logger
from pydantic import ValidationError

from app import paths
from app.models.schema import VideoParams
from app.utils.interprocess import file_lock, write_text_atomic

CHANNELS_FILE_NAME = "channels.toml"
PUBLISH_MODES = ("manual", "approval", "auto")
SUPPORTED_PLATFORMS = ("tiktok", "instagram", "youtube", "facebook")
YOUTUBE_PRIVACY_STATUSES = ("public", "unlisted", "private")
CHANNEL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
POSTING_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
# Content belongs to each video, and local file paths do not travel between
# machines, so a channel never stores these fields.
EXCLUDED_PARAM_KEYS = frozenset(
    {
        "video_subject",
        "video_script",
        "video_terms",
        "video_materials",
        "custom_audio_file",
        "bgm_file",
        "channel_id",
    }
)
# Scheduling searches this far ahead for a free posting slot.
_MAX_SCHEDULE_DAYS = 120
_MIN_SCHEDULE_LEAD = timedelta(minutes=2)

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"mtime": None, "channels": {}}


class ChannelError(ValueError):
    """Raised for an invalid channel definition or an unknown channel."""


@dataclass
class Channel:
    id: str
    name: str
    upload_post_username: str = ""
    platforms: list[str] = field(default_factory=list)
    publish_mode: str = "approval"
    posting_times: list[str] = field(default_factory=list)
    timezone: str = "UTC"
    youtube_privacy_status: str = "public"
    youtube_made_for_kids: bool = False
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "upload_post_username": self.upload_post_username,
            "platforms": list(self.platforms),
            "publish_mode": self.publish_mode,
            "posting_times": list(self.posting_times),
            "timezone": self.timezone,
            "youtube_privacy_status": self.youtube_privacy_status,
            "youtube_made_for_kids": self.youtube_made_for_kids,
            "params": dict(self.params),
        }

    def _toml_table(self) -> dict[str, Any]:
        table = self.to_dict()
        table.pop("id")
        if not table["params"]:
            table.pop("params")
        return table


def channels_file() -> str:
    return os.path.join(str(paths.runtime_root()), CHANNELS_FILE_NAME)


def _lock_file() -> str:
    return channels_file() + ".lock"


def clean_channel_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only valid, portable VideoParams fields for a channel preset."""
    if not params:
        return {}
    if not isinstance(params, dict):
        raise ChannelError("channel params must be a table")
    allowed = set(VideoParams.model_fields) - EXCLUDED_PARAM_KEYS
    unknown = sorted(key for key in params if key not in VideoParams.model_fields)
    if unknown:
        raise ChannelError(f"unknown video params: {', '.join(unknown)}")
    cleaned = {key: value for key, value in params.items() if key in allowed}
    # Validate the values with the real model so a bad preset fails when it
    # is saved, not halfway through a paid generation.
    try:
        validated = VideoParams.model_validate({"video_subject": "", **cleaned})
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise ChannelError(f"invalid video params: {problems}") from exc
    dumped = validated.model_dump(mode="json")
    return {key: dumped[key] for key in cleaned}


def validate_channel(channel: Channel) -> Channel:
    channel_id = str(channel.id or "").strip().lower()
    if not CHANNEL_ID_PATTERN.match(channel_id):
        raise ChannelError(
            "channel id must use lowercase letters, digits, '-' or '_' "
            "(1-48 characters)"
        )
    name = str(channel.name or "").strip() or channel_id
    platforms = []
    for platform in channel.platforms or []:
        platform = str(platform).strip().lower()
        if platform not in SUPPORTED_PLATFORMS:
            raise ChannelError(f"unsupported platform: {platform}")
        if platform not in platforms:
            platforms.append(platform)
    publish_mode = str(channel.publish_mode or "approval").strip().lower()
    if publish_mode not in PUBLISH_MODES:
        raise ChannelError(f"publish_mode must be one of: {', '.join(PUBLISH_MODES)}")
    posting_times = sorted(
        {str(value).strip() for value in channel.posting_times or [] if str(value).strip()}
    )
    for value in posting_times:
        if not POSTING_TIME_PATTERN.match(value):
            raise ChannelError(f"posting time must use HH:MM (24h): {value}")
    tz_name = str(channel.timezone or "UTC").strip() or "UTC"
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ChannelError(f"unknown timezone: {tz_name}") from exc
    privacy = str(channel.youtube_privacy_status or "public").strip().lower()
    if privacy not in YOUTUBE_PRIVACY_STATUSES:
        raise ChannelError(
            f"youtube_privacy_status must be one of: {', '.join(YOUTUBE_PRIVACY_STATUSES)}"
        )
    if not isinstance(channel.youtube_made_for_kids, bool):
        raise ChannelError("youtube_made_for_kids must be true or false")
    username = str(channel.upload_post_username or "").strip()
    if publish_mode != "manual" and not username:
        raise ChannelError("upload_post_username is required unless publish_mode is manual")
    if publish_mode != "manual" and not platforms:
        raise ChannelError("select at least one platform unless publish_mode is manual")
    return Channel(
        id=channel_id,
        name=name,
        upload_post_username=username,
        platforms=platforms,
        publish_mode=publish_mode,
        posting_times=posting_times,
        timezone=tz_name,
        youtube_privacy_status=privacy,
        youtube_made_for_kids=channel.youtube_made_for_kids,
        params=clean_channel_params(channel.params),
    )


def channel_from_mapping(channel_id: str, table: dict[str, Any]) -> Channel:
    if not isinstance(table, dict):
        raise ChannelError(f"channel {channel_id} must be a table")
    return validate_channel(
        Channel(
            id=channel_id,
            name=table.get("name", ""),
            upload_post_username=table.get("upload_post_username", ""),
            platforms=list(table.get("platforms") or []),
            publish_mode=table.get("publish_mode", "approval"),
            posting_times=list(table.get("posting_times") or []),
            timezone=table.get("timezone", "UTC"),
            youtube_privacy_status=table.get("youtube_privacy_status", "public"),
            youtube_made_for_kids=table.get("youtube_made_for_kids", False),
            params=dict(table.get("params") or {}),
        )
    )


def _read_file() -> dict[str, Any]:
    path = channels_file()
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        data = toml.load(handle)
    tables = data.get("channels", {})
    if not isinstance(tables, dict):
        raise ChannelError("[channels] must be a table")
    return tables


def load_channels() -> dict[str, Channel]:
    """Return valid channels in file order, re-reading the file when it changes.

    An invalid channel is skipped with a warning so one typo cannot block
    every other channel.
    """
    path = channels_file()
    try:
        stat = os.stat(path)
        mtime = (path, stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        mtime = None
    with _cache_lock:
        if _cache["mtime"] == mtime and mtime is not None:
            return dict(_cache["channels"])
    channels: dict[str, Channel] = {}
    if mtime is not None:
        try:
            tables = _read_file()
        except (OSError, toml.TomlDecodeError, ChannelError) as exc:
            logger.error(f"failed to read {CHANNELS_FILE_NAME}: {exc}")
            tables = {}
        for raw_id, table in tables.items():
            try:
                channel = channel_from_mapping(str(raw_id), table)
            except ChannelError as exc:
                logger.warning(f"skip invalid channel {raw_id!r}: {exc}")
                continue
            channels[channel.id] = channel
    with _cache_lock:
        _cache["mtime"] = mtime
        _cache["channels"] = dict(channels)
    return channels


def get_channel(channel_id: str | None) -> Channel | None:
    if not channel_id:
        return None
    return load_channels().get(str(channel_id).strip().lower())


def require_channel(channel_id: str) -> Channel:
    channel = get_channel(channel_id)
    if channel is None:
        raise ChannelError(f"unknown channel: {channel_id}")
    return channel


def _write_tables(tables: dict[str, Any]) -> None:
    write_text_atomic(channels_file(), toml.dumps({"channels": tables}))
    with _cache_lock:
        _cache["mtime"] = None


def save_channel(channel: Channel, previous_id: str | None = None) -> Channel:
    """Create or replace a channel; ``previous_id`` renames an existing one."""
    channel = validate_channel(channel)
    with file_lock(_lock_file()):
        tables = _read_file()
        previous_id = (previous_id or "").strip().lower()
        if previous_id and previous_id != channel.id:
            if channel.id in tables:
                raise ChannelError(f"channel already exists: {channel.id}")
            tables.pop(previous_id, None)
        tables[channel.id] = channel._toml_table()
        _write_tables(tables)
    logger.info(f"channel saved: {channel.id}")
    return channel


def delete_channel(channel_id: str) -> bool:
    channel_id = str(channel_id or "").strip().lower()
    with file_lock(_lock_file()):
        tables = _read_file()
        if channel_id not in tables:
            return False
        tables.pop(channel_id)
        _write_tables(tables)
    logger.info(f"channel deleted: {channel_id}")
    return True


def apply_channel_defaults(
    params: VideoParams,
    channel: Channel,
    explicit_fields: Iterable[str] | None = None,
) -> VideoParams:
    """Return a copy of ``params`` filled with the channel's defaults.

    Values the caller set explicitly always win; ``explicit_fields`` defaults
    to the fields set on the model (pydantic's ``model_fields_set``).
    """
    explicit = set(params.model_fields_set if explicit_fields is None else explicit_fields)
    data = params.model_dump()
    for key, value in channel.params.items():
        if key in EXCLUDED_PARAM_KEYS or key in explicit or key not in data:
            continue
        data[key] = value
    data["channel_id"] = channel.id
    return type(params).model_validate(data)


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def next_posting_slot(
    channel: Channel,
    now: datetime | None = None,
    taken: Iterable[str | datetime] = (),
) -> datetime | None:
    """Return the next free posting time (UTC) or ``None`` to publish now.

    ``taken`` holds slots already used by this channel's queued or published
    videos; two videos never share a slot.
    """
    if not channel.posting_times:
        return None
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    taken_minutes = set()
    for value in taken:
        slot = value if isinstance(value, datetime) else _parse_utc(value)
        if slot is not None:
            taken_minutes.add(slot.astimezone(timezone.utc).replace(second=0, microsecond=0))
    zone = ZoneInfo(channel.timezone)
    local_today = now.astimezone(zone).date()
    earliest = now + _MIN_SCHEDULE_LEAD
    for day_offset in range(_MAX_SCHEDULE_DAYS):
        day = local_today + timedelta(days=day_offset)
        for posting_time in channel.posting_times:
            hour, minute = (int(part) for part in posting_time.split(":"))
            local_slot = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
            slot = local_slot.astimezone(timezone.utc)
            if slot < earliest or slot in taken_minutes:
                continue
            return slot
    raise ChannelError(
        f"no free posting slot in the next {_MAX_SCHEDULE_DAYS} days for {channel.id}"
    )
