"""End-to-end Radarr/Sonarr collection sync on SQLite: the arr reads and the
TMDB key are patched, everything else (sync_items, stale pruning, job rows)
runs for real."""

import unittest
import unittest.mock
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from core import arr_collection
from routers import arr_sync


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


def _movie(arr_id: int, tmdb_id: int) -> dict:
    return arr_collection.radarr_movie_item({
        "id": arr_id, "title": f"Movie {tmdb_id}", "tmdbId": tmdb_id, "hasFile": True,
        "movieFile": {"path": f"/movies/{tmdb_id}.mkv", "mediaInfo": {"resolution": "1920x1080"}},
    })


class ArrCollectionSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import models  # noqa: F401 - registers every table on Base.metadata
        from models.base import Base

        self.engine = create_async_engine(
            "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.addAsyncCleanup(self.engine.dispose)
        # UserSettings' JSON server defaults don't round-trip on SQLite, so the
        # (url, token) lookup is patched instead; it's covered on its own below.
        self.creds = ("http://arr:7878", "key")

    async def _run(self, app: str, **reader) -> "SyncJob":
        from models.base import CollectionSource
        from models.sync import SyncJob, SyncStatus

        source = CollectionSource.radarr if app == "radarr" else CollectionSource.sonarr
        async with self.Session() as db:
            job = SyncJob(user_id=1, source=source, status=SyncStatus.pending)
            db.add(job)
            await db.commit()
            job_id = job.id

        runner = arr_sync.run_radarr_collection_sync if app == "radarr" else arr_sync.run_sonarr_collection_sync
        with patch.object(arr_sync, "async_sessionmaker", return_value=self.Session), \
             patch.object(arr_sync, "arr_credentials", AsyncMock(return_value=self.creds)), \
             patch("routers.sync._get_effective_tmdb_key", AsyncMock(return_value="key")), \
             patch.object(arr_sync, "pre_cache_all_collected_bg", lambda: _noop()), \
             patch.object(arr_collection, "get_radarr_items", reader.get("radarr", AsyncMock(return_value=[]))), \
             patch.object(arr_collection, "get_sonarr_library", reader.get("sonarr", AsyncMock(return_value=([], [])))):
            await runner(1, job_id)

        async with self.Session() as db:
            return await db.get(SyncJob, job_id)

    async def _collected(self, source) -> dict[int, "CollectionFile"]:
        from models.collection import Collection, CollectionFile
        from models.media import Media

        async with self.Session() as db:
            rows = (await db.execute(
                select(Media.tmdb_id, Media.season_number, Media.episode_number, CollectionFile)
                .join(Collection, Collection.media_id == Media.id)
                .join(CollectionFile, CollectionFile.collection_id == Collection.id)
                .where(Collection.user_id == 1, CollectionFile.source == source)
            )).all()
        return {(r[0], r[1], r[2]): r[3] for r in rows}

    async def _add_movies(self, *tmdb_ids: int) -> None:
        from models.media import Media, MediaType
        async with self.Session() as db:
            for tid in tmdb_ids:
                db.add(Media(tmdb_id=tid, media_type=MediaType.movie, title=f"Movie {tid}"))
            await db.commit()

    async def test_radarr_files_become_the_collection(self):
        from models.base import CollectionSource
        from models.sync import SyncStatus

        await self._add_movies(603, 604)
        job = await self._run("radarr", radarr=AsyncMock(return_value=[_movie(1, 603), _movie(2, 604)]))
        self.assertEqual(job.status, SyncStatus.completed, job.error_message)
        collected = await self._collected(CollectionSource.radarr)
        self.assertEqual(set(collected), {(603, None, None), (604, None, None)})
        f = collected[(603, None, None)]
        self.assertEqual((f.source_id, f.resolution, f.file_path), ("radarr:1", "1080p", "/movies/603.mkv"))
        self.assertIsNone(f.connection_id)

    async def test_a_file_gone_from_radarr_leaves_the_collection(self):
        from models.base import CollectionSource

        await self._add_movies(603, 604)
        await self._run("radarr", radarr=AsyncMock(return_value=[_movie(1, 603), _movie(2, 604)]))
        job = await self._run("radarr", radarr=AsyncMock(return_value=[_movie(1, 603)]))
        self.assertEqual(job.stats.get("removed"), 1)
        self.assertEqual(set(await self._collected(CollectionSource.radarr)), {(603, None, None)})

    async def test_a_failed_read_prunes_nothing(self):
        from models.base import CollectionSource
        from models.sync import SyncStatus

        await self._add_movies(603)
        await self._run("radarr", radarr=AsyncMock(return_value=[_movie(1, 603)]))
        job = await self._run("radarr", radarr=AsyncMock(side_effect=arr_collection.ArrError("down")))
        self.assertEqual(job.status, SyncStatus.failed)
        self.assertIn("down", job.error_message)
        self.assertEqual(set(await self._collected(CollectionSource.radarr)), {(603, None, None)})

    async def test_sonarr_episodes_with_files_are_collected(self):
        from models.base import CollectionSource, MediaType
        from models.media import Media
        from models.show import Show
        from models.sync import SyncStatus

        async with self.Session() as db:
            show = Show(tmdb_id=95396, title="Severance")
            db.add(show)
            await db.flush()
            for ep in (1, 2):
                db.add(Media(tmdb_id=9000 + ep, media_type=MediaType.episode, title=f"E{ep}",
                             show_id=show.id, season_number=1, episode_number=ep))
            await db.commit()

        series = {"id": 3, "title": "Severance", "tmdbId": 95396, "statistics": {"episodeFileCount": 1}}
        items = arr_collection.sonarr_episode_items(series, [
            {"id": 10, "seasonNumber": 1, "episodeNumber": 1, "hasFile": True, "episodeFileId": 100},
            {"id": 11, "seasonNumber": 1, "episodeNumber": 2, "hasFile": False},
        ], [{"id": 100, "path": "/tv/S01E01.mkv"}])
        job = await self._run("sonarr", sonarr=AsyncMock(return_value=([series], items)))
        self.assertEqual(job.status, SyncStatus.completed, job.error_message)
        self.assertEqual(set(await self._collected(CollectionSource.sonarr)), {(9001, 1, 1)})

    async def test_missing_credentials_fail_the_job(self):
        from models.sync import SyncStatus

        self.creds = None
        job = await self._run("radarr")
        self.assertEqual(job.status, SyncStatus.failed)
        self.assertIn("Radarr", job.error_message)


class ArrCredentialsTests(unittest.IsolatedAsyncioTestCase):
    async def _creds(self, user, global_):
        db = AsyncMock()
        result = unittest.mock.MagicMock()
        result.scalar_one_or_none.return_value = user
        db.execute = AsyncMock(return_value=result)
        with patch("routers.media._get_global_settings", AsyncMock(return_value=global_)):
            return await arr_sync.arr_credentials(db, 1, "sonarr")

    async def test_user_settings_win(self):
        user = unittest.mock.MagicMock(sonarr_url="http://mine", sonarr_token="a")
        glob = unittest.mock.MagicMock(sonarr_url="http://server", sonarr_token="b")
        self.assertEqual(await self._creds(user, glob), ("http://mine", "a"))

    async def test_falls_back_to_the_server_wide_config(self):
        user = unittest.mock.MagicMock(sonarr_url=None, sonarr_token=None)
        glob = unittest.mock.MagicMock(sonarr_url="http://server", sonarr_token="b")
        self.assertEqual(await self._creds(user, glob), ("http://server", "b"))

    async def test_none_when_nothing_is_set(self):
        self.assertIsNone(await self._creds(None, None))

async def _noop():
    return None


if __name__ == "__main__":
    unittest.main()
