"""Background worker that publishes due videos from the publication queue.

Both the WebUI and the API start this worker. The queue's file lock makes
claiming exclusive, so running it in several processes is safe and keeps
scheduled posts flowing when only one of them is up.
"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import toml
from loguru import logger

from app.config import config
from app.services import publication_queue as queue
from app.services import upload_post

DEFAULT_POLL_SECONDS = 30
MAX_PARALLEL_UPLOADS = 2

_start_lock = threading.Lock()
_started = False
_executor = ThreadPoolExecutor(
    max_workers=MAX_PARALLEL_UPLOADS, thread_name_prefix="mpt-publication"
)
_active_lock = threading.Lock()
_active_ids: set[str] = set()


def _fresh_app_config() -> dict:
    """Read ``[app]`` from disk; another process may have saved new keys."""
    try:
        with open(config.config_file, "r", encoding="utf-8") as handle:
            return dict(toml.load(handle).get("app", {}))
    except (OSError, toml.TomlDecodeError) as exc:
        logger.warning(f"could not re-read config.toml, using loaded values: {exc}")
        return dict(config.app)


def scheduler_enabled() -> bool:
    return bool(config.app.get("publication_scheduler_enabled", True))


def poll_seconds() -> int:
    try:
        return max(5, int(config.app.get("publication_poll_seconds", DEFAULT_POLL_SECONDS)))
    except (TypeError, ValueError):
        return DEFAULT_POLL_SECONDS


def build_upload_account(record: dict, app_config: dict | None = None) -> dict:
    app_config = _fresh_app_config() if app_config is None else app_config
    return {
        "upload_post_api_key": app_config.get("upload_post_api_key", ""),
        "upload_post_username": record.get("upload_post_username", ""),
        # Choosing a publishing channel is the opt-in; the global toggle only
        # controls the legacy single-account auto-publish.
        "upload_post_enabled": True,
    }


def build_post_payload(record: dict) -> tuple[str, dict | None]:
    """Return the post caption and the YouTube fields for a record."""
    hashtags = list(record.get("hashtags") or [])
    caption = str(record.get("caption") or record.get("title") or record.get("video_subject") or "")
    tag_text = " ".join(f"#{tag}" for tag in hashtags if f"#{tag}" not in caption)
    post_text = f"{caption}\n\n{tag_text}".strip() if tag_text else caption
    youtube_extra = None
    if "youtube" in (record.get("platforms") or []):
        youtube_extra = {
            "youtube_title": str(record.get("title") or record.get("video_subject") or caption)[:100],
            "youtube_description": post_text,
            "tags": hashtags,
            "privacyStatus": record.get("youtube_privacy_status", "public"),
            "selfDeclaredMadeForKids": bool(record.get("youtube_made_for_kids", False)),
            "containsSyntheticMedia": True,
        }
    return post_text or "Check out this video!", youtube_extra


def publish_record(record: dict) -> dict | None:
    """Upload one claimed record and store the outcome."""
    publication_id = record["id"]
    stop_heartbeat = threading.Event()

    def keep_alive():
        while not stop_heartbeat.wait(queue.HEARTBEAT_INTERVAL_SECONDS):
            try:
                queue.heartbeat(publication_id)
            except Exception as exc:
                logger.warning(f"publication heartbeat failed: {publication_id}: {exc}")

    heartbeat_thread = threading.Thread(
        target=keep_alive, name=f"mpt-heartbeat-{publication_id[:8]}", daemon=True
    )
    heartbeat_thread.start()
    try:
        video_path = record.get("video_path", "")
        if not video_path or not os.path.isfile(video_path):
            return queue.finish_publication(
                publication_id, False, error=f"video file not found: {video_path}"
            )
        account = build_upload_account(record)
        if not account["upload_post_api_key"]:
            return queue.finish_publication(
                publication_id,
                False,
                error="Upload-Post API key is not configured (Settings > Auto-publish)",
            )
        post_text, youtube_extra = build_post_payload(record)
        logger.info(
            f"publishing {publication_id} to {', '.join(record.get('platforms') or [])} "
            f"for channel {record.get('channel_id')}"
        )
        result = upload_post.cross_post_video(
            video_path=video_path,
            title=post_text,
            platforms=list(record.get("platforms") or []),
            youtube_extra=youtube_extra,
            on_background_start=lambda request_id: queue.record_request_id(
                publication_id, request_id
            ),
            account=account,
        )
        if not isinstance(result, dict):
            result = {"success": False, "error": "Upload-Post returned an invalid response"}
        success = bool(result.get("success"))
        return queue.finish_publication(
            publication_id,
            success,
            results=result.get("results"),
            error=None if success else str(result.get("error") or result.get("message") or ""),
            request_id=result.get("request_id"),
        )
    except Exception as exc:
        logger.exception(f"publication {publication_id} failed: {exc}")
        return queue.finish_publication(publication_id, False, error=str(exc))
    finally:
        stop_heartbeat.set()
        with _active_lock:
            _active_ids.discard(publication_id)


def run_once(wait: bool = False) -> int:
    """Recover stale work and start publishing due records; returns the count."""
    queue.recover_stale()
    with _active_lock:
        free_slots = MAX_PARALLEL_UPLOADS - len(_active_ids)
    claimed = queue.claim_due(free_slots)
    futures = []
    for record in claimed:
        with _active_lock:
            _active_ids.add(record["id"])
        futures.append(_executor.submit(publish_record, record))
    if wait:
        for future in futures:
            future.result()
    return len(claimed)


def _loop():
    logger.info(f"publication scheduler started (every {poll_seconds()}s)")
    stop = threading.Event()
    while True:
        try:
            run_once()
        except Exception as exc:
            logger.exception(f"publication scheduler iteration failed: {exc}")
        stop.wait(poll_seconds())


def ensure_started() -> bool:
    """Start the background worker once per process."""
    global _started
    if not scheduler_enabled():
        return False
    with _start_lock:
        if _started:
            return True
        threading.Thread(target=_loop, name="mpt-publication-scheduler", daemon=True).start()
        _started = True
    return True
