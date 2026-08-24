import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from catalog_db import export_static_catalog, validate_refresh, write_catalog


class CatalogDatabaseTest(unittest.TestCase):
    def sample_catalog(self):
        return {
            "schemaVersion": 1,
            "generatedAt": "2026-08-23T00:00:00Z",
            "source": "test",
            "channel": {
                "id": "channel-1",
                "handle": "@test",
                "name": "Test Channel",
                "url": "https://youtube.com/@test",
                "description": "Channel description",
                "thumbnail": "https://example.test/channel.jpg",
            },
            "counts": {
                "playlists": 1,
                "videos": 1,
                "shorts": 0,
                "playlistEntries": 1,
                "unavailablePlaylistEntries": 1,
            },
            "playlists": [
                {
                    "id": "playlist-1",
                    "url": "https://youtube.com/playlist?list=playlist-1",
                    "title": "Playlist",
                    "description": "Playlist description",
                    "thumbnail": "https://example.test/playlist.jpg",
                    "videoCount": 1,
                    "sourceVideoCount": 2,
                    "order": 1,
                    "videoIds": ["video-1"],
                    "unavailableVideoIds": ["private-1"],
                }
            ],
            "videos": [
                {
                    "id": "video-1",
                    "url": "https://youtube.com/watch?v=video-1",
                    "embedUrl": "https://youtube-nocookie.com/embed/video-1",
                    "title": "Video",
                    "description": "Video description",
                    "publishedAt": "2026-08-22T00:00:00Z",
                    "uploadDate": "2026-08-22",
                    "durationSeconds": 90,
                    "durationText": "1:30",
                    "thumbnail": "https://example.test/video.webp",
                    "thumbnails": {
                        "default": "https://example.test/default.jpg",
                        "medium": "https://example.test/medium.jpg",
                        "high": "https://example.test/high.jpg",
                        "standard": "https://example.test/standard.jpg",
                        "maxres": "https://example.test/maxres.jpg",
                    },
                    "isShort": False,
                    "tags": ["AI", "Arabic"],
                    "categories": ["Education"],
                    "language": "ar",
                    "availability": "public",
                    "liveStatus": "not_live",
                    "ageLimit": 0,
                    "stats": {"views": 100, "likes": 10, "comments": 2},
                    "playlists": [{"id": "playlist-1", "position": 1}],
                }
            ],
            "missingVideoIds": ["private-1"],
        }

    def test_sqlite_is_master_and_static_export_uses_blob_thumbnail(self):
        image_bytes = b"RIFFfake-webp-image"

        def fetch_thumbnail(url):
            self.assertEqual(url, "https://example.test/video.webp")
            return image_bytes, "image/webp"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "catalog.sqlite3"
            output_json = root / "site" / "data" / "youtube.json"
            thumbnail_dir = root / "site" / "assets" / "thumbnails"

            write_catalog(self.sample_catalog(), database, fetch_thumbnail=fetch_thumbnail)

            with closing(sqlite3.connect(database)) as connection:
                connection.row_factory = sqlite3.Row
                video = connection.execute(
                    "SELECT thumbnail_blob, thumbnail_mime, thumbnail_size, thumbnail_sha256 "
                    "FROM videos WHERE id = 'video-1'"
                ).fetchone()
                self.assertEqual(video["thumbnail_blob"], image_bytes)
                self.assertEqual(video["thumbnail_mime"], "image/webp")
                self.assertEqual(video["thumbnail_size"], len(image_bytes))
                self.assertEqual(len(video["thumbnail_sha256"]), 64)
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM video_tags").fetchone()[0], 2
                )

            export_static_catalog(database, output_json, thumbnail_dir)
            exported = json.loads(output_json.read_text(encoding="utf-8"))
            exported_video = exported["videos"][0]
            self.assertTrue(exported_video["thumbnail"].startswith("assets/thumbnails/"))
            self.assertTrue(exported_video["thumbnail"].endswith("/video-1.webp"))
            self.assertNotIn("thumbnailBlob", exported_video)
            self.assertEqual(
                (root / "site" / exported_video["thumbnail"]).read_bytes(), image_bytes
            )
            self.assertEqual(exported["counts"]["videos"], 1)
            self.assertEqual(exported["playlists"][0]["videoIds"], ["video-1"])
            self.assertEqual(exported["missingVideoIds"], ["private-1"])

    def test_failed_thumbnail_refresh_preserves_existing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.sqlite3"
            write_catalog(
                self.sample_catalog(),
                database,
                fetch_thumbnail=lambda _url: (b"original-image", "image/jpeg"),
            )
            changed = self.sample_catalog()
            changed["videos"][0]["title"] = "Incomplete refresh"

            def fail_thumbnail(_url):
                raise OSError("network unavailable")

            with self.assertRaises(RuntimeError):
                write_catalog(changed, database, fetch_thumbnail=fail_thumbnail)

            with closing(sqlite3.connect(database)) as connection:
                title, image = connection.execute(
                    "SELECT title, thumbnail_blob FROM videos WHERE id = 'video-1'"
                ).fetchone()
            self.assertEqual(title, "Video")
            self.assertEqual(image, b"original-image")

    def test_thumbnail_download_falls_back_to_next_resolution(self):
        attempted = []

        def fetch_thumbnail(url):
            attempted.append(url)
            if url.endswith("video.webp"):
                raise OSError("best image unavailable")
            return b"fallback-image", "image/jpeg"

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.sqlite3"
            write_catalog(
                self.sample_catalog(), database, fetch_thumbnail=fetch_thumbnail
            )
            with closing(sqlite3.connect(database)) as connection:
                source_url, image = connection.execute(
                    "SELECT thumbnail_source_url, thumbnail_blob FROM videos"
                ).fetchone()

        self.assertEqual(
            attempted[:2],
            [
                "https://example.test/video.webp",
                "https://example.test/maxres.jpg",
            ],
        )
        self.assertEqual(source_url, "https://example.test/maxres.jpg")
        self.assertEqual(image, b"fallback-image")

    def test_duplicate_video_positions_in_one_playlist_are_preserved(self):
        catalog = self.sample_catalog()
        catalog["playlists"][0].update(
            videoCount=2,
            sourceVideoCount=2,
            videoIds=["video-1", "video-1"],
            unavailableVideoIds=[],
        )
        catalog["videos"][0]["playlists"] = [
            {"id": "playlist-1", "position": 1},
            {"id": "playlist-1", "position": 2},
        ]
        catalog["missingVideoIds"] = []

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "catalog.sqlite3"
            output = root / "site" / "data" / "youtube.json"
            write_catalog(
                catalog,
                database,
                fetch_thumbnail=lambda _url: (b"image", "image/jpeg"),
            )
            export_static_catalog(database, output, root / "site/assets/thumbnails")
            exported = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(
            exported["playlists"][0]["videoIds"], ["video-1", "video-1"]
        )
        self.assertEqual(
            exported["videos"][0]["playlists"],
            [
                {"id": "playlist-1", "position": 1},
                {"id": "playlist-1", "position": 2},
            ],
        )

    def test_missing_video_ids_not_in_a_playlist_survive_export(self):
        catalog = self.sample_catalog()
        catalog["missingVideoIds"].append("unlisted-upload-1")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "catalog.sqlite3"
            output = root / "site/data/youtube.json"
            write_catalog(
                catalog,
                database,
                fetch_thumbnail=lambda _url: (b"image", "image/jpeg"),
            )
            export_static_catalog(database, output, root / "site/assets/thumbnails")
            exported = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            exported["missingVideoIds"], ["private-1", "unlisted-upload-1"]
        )

    def test_manual_groups_are_exported_and_survive_catalog_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "catalog.sqlite3"
            output = root / "site/data/youtube.json"
            thumbnails = root / "site/assets/thumbnails"
            fetch_thumbnail = lambda _url: (b"image", "image/jpeg")

            write_catalog(
                self.sample_catalog(), database, fetch_thumbnail=fetch_thumbnail
            )
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.executemany(
                    "INSERT INTO groups(id, short_name, icon) VALUES (?, ?, ?)",
                    [
                        ("video-generation", "Video Generation", "🎬"),
                        ("open-source", "Open Source", "code.svg"),
                    ],
                )
                connection.executemany(
                    "INSERT INTO video_groups(video_id, group_id) VALUES (?, ?)",
                    [
                        ("video-1", "video-generation"),
                        ("video-1", "open-source"),
                    ],
                )

            refreshed = self.sample_catalog()
            refreshed["videos"][0]["title"] = "Refreshed title"
            write_catalog(refreshed, database, fetch_thumbnail=fetch_thumbnail)
            export_static_catalog(database, output, thumbnails)
            exported = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(
            exported["groups"],
            [
                {
                    "id": "open-source",
                    "shortName": "Open Source",
                    "icon": "code.svg",
                    "videoCount": 1,
                },
                {
                    "id": "video-generation",
                    "shortName": "Video Generation",
                    "icon": "🎬",
                    "videoCount": 1,
                },
            ],
        )
        self.assertEqual(
            exported["videos"][0]["groups"],
            ["open-source", "video-generation"],
        )
        self.assertEqual(exported["videos"][0]["title"], "Refreshed title")

    def test_manual_playlist_order_survives_refresh_and_export(self):
        catalog = self.sample_catalog()
        second_playlist = dict(catalog["playlists"][0])
        second_playlist.update(
            id="playlist-2",
            url="https://youtube.com/playlist?list=playlist-2",
            title="Second playlist",
            order=2,
            videoCount=0,
            sourceVideoCount=0,
            videoIds=[],
            unavailableVideoIds=[],
        )
        catalog["playlists"].append(second_playlist)
        catalog["counts"]["playlists"] = 2

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "catalog.sqlite3"
            output = root / "site/data/youtube.json"
            thumbnails = root / "site/assets/thumbnails"
            fetch_thumbnail = lambda _url: (b"image", "image/jpeg")
            write_catalog(catalog, database, fetch_thumbnail=fetch_thumbnail)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "UPDATE playlists SET sort_order = CASE id "
                    "WHEN 'playlist-2' THEN 1 WHEN 'playlist-1' THEN 2 END"
                )

            refreshed = self.sample_catalog()
            refreshed_second = dict(second_playlist)
            refreshed["playlists"] = [refreshed["playlists"][0], refreshed_second]
            refreshed["counts"]["playlists"] = 2
            write_catalog(refreshed, database, fetch_thumbnail=fetch_thumbnail)
            export_static_catalog(database, output, thumbnails)
            exported = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(
            [(playlist["id"], playlist["order"]) for playlist in exported["playlists"]],
            [("playlist-2", 1), ("playlist-1", 2)],
        )

    def test_manual_group_edit_waiting_on_refresh_is_not_lost(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.sqlite3"
            fetch_thumbnail = lambda _url: (b"image", "image/jpeg")
            write_catalog(
                self.sample_catalog(), database, fetch_thumbnail=fetch_thumbnail
            )
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "INSERT INTO groups(id, short_name, icon) VALUES (?, ?, ?)",
                    ("video-generation", "Before", "🎬"),
                )

            edit_attempted = threading.Event()
            editor_errors = []

            def edit_group():
                try:
                    with closing(
                        sqlite3.connect(database, timeout=10)
                    ) as connection, connection:
                        edit_attempted.set()
                        connection.execute(
                            "UPDATE groups SET short_name = ? WHERE id = ?",
                            ("Concurrent edit", "video-generation"),
                        )
                except Exception as error:
                    editor_errors.append(error)

            editor = None

            def refresh_locked_hook():
                nonlocal editor
                editor = threading.Thread(target=edit_group)
                editor.start()
                self.assertTrue(edit_attempted.wait(timeout=2))
                time.sleep(0.1)

            refreshed = self.sample_catalog()
            refreshed["videos"][0]["title"] = "Refreshed"
            write_catalog(
                refreshed,
                database,
                fetch_thumbnail=fetch_thumbnail,
                refresh_locked_hook=refresh_locked_hook,
            )
            editor.join(timeout=10)

            self.assertFalse(editor.is_alive())
            self.assertEqual(editor_errors, [])
            with closing(sqlite3.connect(database)) as connection:
                group_name = connection.execute(
                    "SELECT short_name FROM groups WHERE id = 'video-generation'"
                ).fetchone()[0]
                video_title = connection.execute(
                    "SELECT title FROM videos WHERE id = 'video-1'"
                ).fetchone()[0]

        self.assertEqual(group_name, "Concurrent edit")
        self.assertEqual(video_title, "Refreshed")

    def test_export_upgrades_a_database_created_before_groups_existed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "catalog.sqlite3"
            output = root / "site/data/youtube.json"
            write_catalog(
                self.sample_catalog(),
                database,
                fetch_thumbnail=lambda _url: (b"image", "image/jpeg"),
            )
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("DROP TABLE video_groups")
                connection.execute("DROP TABLE groups")

            export_static_catalog(
                database, output, root / "site/assets/thumbnails"
            )
            exported = json.loads(output.read_text(encoding="utf-8"))
            with closing(sqlite3.connect(database)) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }

        self.assertEqual(exported["groups"], [])
        self.assertEqual(exported["videos"][0]["groups"], [])
        self.assertTrue({"groups", "video_groups"}.issubset(tables))

    def test_refresh_rejects_partial_extraction_and_unapproved_removals(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.sqlite3"
            write_catalog(
                self.sample_catalog(),
                database,
                fetch_thumbnail=lambda _url: (b"image", "image/jpeg"),
            )
            with self.assertRaisesRegex(RuntimeError, "failed to extract"):
                validate_refresh(database, {"video-1", "video-2"}, {"video-1"})
            with self.assertRaisesRegex(RuntimeError, "would remove"):
                validate_refresh(database, set(), set())
            validate_refresh(database, set(), set(), allow_removals=True)

    def test_export_rejects_corrupt_blob_without_switching_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "catalog.sqlite3"
            output = root / "site/data/youtube.json"
            thumbnails = root / "site/assets/thumbnails"
            write_catalog(
                self.sample_catalog(),
                database,
                fetch_thumbnail=lambda _url: (b"valid-image", "image/jpeg"),
            )
            export_static_catalog(database, output, thumbnails)
            original_json = output.read_bytes()
            original_image = root / "site" / json.loads(
                original_json.decode("utf-8")
            )["videos"][0]["thumbnail"]
            original_image_bytes = original_image.read_bytes()
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "UPDATE videos SET thumbnail_blob = ? WHERE id = 'video-1'",
                    (b"tampered",),
                )

            with self.assertRaisesRegex(ValueError, "size|checksum"):
                export_static_catalog(database, output, thumbnails)

            self.assertEqual(output.read_bytes(), original_json)
            self.assertEqual(original_image.read_bytes(), original_image_bytes)

    def test_export_cleanup_preserves_unrelated_thumbnail_directory_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "catalog.sqlite3"
            output = root / "site/data/youtube.json"
            thumbnails = root / "site/assets/thumbnails"
            write_catalog(
                self.sample_catalog(),
                database,
                fetch_thumbnail=lambda _url: (b"image", "image/jpeg"),
            )
            export_static_catalog(database, output, thumbnails)
            unrelated_file = thumbnails / "do-not-delete.txt"
            unrelated_file.write_text("keep", encoding="utf-8")
            unrelated_directory = thumbnails / "generation-custom-assets"
            unrelated_directory.mkdir()
            (unrelated_directory / "icon.svg").write_text("keep", encoding="utf-8")

            export_static_catalog(database, output, thumbnails)

            self.assertEqual(unrelated_file.read_text(encoding="utf-8"), "keep")
            self.assertEqual(
                (unrelated_directory / "icon.svg").read_text(encoding="utf-8"),
                "keep",
            )

    def test_export_uses_safe_thumbnail_filename_for_untrusted_video_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "catalog.sqlite3"
            output = root / "site/data/youtube.json"
            thumbnails = root / "site/assets/thumbnails"
            escaped_stem = root / "escaped-thumbnail"
            catalog = self.sample_catalog()
            malicious_id = str(escaped_stem)
            catalog["videos"][0]["id"] = malicious_id
            catalog["playlists"][0]["videoIds"] = [malicious_id]

            write_catalog(
                catalog,
                database,
                fetch_thumbnail=lambda _url: (b"image", "image/jpeg"),
            )
            export_static_catalog(database, output, thumbnails)
            exported = json.loads(output.read_text(encoding="utf-8"))
            exported_image = root / "site" / exported["videos"][0]["thumbnail"]

            self.assertFalse(escaped_stem.with_suffix(".jpg").exists())
            self.assertTrue(exported_image.is_file())
            self.assertTrue(exported_image.resolve().is_relative_to(thumbnails.resolve()))

    def test_different_databases_serialize_exports_to_same_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_database = root / "first.sqlite3"
            second_database = root / "second.sqlite3"
            output = root / "site/data/youtube.json"
            thumbnails = root / "site/assets/thumbnails"
            first_catalog = self.sample_catalog()
            second_catalog = self.sample_catalog()
            first_catalog["videos"][0]["title"] = "First export"
            second_catalog["videos"][0]["title"] = "Second export"
            write_catalog(
                first_catalog,
                first_database,
                fetch_thumbnail=lambda _url: (b"first-image", "image/jpeg"),
            )
            write_catalog(
                second_catalog,
                second_database,
                fetch_thumbnail=lambda _url: (b"second-image", "image/jpeg"),
            )
            first_started = threading.Event()
            release_first = threading.Event()
            errors = []

            def first_hook():
                first_started.set()
                self.assertTrue(release_first.wait(5))

            def run_export(database, hook=None):
                try:
                    export_static_catalog(
                        database, output, thumbnails, snapshot_hook=hook
                    )
                except Exception as error:
                    errors.append(error)

            first_thread = threading.Thread(
                target=run_export, args=(first_database, first_hook)
            )
            first_thread.start()
            self.assertTrue(first_started.wait(5))
            second_thread = threading.Thread(
                target=run_export, args=(second_database,)
            )
            second_thread.start()
            time.sleep(0.1)
            self.assertTrue(second_thread.is_alive())
            release_first.set()
            first_thread.join(5)
            second_thread.join(5)

            self.assertEqual(errors, [])
            exported = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(exported["videos"][0]["title"], "Second export")
            self.assertEqual(
                (root / "site" / exported["videos"][0]["thumbnail"]).read_bytes(),
                b"second-image",
            )

    def test_same_json_with_different_thumbnail_directories_is_serialized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_database = root / "first.sqlite3"
            second_database = root / "second.sqlite3"
            output = root / "site/data/youtube.json"
            first_thumbnails = root / "site/assets/first-thumbnails"
            second_thumbnails = root / "site/assets/second-thumbnails"
            first_catalog = self.sample_catalog()
            second_catalog = self.sample_catalog()
            first_catalog["videos"][0]["title"] = "First export"
            second_catalog["videos"][0]["title"] = "Second export"
            write_catalog(
                first_catalog,
                first_database,
                fetch_thumbnail=lambda _url: (b"first-image", "image/jpeg"),
            )
            write_catalog(
                second_catalog,
                second_database,
                fetch_thumbnail=lambda _url: (b"second-image", "image/jpeg"),
            )
            first_started = threading.Event()
            release_first = threading.Event()
            errors = []

            def first_hook():
                first_started.set()
                self.assertTrue(release_first.wait(5))

            def run_export(database, thumbnails, hook=None):
                try:
                    export_static_catalog(
                        database, output, thumbnails, snapshot_hook=hook
                    )
                except Exception as error:
                    errors.append(error)

            first_thread = threading.Thread(
                target=run_export,
                args=(first_database, first_thumbnails, first_hook),
            )
            first_thread.start()
            self.assertTrue(first_started.wait(5))
            second_thread = threading.Thread(
                target=run_export,
                args=(second_database, second_thumbnails),
            )
            second_thread.start()
            time.sleep(0.1)
            self.assertTrue(second_thread.is_alive())
            release_first.set()
            first_thread.join(5)
            second_thread.join(5)

            self.assertEqual(errors, [])
            exported = json.loads(output.read_text(encoding="utf-8"))
            exported_image = root / "site" / exported["videos"][0]["thumbnail"]
            self.assertEqual(exported["videos"][0]["title"], "Second export")
            self.assertEqual(exported_image.read_bytes(), b"second-image")
            self.assertTrue(exported_image.resolve().is_relative_to(second_thumbnails.resolve()))

    def test_export_reads_one_database_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "catalog.sqlite3"
            output = root / "site/data/youtube.json"
            thumbnails = root / "site/assets/thumbnails"
            write_catalog(
                self.sample_catalog(),
                database,
                fetch_thumbnail=lambda _url: (b"image", "image/jpeg"),
            )
            snapshot_started = threading.Event()
            allow_export = threading.Event()
            errors = []

            def snapshot_hook():
                snapshot_started.set()
                self.assertTrue(allow_export.wait(5))

            def run_export():
                try:
                    export_static_catalog(
                        database, output, thumbnails, snapshot_hook=snapshot_hook
                    )
                except Exception as error:
                    errors.append(error)

            export_thread = threading.Thread(target=run_export)
            export_thread.start()
            self.assertTrue(snapshot_started.wait(5))

            update_finished = threading.Event()

            def update_database():
                with closing(sqlite3.connect(database, timeout=5)) as connection, connection:
                    connection.execute(
                        "UPDATE playlists SET title = 'New Playlist'"
                    )
                    connection.execute("UPDATE videos SET title = 'New Video'")
                update_finished.set()

            update_thread = threading.Thread(target=update_database)
            update_thread.start()
            time.sleep(0.1)
            self.assertFalse(update_finished.is_set())
            allow_export.set()
            export_thread.join(5)
            update_thread.join(5)
            self.assertEqual(errors, [])
            self.assertTrue(update_finished.is_set())
            exported = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(exported["playlists"][0]["title"], "Playlist")
            self.assertEqual(exported["videos"][0]["title"], "Video")

    def test_concurrent_exports_do_not_delete_active_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "catalog.sqlite3"
            output = root / "site/data/youtube.json"
            thumbnails = root / "site/assets/thumbnails"
            write_catalog(
                self.sample_catalog(),
                database,
                fetch_thumbnail=lambda _url: (b"image", "image/jpeg"),
            )
            first_write_started = threading.Event()
            release_first_write = threading.Event()
            write_lock = threading.Lock()
            first_write = True
            original_write_bytes = Path.write_bytes
            errors = []

            def blocking_write(path, data):
                nonlocal first_write
                with write_lock:
                    should_block = first_write
                    first_write = False
                if should_block:
                    first_write_started.set()
                    self.assertTrue(release_first_write.wait(5))
                return original_write_bytes(path, data)

            def run_export():
                try:
                    export_static_catalog(database, output, thumbnails)
                except Exception as error:
                    errors.append(error)

            with patch.object(Path, "write_bytes", blocking_write):
                first_thread = threading.Thread(target=run_export)
                first_thread.start()
                self.assertTrue(first_write_started.wait(5))
                second_thread = threading.Thread(target=run_export)
                second_thread.start()
                time.sleep(0.1)
                self.assertTrue(second_thread.is_alive())
                release_first_write.set()
                first_thread.join(5)
                second_thread.join(5)

            self.assertEqual(errors, [])
            exported = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue((root / "site" / exported["videos"][0]["thumbnail"]).is_file())
            generations = [path for path in thumbnails.iterdir() if path.is_dir()]
            self.assertEqual(len(generations), 1)


if __name__ == "__main__":
    unittest.main()
