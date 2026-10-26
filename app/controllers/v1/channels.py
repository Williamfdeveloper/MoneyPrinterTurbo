"""API for channel profiles and the publication queue."""

from typing import Any, List, Optional

from fastapi import Depends, Request
from pydantic import BaseModel, Field

from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.exception import HttpException
from app.services import channels as channel_service
from app.services import publication_queue as queue
from app.utils import utils

router = new_router(dependencies=[Depends(base.verify_token)])


class ChannelBody(BaseModel):
    name: str = Field(default="", max_length=100)
    upload_post_username: str = Field(default="", max_length=200)
    platforms: List[str] = Field(default_factory=list)
    publish_mode: str = "approval"
    posting_times: List[str] = Field(default_factory=list)
    timezone: str = "UTC"
    youtube_privacy_status: str = "public"
    youtube_made_for_kids: bool = False
    params: dict[str, Any] = Field(default_factory=dict)


class PublicationEditBody(BaseModel):
    title: Optional[str] = Field(default=None, max_length=queue.MAX_TITLE_LENGTH)
    caption: Optional[str] = Field(default=None, max_length=queue.MAX_CAPTION_LENGTH)
    hashtags: Optional[List[str]] = None
    platforms: Optional[List[str]] = None


class PublicationApproveBody(BaseModel):
    # ISO-8601 time; without a timezone it is read as UTC. Omit it to use the
    # channel's next free posting slot.
    scheduled_at: Optional[str] = None
    publish_now: bool = False


def _bad_request(request: Request, exc: Exception, status_code: int = 400):
    request_id = base.get_task_id(request)
    raise HttpException(
        task_id=request_id, status_code=status_code, message=f"{request_id}: {exc}"
    )


def _not_found(request: Request, message: str):
    _bad_request(request, ValueError(message), status_code=404)


@router.get("/channels", summary="List channel profiles")
def list_channels(request: Request):
    channels = [channel.to_dict() for channel in channel_service.load_channels().values()]
    return utils.get_response(200, {"channels": channels})


@router.get("/channels/{channel_id}", summary="Get a channel profile")
def get_channel(request: Request, channel_id: str):
    channel = channel_service.get_channel(channel_id)
    if channel is None:
        _not_found(request, f"channel not found: {channel_id}")
    return utils.get_response(200, channel.to_dict())


@router.put("/channels/{channel_id}", summary="Create or replace a channel profile")
def save_channel(request: Request, channel_id: str, body: ChannelBody):
    try:
        channel = channel_service.save_channel(
            channel_service.Channel(id=channel_id, **body.model_dump())
        )
    except channel_service.ChannelError as exc:
        _bad_request(request, exc)
    return utils.get_response(200, channel.to_dict())


@router.delete("/channels/{channel_id}", summary="Delete a channel profile")
def delete_channel(request: Request, channel_id: str):
    if not channel_service.delete_channel(channel_id):
        _not_found(request, f"channel not found: {channel_id}")
    return utils.get_response(200, {"deleted": channel_id})


@router.get("/publications", summary="List queued and published videos")
def list_publications(
    request: Request, status: Optional[str] = None, channel_id: Optional[str] = None
):
    if status and status not in queue.STATUSES:
        _bad_request(request, ValueError(f"status must be one of: {', '.join(queue.STATUSES)}"))
    records = queue.list_publications(status=status, channel_id=channel_id)
    return utils.get_response(200, {"publications": records, "counts": queue.count_by_status()})


@router.get("/publications/{publication_id}", summary="Get a publication")
def get_publication(request: Request, publication_id: str):
    try:
        record = queue.get_publication(publication_id)
    except queue.PublicationError as exc:
        _bad_request(request, exc)
    if record is None:
        _not_found(request, f"publication not found: {publication_id}")
    return utils.get_response(200, record)


def _queue_action(request: Request, action, *args, **kwargs):
    try:
        record = action(*args, **kwargs)
    except queue.PublicationError as exc:
        status_code = 404 if "not found" in str(exc) else 409
        _bad_request(request, exc, status_code=status_code)
    except channel_service.ChannelError as exc:
        _bad_request(request, exc, status_code=409)
    return utils.get_response(200, record)


@router.patch("/publications/{publication_id}", summary="Edit title, caption or hashtags")
def edit_publication(request: Request, publication_id: str, body: PublicationEditBody):
    return _queue_action(
        request,
        queue.update_publication,
        publication_id,
        title=body.title,
        caption=body.caption,
        hashtags=body.hashtags,
        platforms=body.platforms,
    )


@router.post("/publications/{publication_id}/approve", summary="Approve or reschedule")
def approve_publication(
    request: Request, publication_id: str, body: PublicationApproveBody | None = None
):
    body = body or PublicationApproveBody()
    return _queue_action(
        request,
        queue.approve_publication,
        publication_id,
        scheduled_at=body.scheduled_at,
        publish_now=body.publish_now,
    )


@router.post("/publications/{publication_id}/reject", summary="Reject a video")
def reject_publication(request: Request, publication_id: str):
    return _queue_action(request, queue.reject_publication, publication_id)


@router.post("/publications/{publication_id}/retry", summary="Retry a failed publication")
def retry_publication(request: Request, publication_id: str):
    return _queue_action(request, queue.retry_publication, publication_id)


@router.delete("/publications/{publication_id}", summary="Remove a finished publication")
def delete_publication(request: Request, publication_id: str):
    try:
        deleted = queue.delete_publication(publication_id)
    except queue.PublicationError as exc:
        _bad_request(request, exc, status_code=409)
    if not deleted:
        _not_found(request, f"publication not found: {publication_id}")
    return utils.get_response(200, {"deleted": publication_id})
