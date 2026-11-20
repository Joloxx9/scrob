"""Collection sync from Radarr and Sonarr.

What Radarr/Sonarr have on disk becomes the user's collection, through the
same sync_items() path a Jellyfin pull uses (see core/arr_collection.py).
Collection only: the arr apps know nothing about watched state or ratings.
A file that disappears from Radarr/Sonarr is removed from the collection on
the next full sync, guarded by the same bad-scan check as media servers.
"""

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import arr_collection, tmdb
from core.image_cache import pre_cache_all_collected_bg
from db import engine, get_db
from dependencies import get_current_user
from models.base import CollectionSource, MediaType
from models.sync import SyncJob, SyncStatus
from models.users import User, UserSettings

logger = logging.getLogger(__name__)
router = APIRouter()


async def arr_credentials(db: AsyncSession, user_id: int, app: str) -> tuple[str, str] | None:
    """(url, token) for "radarr"/"sonarr": the user's own, else the server-wide
    one. Unlike adding titles, reading the library needs no root folder or
    quality profile."""
    from routers.media import _get_global_settings

    user_settings = (
        await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    ).scalar_one_or_none()
    for s in (user_settings, await _get_global_settings(db)):
        url, token = getattr(s, f"{app}_url", None), getattr(s, f"{app}_token", None)
        if s and url and token:
            return url, token
    return None


async def _fail(db: AsyncSession, job_id: int, message: str) -> None:
    await db.rollback()
    await db.execute(
        update(SyncJob).where(SyncJob.id == job_id).values(
            status=SyncStatus.failed, error_message=message[:900], updated_at=func.now()
        )
    )
    await db.commit()


async def _resolve_series_tmdb_ids(series_list: list[dict], api_key: str | None) -> dict[str, int]:
    """source series id -> TMDB id. Sonarr v4 reports tmdbId; older ones only
    tvdbId, which TMDB's /find resolves."""
    from routers.sync import TMDB_CONCURRENCY

    result: dict[str, int] = {}
    semaphore = asyncio.Semaphore(TMDB_CONCURRENCY)

    async def resolve(series: dict) -> None:
        sid = arr_collection.sonarr_series_source_id(series)
        if series.get("tmdbId"):
            result[sid] = int(series["tmdbId"])
            return
        if not series.get("tvdbId"):
            return
        async with semaphore:
            try:
                found = await tmdb.find_by_external_id(str(series["tvdbId"]), "tvdb_id", api_key=api_key)
            except Exception as e:  # one unmatched show must not fail the sync
                logger.warning("Sonarr sync: could not resolve TVDB %s: %s", series["tvdbId"], e)
                return
        if found.get("tv_results"):
            result[sid] = int(found["tv_results"][0]["id"])

    await asyncio.gather(*[resolve(s) for s in series_list])
    return result


async def run_radarr_collection_sync(user_id: int, job_id: int) -> None:
    from routers.sync import _sync_semaphore

    async with _sync_semaphore:  # one sync at a time, like every media-server pull
        await _run_arr_sync(user_id, job_id, "radarr")


async def run_sonarr_collection_sync(user_id: int, job_id: int) -> None:
    from routers.sync import _sync_semaphore

    async with _sync_semaphore:
        await _run_arr_sync(user_id, job_id, "sonarr")


async def _run_arr_sync(user_id: int, job_id: int, app: str) -> None:
    from routers.sync import (
        SyncCancelled,
        _get_effective_tmdb_key,
        _mark_job_running_unless_cancelled,
        _remove_stale_collection_files,
        _stamp_matched_show_warnings,
        sync_items,
        sync_shows_batch,
    )

    source = CollectionSource.radarr if app == "radarr" else CollectionSource.sonarr
    label = "Radarr" if app == "radarr" else "Sonarr"
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as db:
        stats = {"movies": 0, "episodes": 0, "skipped": 0, "errors": 0}
        try:
            if not await _mark_job_running_unless_cancelled(db, job_id, processed_items=0, total_items=0):
                return
            creds = await arr_credentials(db, user_id, app)
            settings = (
                await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
            ).scalar_one_or_none()
            tmdb_api_key = await _get_effective_tmdb_key(db, settings)
            if not creds or not tmdb_api_key:
                await _fail(db, job_id, f"Missing {label} URL/API key or TMDB API key")
                return
            url, token = creds

            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(current_step=f"Reading {label}"))
            await db.commit()

            warnings: list[dict] = []
            seen: set[str] = set()
            new_collected: set[int] = set()
            common = dict(
                api_key=tmdb_api_key, sync_collection=True, sync_watched=False, sync_ratings=False,
                new_collected_ids=new_collected, seen_source_ids=seen,
            )

            if app == "radarr":
                items = await arr_collection.get_radarr_items(url, token)
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(
                    total_items=len(items), current_step="Pulling movies"))
                await db.commit()
                warnings += await sync_items(items, MediaType.movie, source, db, stats, user_id, job_id, **common)
            else:
                series_list, items = await arr_collection.get_sonarr_library(url, token)
                series_tmdb_map = await _resolve_series_tmdb_ids(series_list, tmdb_api_key)
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(
                    total_items=len(items), current_step="Pulling episodes"))
                await db.commit()
                show_map, show_id_to_tmdb = await sync_shows_batch(series_tmdb_map, db, api_key=tmdb_api_key)
                for s in series_list:
                    if arr_collection.sonarr_series_source_id(s) not in show_map:
                        warnings.append({
                            "title": s.get("title"),
                            "media_type": "series",
                            "source_id": arr_collection.sonarr_series_source_id(s),
                            "reason": "Unmatched on source — no TMDB ID available for the series",
                        })
                matched = [i for i in items if i["SeriesId"] in show_map]
                # Episodes of an unmatched show are still marked seen: they
                # exist in Sonarr, so they must not be pruned as deleted.
                seen.update(i["Id"] for i in items if i["SeriesId"] not in show_map)
                warnings += await sync_items(
                    matched, MediaType.episode, source, db, stats, user_id, job_id, show_map,
                    show_id_to_tmdb=show_id_to_tmdb, **common,
                )

            removed = await _remove_stale_collection_files(db, user_id, source, None, seen)
            if removed:
                stats["removed"] = len(removed)
            await db.commit()

            warnings = await _stamp_matched_show_warnings(db, user_id, warnings)
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(
                status=SyncStatus.completed, stats=stats, warnings=warnings or None, updated_at=func.now()))
            await db.commit()
            print(f"{label} collection sync job {job_id} completed. Stats: {stats}")
            asyncio.create_task(pre_cache_all_collected_bg())
        except SyncCancelled:
            await db.rollback()
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(
                status=SyncStatus.cancelled, stats=stats, updated_at=func.now()))
            await db.commit()
        except arr_collection.ArrError as e:
            print(f"{label} collection sync job {job_id} failed: {e}")
            await _fail(db, job_id, f"{label}: {e}")
        except Exception as e:
            import traceback
            traceback.print_exc()
            await _fail(db, job_id, str(e))


async def _start(app: str, background_tasks: BackgroundTasks, db: AsyncSession, user: User) -> dict:
    source = CollectionSource.radarr if app == "radarr" else CollectionSource.sonarr
    label = "Radarr" if app == "radarr" else "Sonarr"
    if not await arr_credentials(db, user.id, app):
        raise HTTPException(status_code=400, detail=f"Configure {label} URL and API key first")
    active = await db.execute(
        select(SyncJob.id).where(
            SyncJob.user_id == user.id,
            SyncJob.source == source,
            SyncJob.status.in_([SyncStatus.pending, SyncStatus.running]),
        ).limit(1)
    )
    if active.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"A {label} collection sync is already running")
    job = SyncJob(user_id=user.id, source=source, status=SyncStatus.pending)
    db.add(job)
    await db.commit()
    await db.refresh(job)
    runner = run_radarr_collection_sync if app == "radarr" else run_sonarr_collection_sync
    background_tasks.add_task(runner, user.id, job.id)
    return {"status": "started", "job_id": job.id, "message": f"{label} collection sync is running in the background"}


@router.post("/radarr/sync")
async def sync_radarr_collection(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await _start("radarr", background_tasks, db, current_user)


@router.post("/sonarr/sync")
async def sync_sonarr_collection(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await _start("sonarr", background_tasks, db, current_user)
