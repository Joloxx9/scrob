import unittest

from core import arr_collection
from core.jellyfin import extract_quality, get_jellyfin_tmdb_id, get_jellyfin_tvdb_id
from models.base import CollectionSource
from routers.sync import _MEDIA_BROWSER_ITEM_SOURCES, provider_added_at


class RadarrItemTests(unittest.TestCase):
    def test_movie_with_file_becomes_a_jellyfin_shaped_item(self) -> None:
        item = arr_collection.radarr_movie_item({
            "id": 7, "title": "Dune", "year": 2021, "tmdbId": 438631, "imdbId": "tt1160419", "hasFile": True,
            "movieFile": {
                "path": "/movies/Dune (2021)/Dune.mkv", "dateAdded": "2024-01-02T03:04:05Z",
                "quality": {"quality": {"resolution": 2160}},
                "mediaInfo": {"resolution": "3840x1600", "videoCodec": "x265", "audioCodec": "EAC3", "audioChannels": 5.1},
            },
        })
        self.assertEqual(item["Id"], "radarr:7")
        self.assertEqual(get_jellyfin_tmdb_id(item["ProviderIds"]), 438631)
        self.assertEqual(item["ProviderIds"]["Imdb"], "tt1160419")
        self.assertEqual(item["Path"], "/movies/Dune (2021)/Dune.mkv")
        quality = extract_quality(item["MediaStreams"])
        self.assertEqual(quality["resolution"], "4K")
        self.assertEqual(quality["video_codec"], "X265")
        self.assertEqual(quality["audio_codec"], "EAC3")
        self.assertEqual(quality["audio_channels"], "5.1")
        self.assertIsNotNone(provider_added_at(item, CollectionSource.radarr))

    def test_movie_without_a_file_is_not_collected(self) -> None:
        self.assertIsNone(arr_collection.radarr_movie_item({"id": 1, "tmdbId": 5, "hasFile": False}))

    def test_movie_without_any_id_is_skipped(self) -> None:
        self.assertIsNone(arr_collection.radarr_movie_item({"id": 1, "hasFile": True, "movieFile": {}}))

    def test_resolution_falls_back_to_the_quality_profile(self) -> None:
        streams = arr_collection.file_media_streams({"quality": {"quality": {"resolution": 1080}}, "mediaInfo": {}})
        self.assertEqual(extract_quality(streams)["resolution"], "1080p")

    def test_stereo_and_7_1_channel_counts(self) -> None:
        self.assertEqual(arr_collection._channels(2), 2)
        self.assertEqual(arr_collection._channels(7.1), 8)
        self.assertEqual(arr_collection._channels(None), 0)


class SonarrItemTests(unittest.TestCase):
    series = {"id": 3, "title": "Severance", "tvdbId": 371980, "tmdbId": 95396}

    def test_only_episodes_with_files_are_returned(self) -> None:
        episodes = [
            {"id": 10, "seasonNumber": 1, "episodeNumber": 1, "title": "Good News", "hasFile": True, "episodeFileId": 100, "tvdbId": 8001},
            {"id": 11, "seasonNumber": 1, "episodeNumber": 2, "title": "Half Loop", "hasFile": False, "episodeFileId": 0, "tvdbId": 8002},
        ]
        files = [{"id": 100, "path": "/tv/S01E01.mkv", "dateAdded": "2024-05-01T00:00:00Z", "mediaInfo": {"resolution": "1920x1080"}}]
        items = arr_collection.sonarr_episode_items(self.series, episodes, files)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["Id"], "sonarr:100")
        self.assertEqual(item["SeriesId"], "sonarr-series:3")
        self.assertEqual(item["SeriesName"], "Severance")
        self.assertEqual((item["ParentIndexNumber"], item["IndexNumber"]), (1, 1))
        self.assertEqual(get_jellyfin_tvdb_id(item["ProviderIds"]), 8001)
        self.assertEqual(item["Path"], "/tv/S01E01.mkv")
        self.assertEqual(extract_quality(item["MediaStreams"])["resolution"], "1080p")

    def test_double_episode_file_yields_one_item_per_episode(self) -> None:
        episodes = [
            {"id": 20, "seasonNumber": 2, "episodeNumber": 1, "hasFile": True, "episodeFileId": 200, "tvdbId": 9001},
            {"id": 21, "seasonNumber": 2, "episodeNumber": 2, "hasFile": True, "episodeFileId": 200, "tvdbId": 9002},
        ]
        items = arr_collection.sonarr_episode_items(self.series, episodes, [{"id": 200}])
        self.assertEqual([i["IndexNumber"] for i in items], [1, 2])
        self.assertEqual({i["Id"] for i in items}, {"sonarr:200"})
        self.assertEqual([i["ProviderIds"]["Tvdb"] for i in items], ["9001", "9002"])


class SourceWiringTests(unittest.TestCase):
    def test_arr_sources_take_the_jellyfin_item_path(self) -> None:
        self.assertIn(CollectionSource.radarr, _MEDIA_BROWSER_ITEM_SOURCES)
        self.assertIn(CollectionSource.sonarr, _MEDIA_BROWSER_ITEM_SOURCES)


if __name__ == "__main__":
    unittest.main()
