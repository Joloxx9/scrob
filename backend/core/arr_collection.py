"""Read what Radarr and Sonarr actually have on disk, shaped like Jellyfin items.

The collection sync (routers/arr_sync.py) feeds these items through the same
sync_items() path as a Jellyfin library pull, so matching, show/episode
creation, TVDB-order folding and stale-file pruning all behave the same way.
Only items with a file on disk are returned: a monitored-but-missing title is
not part of the collection.

Each item's "Id" is prefixed with the app name so a Radarr movie id and a
Sonarr episode-file id can never collide as CollectionFile.source_id.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

TIMEOUT = 60.0
SONARR_CONCURRENCY = 4


class ArrError(Exception):
    """Radarr/Sonarr could not be read; the sync must not prune anything."""


async def _get_json(client: httpx.AsyncClient, url: str, token: str, path: str, params: dict | None = None) -> Any:
    try:
        r = await client.get(f"{url.rstrip('/')}{path}", headers={"X-Api-Key": token}, params=params)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise ArrError(f"GET {path} failed: {e}") from e


def _resolution_dims(media_info: dict, quality: dict) -> tuple[int, int]:
    """(width, height) from mediaInfo.resolution ("1920x1080"), else from the
    quality profile's resolution (1080 -> 1920x1080)."""
    raw = str((media_info or {}).get("resolution") or "")
    if "x" in raw:
        w, _, h = raw.partition("x")
        if w.strip().isdigit() and h.strip().isdigit():
            return int(w), int(h)
    height = ((quality or {}).get("quality") or {}).get("resolution") or 0
    try:
        height = int(height)
    except (TypeError, ValueError):
        height = 0
    return (height * 16 // 9, height) if height else (0, 0)


def _channels(value: Any) -> int:
    """mediaInfo.audioChannels is a float like 5.1 or 2; Jellyfin counts 6 / 2."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0
    whole = int(f)
    lfe = 1 if round((f - whole) * 10) >= 1 else 0
    return whole + lfe


def file_media_streams(file: dict | None) -> list[dict]:
    """Synthesise Jellyfin MediaStreams from an arr movieFile/episodeFile so
    extract_quality() can fill resolution and codecs. Languages are left out:
    the arr apps report display names, Jellyfin ISO codes."""
    if not file:
        return []
    media_info = file.get("mediaInfo") or {}
    streams: list[dict] = []
    width, height = _resolution_dims(media_info, file.get("quality") or {})
    video_codec = media_info.get("videoCodec")
    if width or height or video_codec:
        streams.append({"Type": "Video", "Width": width, "Height": height, "Codec": video_codec or ""})
    audio_codec = media_info.get("audioCodec")
    if audio_codec:
        streams.append({"Type": "Audio", "Codec": audio_codec, "Channels": _channels(media_info.get("audioChannels"))})
    return streams


def radarr_movie_item(movie: dict) -> Optional[dict]:
    """A Jellyfin-shaped movie item for a Radarr movie with a file, else None."""
    if not movie.get("hasFile") or not movie.get("id"):
        return None
    file = movie.get("movieFile") or {}
    provider_ids: Dict[str, str] = {}
    if movie.get("tmdbId"):
        provider_ids["Tmdb"] = str(movie["tmdbId"])
    if movie.get("imdbId"):
        provider_ids["Imdb"] = str(movie["imdbId"])
    if not provider_ids:
        return None
    return {
        "Id": f"radarr:{movie['id']}",
        "Name": movie.get("title"),
        "ProductionYear": movie.get("year"),
        "ProviderIds": provider_ids,
        "DateCreated": file.get("dateAdded"),
        "Path": file.get("path"),
        "MediaStreams": file_media_streams(file),
    }


def sonarr_series_source_id(series: dict) -> str:
    return f"sonarr-series:{series['id']}"


def sonarr_episode_items(series: dict, episodes: list[dict], files: list[dict]) -> list[dict]:
    """Jellyfin-shaped episode items for every episode of a series with a file.

    One file can hold several episodes (a double episode); each gets its own
    item sharing the file's source id, just like an expanded Jellyfin
    multi-episode item."""
    files_by_id = {f.get("id"): f for f in files if f.get("id")}
    items: list[dict] = []
    for ep in episodes:
        if not ep.get("hasFile"):
            continue
        season, number = ep.get("seasonNumber"), ep.get("episodeNumber")
        if season is None or number is None:
            continue
        file = files_by_id.get(ep.get("episodeFileId")) or {}
        provider_ids: Dict[str, str] = {}
        if ep.get("tvdbId"):
            provider_ids["Tvdb"] = str(ep["tvdbId"])
        file_id = ep.get("episodeFileId") or f"ep{ep.get('id')}"
        items.append({
            "Id": f"sonarr:{file_id}",
            "SeriesId": sonarr_series_source_id(series),
            "SeriesName": series.get("title"),
            "Name": ep.get("title"),
            "ParentIndexNumber": season,
            "IndexNumber": number,
            "ProviderIds": provider_ids,
            "DateCreated": file.get("dateAdded"),
            "Path": file.get("path"),
            "MediaStreams": file_media_streams(file),
        })
    return items


async def get_radarr_items(url: str, token: str) -> List[dict]:
    """Every Radarr movie with a file on disk. Raises ArrError on failure."""
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
        movies = await _get_json(client, url, token, "/api/v3/movie")
    if not isinstance(movies, list):
        raise ArrError("Radarr /api/v3/movie returned an unexpected response")
    return [item for item in (radarr_movie_item(m) for m in movies) if item]


async def get_sonarr_library(url: str, token: str) -> tuple[List[dict], List[dict]]:
    """(series with at least one file, their episode items). Raises ArrError
    on failure - a partial read would make the sync prune real files."""
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
        series_list = await _get_json(client, url, token, "/api/v3/series")
        if not isinstance(series_list, list):
            raise ArrError("Sonarr /api/v3/series returned an unexpected response")
        with_files = [
            s for s in series_list
            if s.get("id") and ((s.get("statistics") or {}).get("episodeFileCount") or 0) > 0
        ]
        semaphore = asyncio.Semaphore(SONARR_CONCURRENCY)

        async def episodes_for(series: dict) -> list[dict]:
            async with semaphore:
                params = {"seriesId": series["id"]}
                episodes = await _get_json(client, url, token, "/api/v3/episode", params)
                files = await _get_json(client, url, token, "/api/v3/episodefile", params)
            if not isinstance(episodes, list) or not isinstance(files, list):
                raise ArrError(f"Sonarr returned an unexpected response for series {series['id']}")
            return sonarr_episode_items(series, episodes, files)

        results = await asyncio.gather(*[episodes_for(s) for s in with_files])
    items = [item for chunk in results for item in chunk]
    return with_files, items
