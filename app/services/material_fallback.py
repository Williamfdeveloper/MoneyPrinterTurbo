"""Automatic fallback between stock-footage sources.

When the chosen stock source (Pexels, Pixabay or Coverr) fails or finds no
footage, the next source with a configured API key is tried. Paid AI video
sources never fall back: switching them silently could create unexpected
charges.
"""

from typing import Callable

from loguru import logger

from app.config import config

STOCK_SOURCES = ("pexels", "pixabay", "coverr")
DEFAULT_FALLBACK_ORDER = list(STOCK_SOURCES)


def _has_api_key(source: str) -> bool:
    keys = config.app.get(f"{source}_api_keys")
    if isinstance(keys, str):
        return bool(keys.strip())
    return any(str(key or "").strip() for key in keys or [])


def candidate_sources(primary: str) -> list[str]:
    """``primary`` first, then configured fallbacks that have an API key."""
    if primary not in STOCK_SOURCES:
        return [primary]
    order = config.app.get("video_source_fallbacks", DEFAULT_FALLBACK_ORDER)
    if isinstance(order, str):
        order = [part.strip() for part in order.split(",")]
    sources = [primary]
    for source in order or []:
        source = str(source).strip().lower()
        if source in STOCK_SOURCES and source not in sources and _has_api_key(source):
            sources.append(source)
    return sources


def download_with_fallback(
    primary: str, download: Callable[[str], list[str]]
) -> tuple[list[str], str]:
    """Run ``download(source)`` until one source returns footage.

    Returns the downloaded paths and the source that produced them. When every
    source fails, the primary source's exception is raised again so callers
    keep reporting the original error.
    """
    sources = candidate_sources(primary)
    primary_error: Exception | None = None
    for source in sources:
        try:
            videos = download(source)
        except Exception as exc:
            if len(sources) == 1:
                raise
            if source == primary:
                primary_error = exc
            logger.warning(f"video source {source} failed, trying the next one: {exc}")
            continue
        if videos:
            if source != primary:
                logger.warning(
                    f"used fallback video source {source} because {primary} "
                    "returned no footage"
                )
            return videos, source
        logger.warning(f"video source {source} returned no footage")
    if primary_error is not None:
        raise primary_error
    return [], primary
