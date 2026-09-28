import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from models.lists import List as ListModel, ListItem
from routers import lists


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    """JSONB is PostgreSQL-only and has no SQLite rendering of its own."""
    return "JSON"


class ParseTmdbListIdTests(unittest.TestCase):
    def test_bare_digits(self) -> None:
        self.assertEqual(lists._parse_tmdb_list_id("1"), 1)
        self.assertEqual(lists._parse_tmdb_list_id("  8290920  "), 8290920)

    def test_url_with_slug(self) -> None:
        self.assertEqual(
            lists._parse_tmdb_list_id("https://www.themoviedb.org/list/1-the-marvel-universe"), 1
        )

    def test_url_without_slug(self) -> None:
        self.assertEqual(lists._parse_tmdb_list_id("https://www.themoviedb.org/list/42"), 42)

    def test_bare_id_and_slug_without_domain(self) -> None:
        # What someone pastes if they copy just the end of the URL bar.
        self.assertEqual(lists._parse_tmdb_list_id("1-the-marvel-universe"), 1)
        self.assertEqual(lists._parse_tmdb_list_id("  42-some-list  "), 42)

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(lists._parse_tmdb_list_id("not a list at all"))
        self.assertIsNone(lists._parse_tmdb_list_id(""))
        self.assertIsNone(lists._parse_tmdb_list_id("the-marvel-universe"))


class ImportTmdbListTests(unittest.IsolatedAsyncioTestCase):
    """Real-SQLite regression coverage for #442: TMDB responses/media
    resolution are mocked, but the List/ListItem persistence (the find-or-
    create-by-tmdb_list_id lookup and the dedup-on-re-import logic) runs
    against a real DB, the same reasoning as FindLocalShowForTvdbTests -
    those are exactly the kind of WHERE-clause bugs a fake DB double can't
    catch."""

    async def asyncSetUp(self) -> None:
        import models  # noqa: F401 - registers every table on Base.metadata
        from models.base import Base

        self.engine = create_async_engine(
            "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.addAsyncCleanup(self.engine.dispose)

        from models.users import User
        async with self.Session() as db:
            db.add(User(id=1, email="a@test.local", username="a", api_key="key-a", password_hash="x"))
            await db.commit()

    @staticmethod
    def _page(name: str, results: list[dict], total_pages: int = 1) -> dict:
        return {"name": name, "description": "A test list", "total_pages": total_pages, "results": results}

    async def test_creates_list_and_resolves_items(self) -> None:
        from models.media import Media
        from models.base import MediaType

        async def fake_movie(db, tmdb_id, title, api_key):
            m = Media(tmdb_id=tmdb_id, media_type=MediaType.movie, title=title)
            db.add(m)
            await db.flush()
            return m

        async def fake_series(db, tmdb_id, title, api_key):
            m = Media(tmdb_id=tmdb_id, media_type=MediaType.series, title=title)
            db.add(m)
            await db.flush()
            return m

        page = self._page("The Marvel Universe", [
            {"id": 986056, "title": "Thunderbolts*", "media_type": "movie"},
            {"id": 1396, "name": "Breaking Bad", "media_type": "tv"},
            {"id": 999, "name": "A Person", "media_type": "person"},  # skipped - not movie/tv
        ])

        async with self.Session() as db:
            with patch("core.tmdb.get_list", AsyncMock(return_value=page)), \
                 patch("routers.trakt._get_or_create_movie_media", AsyncMock(side_effect=fake_movie)), \
                 patch("routers.trakt._get_or_create_series_media", AsyncMock(side_effect=fake_series)), \
                 patch("routers.media.get_user_tmdb_key", AsyncMock(return_value="fake-key")):
                result = await lists.import_tmdb_list(
                    lists.TmdbListImport(list_id_or_url="https://www.themoviedb.org/list/1-the-marvel-universe"),
                    db=db, current_user=SimpleNamespace(id=1),
                )

        self.assertEqual(result["name"], "The Marvel Universe")
        self.assertEqual(result["item_count"], 2)  # person entry skipped

        async with self.Session() as db:
            from sqlalchemy import select
            lst = (await db.execute(select(ListModel).where(ListModel.tmdb_list_id == 1))).scalar_one()
            self.assertEqual(lst.user_id, 1)
            items = (await db.execute(select(ListItem).where(ListItem.list_id == lst.id))).scalars().all()
            self.assertEqual(len(items), 2)

    async def test_reimport_is_idempotent_and_adds_only_new_items(self) -> None:
        from models.media import Media
        from models.base import MediaType

        async def fake_movie(db, tmdb_id, title, api_key):
            existing = (await db.execute(
                __import__("sqlalchemy").select(Media).where(Media.tmdb_id == tmdb_id, Media.media_type == MediaType.movie)
            )).scalars().first()
            if existing:
                return existing
            m = Media(tmdb_id=tmdb_id, media_type=MediaType.movie, title=title)
            db.add(m)
            await db.flush()
            return m

        first_page = self._page("List", [{"id": 1, "title": "Movie One", "media_type": "movie"}])
        second_page = self._page("List", [
            {"id": 1, "title": "Movie One", "media_type": "movie"},
            {"id": 2, "title": "Movie Two", "media_type": "movie"},
        ])

        async with self.Session() as db:
            with patch("core.tmdb.get_list", AsyncMock(return_value=first_page)), \
                 patch("routers.trakt._get_or_create_movie_media", AsyncMock(side_effect=fake_movie)), \
                 patch("routers.trakt._get_or_create_series_media", AsyncMock()), \
                 patch("routers.media.get_user_tmdb_key", AsyncMock(return_value="fake-key")):
                first = await lists.import_tmdb_list(
                    lists.TmdbListImport(list_id_or_url="1"), db=db, current_user=SimpleNamespace(id=1),
                )

        async with self.Session() as db:
            with patch("core.tmdb.get_list", AsyncMock(return_value=second_page)), \
                 patch("routers.trakt._get_or_create_movie_media", AsyncMock(side_effect=fake_movie)), \
                 patch("routers.trakt._get_or_create_series_media", AsyncMock()), \
                 patch("routers.media.get_user_tmdb_key", AsyncMock(return_value="fake-key")):
                second = await lists.import_tmdb_list(
                    lists.TmdbListImport(list_id_or_url="1"), db=db, current_user=SimpleNamespace(id=1),
                )

        self.assertEqual(first["id"], second["id"], "re-import must reuse the same local list")
        self.assertEqual(first["item_count"], 1)
        self.assertEqual(second["item_count"], 2)


class ClearAllListsTests(unittest.IsolatedAsyncioTestCase):
    async def test_deletes_every_list_owned_by_the_user(self) -> None:
        db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())

        response = await lists.clear_all_lists(db=db, current_user=SimpleNamespace(id=7))

        self.assertEqual(response["status"], "ok")
        db.execute.assert_awaited_once()
        stmt = db.execute.call_args.args[0]
        self.assertEqual(stmt.table.name, ListModel.__tablename__)
        db.commit.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
