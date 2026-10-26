"""Persistent publication queue for channel videos.

Every finished video of a channel becomes one JSON record under
``storage/publications``. Records move through these states::

    pending_approval --approve--> scheduled --claim--> publishing --> published
            |                         |                     |
            +--reject--> rejected <---+                     +--> failed --retry--> scheduled

The queue is file based because WebUI and API run in separate processes that
only share the data volume. Every state change happens under one file lock, so
a video is claimed for publishing exactly once even when both processes run
the scheduler.
"""

import json
import os
import socket
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from uuid import uuid4

from loguru import logger

from app.services import channels as channel_service
from app.utils import utils
from app.utils.interprocess import file_lock, write_text_atomic

STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_SCHEDULED = "scheduled"
STATUS_PUBLISHING = "publishing"
STATUS_PUBLISHED = "published"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"
STATUSES = (
    STATUS_PENDING_APPROVAL,
    STATUS_SCHEDULED,
    STATUS_PUBLISHING,
    STATUS_PUBLISHED,
    STATUS_FAILED,
    STATUS_REJECTED,
)
EDITABLE_STATUSES = frozenset({STATUS_PENDING_APPROVAL, STATUS_SCHEDULED, STATUS_FAILED})
REMOVABLE_STATUSES = frozenset({STATUS_PUBLISHED, STATUS_FAILED, STATUS_REJECTED})
# Slots used by these records are not offered to another video of the channel.
SLOT_HOLDING_STATUSES = frozenset({STATUS_SCHEDULED, STATUS_PUBLISHING, STATUS_PUBLISHED})
# A publishing worker refreshes its heartbeat while Upload-Post is busy. A
# record without a fresh heartbeat lost its worker (crash or restart).
HEARTBEAT_INTERVAL_SECONDS = 60
STALE_PUBLISHING_AFTER = timedelta(minutes=10)
MAX_TITLE_LENGTH = 100
MAX_CAPTION_LENGTH = 2200
MAX_HASHTAGS = 30
_PROCESS_OWNER = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
# Map Upload-Post platform ids to the social platform names llm.py accepts.
_SOCIAL_PLATFORMS = {
    "tiktok": "tiktok",
    "instagram": "instagram_reels",
    "facebook": "facebook_reels",
    "youtube": "youtube_shorts",
}


class PublicationError(ValueError):
    """Raised when a queue operation is not allowed for the record."""


def process_owner() -> str:
    return _PROCESS_OWNER


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_time(value: str | datetime | None) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise PublicationError(f"invalid date/time: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def queue_dir() -> str:
    return utils.storage_dir("publications", create=True)


def _lock_path() -> str:
    return os.path.join(queue_dir(), ".queue.lock")


def _record_path(publication_id: str) -> str:
    if not publication_id or not all(c.isalnum() or c == "-" for c in publication_id):
        raise PublicationError(f"invalid publication id: {publication_id!r}")
    return os.path.join(queue_dir(), f"{publication_id}.json")


def _read(publication_id: str) -> dict[str, Any] | None:
    path = _record_path(publication_id)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None


def _write(record: dict[str, Any]) -> None:
    record["updated_at"] = _iso(_now())
    write_text_atomic(
        _record_path(record["id"]),
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
    )


def _all_records() -> list[dict[str, Any]]:
    records = []
    for name in os.listdir(queue_dir()):
        if not name.endswith(".json") or name.startswith("."):
            continue
        try:
            with open(os.path.join(queue_dir(), name), "r", encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError) as exc:
            logger.warning(f"skip unreadable publication record {name}: {exc}")
            continue
        if isinstance(record, dict) and record.get("id"):
            records.append(record)
    return records


def _require(publication_id: str) -> dict[str, Any]:
    record = _read(publication_id)
    if record is None:
        raise PublicationError(f"publication not found: {publication_id}")
    return record


def _normalize_hashtags(hashtags: Iterable[str] | str | None) -> list[str]:
    if hashtags is None:
        return []
    if isinstance(hashtags, str):
        hashtags = hashtags.replace(",", " ").split()
    normalized = []
    for tag in hashtags:
        tag = str(tag).strip().lstrip("#").strip()
        if tag and tag not in normalized:
            normalized.append(tag)
    return normalized[:MAX_HASHTAGS]


def list_publications(
    status: str | Iterable[str] | None = None,
    channel_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return records newest first, optionally filtered."""
    statuses = {status} if isinstance(status, str) else set(status or [])
    records = [
        record
        for record in _all_records()
        if (not statuses or record.get("status") in statuses)
        and (not channel_id or record.get("channel_id") == channel_id)
    ]
    records.sort(key=lambda record: record.get("created_at") or "", reverse=True)
    return records


def get_publication(publication_id: str) -> dict[str, Any] | None:
    return _read(publication_id)


def count_by_status() -> dict[str, int]:
    counts = {status: 0 for status in STATUSES}
    for record in _all_records():
        status = record.get("status")
        if status in counts:
            counts[status] += 1
    return counts


def _taken_slots(channel_id: str, exclude_id: str | None = None) -> list[str]:
    return [
        record["scheduled_at"]
        for record in _all_records()
        if record.get("channel_id") == channel_id
        and record.get("id") != exclude_id
        and record.get("status") in SLOT_HOLDING_STATUSES
        and record.get("scheduled_at")
    ]


def generate_metadata(
    video_subject: str,
    video_script: str,
    video_language: str,
    platforms: list[str],
) -> dict[str, Any]:
    """Title, caption and hashtags from the shared LLM (with a safe fallback)."""
    from app.services import llm

    social_platform = "youtube_shorts"
    if platforms and "youtube" not in platforms:
        social_platform = _SOCIAL_PLATFORMS.get(platforms[0], platforms[0])
    try:
        metadata = llm.generate_social_metadata(
            video_subject=video_subject or "",
            video_script=video_script or "",
            language=video_language or "",
            platform=social_platform,
        )
    except Exception as exc:  # metadata must never block the queue
        logger.warning(f"social metadata generation failed, using subject: {exc}")
        metadata = {}
    return {
        "title": str(metadata.get("title") or video_subject or "")[:MAX_TITLE_LENGTH],
        "caption": str(metadata.get("caption") or video_subject or "")[:MAX_CAPTION_LENGTH],
        "hashtags": _normalize_hashtags(metadata.get("hashtags")),
    }


def enqueue_task_videos(
    task_id: str,
    video_paths: list[str],
    channel: "channel_service.Channel",
    video_subject: str = "",
    video_script: str = "",
    video_language: str = "",
    metadata: dict[str, Any] | None = None,
) -> list[str]:
    """Queue every finished video of a task according to the channel mode.

    ``manual`` channels queue nothing. ``approval`` waits for a person;
    ``auto`` is scheduled immediately at the channel's next free slot.
    """
    if channel.publish_mode == "manual" or not video_paths:
        return []
    if metadata is None:
        metadata = generate_metadata(
            video_subject, video_script, video_language, channel.platforms
        )
    created_ids = []
    with file_lock(_lock_path()):
        for index, video_path in enumerate(video_paths, start=1):
            now = _now()
            record = {
                "id": uuid4().hex,
                "task_id": task_id,
                "video_index": index,
                "video_path": os.path.abspath(video_path),
                "channel_id": channel.id,
                "channel_name": channel.name,
                "upload_post_username": channel.upload_post_username,
                "platforms": list(channel.platforms),
                "youtube_privacy_status": channel.youtube_privacy_status,
                "youtube_made_for_kids": channel.youtube_made_for_kids,
                "video_subject": video_subject,
                "video_language": video_language,
                "title": metadata.get("title", ""),
                "caption": metadata.get("caption", ""),
                "hashtags": list(metadata.get("hashtags", [])),
                "status": STATUS_PENDING_APPROVAL,
                "scheduled_at": None,
                "created_at": _iso(now),
                "approved_at": None,
                "published_at": None,
                "attempts": 0,
                "owner": None,
                "heartbeat_at": None,
                "request_id": None,
                "results": None,
                "error": None,
            }
            if channel.publish_mode == "auto":
                slot = channel_service.next_posting_slot(
                    channel, now, _taken_slots(channel.id)
                )
                record["status"] = STATUS_SCHEDULED
                record["scheduled_at"] = _iso(slot or now)
                record["approved_at"] = _iso(now)
            _write(record)
            created_ids.append(record["id"])
    logger.info(
        f"queued {len(created_ids)} publication(s) for task {task_id} "
        f"on channel {channel.id} ({channel.publish_mode})"
    )
    return created_ids


def update_publication(
    publication_id: str,
    title: str | None = None,
    caption: str | None = None,
    hashtags: Iterable[str] | str | None = None,
    platforms: list[str] | None = None,
) -> dict[str, Any]:
    with file_lock(_lock_path()):
        record = _require(publication_id)
        if record.get("status") not in EDITABLE_STATUSES:
            raise PublicationError(f"cannot edit a {record.get('status')} publication")
        if title is not None:
            record["title"] = str(title).strip()[:MAX_TITLE_LENGTH]
        if caption is not None:
            record["caption"] = str(caption).strip()[:MAX_CAPTION_LENGTH]
        if hashtags is not None:
            record["hashtags"] = _normalize_hashtags(hashtags)
        if platforms is not None:
            cleaned = []
            for platform in platforms:
                platform = str(platform).strip().lower()
                if platform not in channel_service.SUPPORTED_PLATFORMS:
                    raise PublicationError(f"unsupported platform: {platform}")
                if platform not in cleaned:
                    cleaned.append(platform)
            if not cleaned:
                raise PublicationError("select at least one platform")
            record["platforms"] = cleaned
        _write(record)
        return record


def approve_publication(
    publication_id: str,
    scheduled_at: str | datetime | None = None,
    publish_now: bool = False,
) -> dict[str, Any]:
    """Approve (or reschedule) a video.

    Without an explicit time the channel's next free posting slot is used;
    channels without posting times publish right away.
    """
    with file_lock(_lock_path()):
        record = _require(publication_id)
        if record.get("status") not in {STATUS_PENDING_APPROVAL, STATUS_SCHEDULED}:
            raise PublicationError(
                f"cannot approve a {record.get('status')} publication"
            )
        now = _now()
        if publish_now:
            slot = now
        elif scheduled_at not in (None, ""):
            slot = parse_time(scheduled_at)
        else:
            channel = channel_service.get_channel(record.get("channel_id"))
            slot = None
            if channel is not None:
                slot = channel_service.next_posting_slot(
                    channel, now, _taken_slots(record["channel_id"], record["id"])
                )
            slot = slot or now
        record["status"] = STATUS_SCHEDULED
        record["scheduled_at"] = _iso(slot)
        record["approved_at"] = _iso(now)
        record["error"] = None
        _write(record)
        return record


def reject_publication(publication_id: str) -> dict[str, Any]:
    with file_lock(_lock_path()):
        record = _require(publication_id)
        if record.get("status") not in EDITABLE_STATUSES:
            raise PublicationError(f"cannot reject a {record.get('status')} publication")
        record["status"] = STATUS_REJECTED
        _write(record)
        return record


def retry_publication(publication_id: str) -> dict[str, Any]:
    """Queue a failed video again right away.

    A failure after Upload-Post accepted the request may already be live; the
    record keeps the remote ``request_id`` so the user can check first.
    """
    with file_lock(_lock_path()):
        record = _require(publication_id)
        if record.get("status") != STATUS_FAILED:
            raise PublicationError("only failed publications can be retried")
        record["status"] = STATUS_SCHEDULED
        record["scheduled_at"] = _iso(_now())
        record["error"] = None
        _write(record)
        return record


def delete_publication(publication_id: str) -> bool:
    with file_lock(_lock_path()):
        record = _read(publication_id)
        if record is None:
            return False
        if record.get("status") not in REMOVABLE_STATUSES:
            raise PublicationError(
                f"cannot remove a {record.get('status')} publication; reject it first"
            )
        os.remove(_record_path(publication_id))
        return True


def claim_due(limit: int, now: datetime | None = None) -> list[dict[str, Any]]:
    """Atomically move up to ``limit`` due records to ``publishing``."""
    if limit <= 0:
        return []
    now = now or _now()
    claimed = []
    with file_lock(_lock_path()):
        due = [
            record
            for record in _all_records()
            if record.get("status") == STATUS_SCHEDULED
            and (parse_time(record.get("scheduled_at")) or now) <= now
        ]
        due.sort(key=lambda record: record.get("scheduled_at") or "")
        for record in due[:limit]:
            record["status"] = STATUS_PUBLISHING
            record["owner"] = _PROCESS_OWNER
            record["heartbeat_at"] = _iso(now)
            record["attempts"] = int(record.get("attempts") or 0) + 1
            _write(record)
            claimed.append(record)
    return claimed


def heartbeat(publication_id: str) -> None:
    with file_lock(_lock_path()):
        record = _read(publication_id)
        if record and record.get("status") == STATUS_PUBLISHING and record.get(
            "owner"
        ) == _PROCESS_OWNER:
            record["heartbeat_at"] = _iso(_now())
            _write(record)


def record_request_id(publication_id: str, request_id: str) -> None:
    with file_lock(_lock_path()):
        record = _read(publication_id)
        if record is not None:
            record["request_id"] = request_id
            _write(record)


def finish_publication(
    publication_id: str,
    success: bool,
    results: Any = None,
    error: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any] | None:
    with file_lock(_lock_path()):
        record = _read(publication_id)
        if record is None:
            return None
        record["status"] = STATUS_PUBLISHED if success else STATUS_FAILED
        record["results"] = results
        record["error"] = None if success else (error or "unknown upload error")
        record["owner"] = None
        record["heartbeat_at"] = None
        if request_id:
            record["request_id"] = request_id
        if success:
            record["published_at"] = _iso(_now())
        _write(record)
        return record


def recover_stale(now: datetime | None = None) -> int:
    """Fail ``publishing`` records whose worker stopped sending heartbeats.

    They are not retried automatically: Upload-Post may already have posted
    the video, and a duplicate post is worse than a visible failure.
    """
    now = now or _now()
    recovered = 0
    with file_lock(_lock_path()):
        for record in _all_records():
            if record.get("status") != STATUS_PUBLISHING:
                continue
            beat = parse_time(record.get("heartbeat_at"))
            if beat is not None and now - beat < STALE_PUBLISHING_AFTER:
                continue
            request_id = record.get("request_id")
            hint = f"; check Upload-Post request_id {request_id}" if request_id else ""
            record["status"] = STATUS_FAILED
            record["error"] = f"publishing was interrupted before it finished{hint}"
            record["owner"] = None
            record["heartbeat_at"] = None
            _write(record)
            recovered += 1
    if recovered:
        logger.warning(f"recovered interrupted publications: {recovered}")
    return recovered
