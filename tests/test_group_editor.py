import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from catalog_db import write_catalog
from group_editor import GroupRepository, generate_group_id, store_group_icon

from tests import test_catalog_db


class GroupRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "catalog.sqlite3"
        catalog = test_catalog_db.CatalogDatabaseTest().sample_catalog()
        second_video = dict(catalog["videos"][0])
        second_video.update(
            id="video-2",
            url="https://youtube.com/watch?v=video-2",
            embedUrl="https://youtube-nocookie.com/embed/video-2",
            title="Second Video",
            thumbnail="https://example.test/video-2.jpg",
            thumbnails={},
            playlists=[],
        )
        catalog["videos"].append(second_video)
        catalog["counts"]["videos"] = 2
        write_catalog(
            catalog,
            self.database,
            fetch_thumbnail=lambda _url: (b"image", "image/jpeg"),
        )
        self.repository = GroupRepository(self.database)

    def tearDown(self):
        self.directory.cleanup()

    def test_group_crud_and_assignments(self):
        self.repository.create_group("generation", "Generation", "🎬")
        self.repository.set_video_assignment("generation", "video-1", True)
        self.repository.set_video_assignment("generation", "video-2", True)

        group = self.repository.list_groups()[0]
        self.assertEqual(group["id"], "generation")
        self.assertEqual(group["short_name"], "Generation")
        self.assertEqual(group["icon"], "🎬")
        self.assertEqual(group["video_count"], 2)
        self.assertTrue(group["revision"])
        self.assertEqual(
            [video["id"] for video in self.repository.list_videos("generation") if video["assigned"]],
            ["video-2", "video-1"],
        )

        self.repository.update_group(
            "generation",
            "AI Generation",
            "sparkles.svg",
            expected_short_name="Generation",
            expected_icon="🎬",
            expected_revision=group["revision"],
        )
        self.repository.set_video_assignment("generation", "video-1", False)
        self.assertEqual(self.repository.list_groups()[0]["short_name"], "AI Generation")
        self.assertEqual(
            [video["id"] for video in self.repository.list_videos("generation") if video["assigned"]],
            ["video-2"],
        )

        updated_group = self.repository.list_groups()[0]
        self.repository.delete_group(
            "generation", expected_revision=updated_group["revision"]
        )
        self.assertEqual(self.repository.list_groups(), [])
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM video_groups").fetchone()[0], 0)

    def test_video_search_matches_title_and_id(self):
        self.repository.create_group("group", "Group", "icon")
        self.assertEqual(
            [video["id"] for video in self.repository.list_videos("group", "second")],
            ["video-2"],
        )
        self.assertEqual(
            [video["id"] for video in self.repository.list_videos("group", "video-1")],
            ["video-1"],
        )

    def test_playlist_display_order_can_move_up_and_down(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            channel_id = connection.execute("SELECT id FROM channels").fetchone()[0]
            connection.execute(
                "INSERT INTO playlists "
                "(id, channel_id, url, title, description, thumbnail_url, "
                "sort_order, source_video_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "playlist-2",
                    channel_id,
                    "https://youtube.com/playlist?list=playlist-2",
                    "Second playlist",
                    "",
                    None,
                    2,
                    0,
                ),
            )

        self.assertEqual(
            [playlist["id"] for playlist in self.repository.list_playlists()],
            ["playlist-1", "playlist-2"],
        )
        self.assertTrue(self.repository.move_playlist("playlist-2", -1))
        self.assertEqual(
            [playlist["id"] for playlist in self.repository.list_playlists()],
            ["playlist-2", "playlist-1"],
        )
        self.assertFalse(self.repository.move_playlist("playlist-2", -1))
        self.assertTrue(self.repository.move_playlist("playlist-2", 1))
        self.assertEqual(
            [playlist["sort_order"] for playlist in self.repository.list_playlists()],
            [1, 2],
        )
        with self.assertRaisesRegex(ValueError, "direction"):
            self.repository.move_playlist("playlist-1", 0)

    def test_invalid_or_unknown_values_do_not_partially_change_assignments(self):
        self.repository.create_group("group", "Group", "icon")
        self.repository.set_video_assignment("group", "video-1", True)

        for args in [("", "Name", "icon"), ("id", "", "icon"), ("id", "Name", "")]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.repository.create_group(*args)

        with self.assertRaisesRegex(ValueError, "Unknown video"):
            self.repository.set_video_assignment("group", "missing", True)
        self.assertEqual(
            [video["id"] for video in self.repository.list_videos("group") if video["assigned"]],
            ["video-1"],
        )

        with self.assertRaisesRegex(ValueError, "already exists"):
            self.repository.create_group("group", "Another", "icon")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.repository.create_group("other", "Group", "icon")

    def test_two_editors_do_not_replace_each_others_assignments(self):
        self.repository.create_group("group", "Group", "icon")
        other_editor = GroupRepository(self.database)

        self.assertEqual(other_editor.set_video_assignment("group", "video-1", True), 1)
        self.assertEqual(self.repository.set_video_assignment("group", "video-2", True), 2)

        self.assertEqual(
            [video["id"] for video in self.repository.list_videos("group") if video["assigned"]],
            ["video-2", "video-1"],
        )

    def test_stale_group_metadata_save_is_rejected(self):
        self.repository.create_group("group", "Group", "icon")
        other_editor = GroupRepository(self.database)
        original = other_editor.list_groups()[0]

        self.repository.update_group(
            "group",
            "First editor",
            "icon",
            expected_short_name="Group",
            expected_icon="icon",
            expected_revision=original["revision"],
        )
        with self.assertRaisesRegex(ValueError, "changed by another editor"):
            other_editor.update_group(
                "group",
                "Group",
                "second-icon",
                expected_short_name="Group",
                expected_icon="icon",
                expected_revision=original["revision"],
            )

        group = self.repository.list_groups()[0]
        self.assertEqual(group["short_name"], "First editor")
        self.assertEqual(group["icon"], "icon")

    def test_stale_delete_cannot_remove_updated_or_recreated_group(self):
        self.repository.create_group("group", "Group", "icon")
        stale = self.repository.list_groups()[0]
        other_editor = GroupRepository(self.database)
        other_editor.update_group(
            "group",
            "Updated",
            "icon",
            expected_short_name="Group",
            expected_icon="icon",
            expected_revision=stale["revision"],
        )

        with self.assertRaisesRegex(ValueError, "changed by another editor"):
            self.repository.delete_group(
                "group", expected_revision=stale["revision"]
            )

        updated = self.repository.list_groups()[0]
        other_editor.delete_group("group", expected_revision=updated["revision"])
        other_editor.create_group("group", "Updated", "icon")
        with self.assertRaisesRegex(ValueError, "changed by another editor"):
            self.repository.delete_group(
                "group", expected_revision=updated["revision"]
            )

    def test_group_ids_are_generated_automatically(self):
        first = generate_group_id()
        second = generate_group_id()

        self.assertRegex(first, r"^group-[0-9a-f]{12}$")
        self.assertRegex(second, r"^group-[0-9a-f]{12}$")
        self.assertNotEqual(first, second)

    def test_icon_file_is_validated_and_copied_into_website_assets(self):
        root = Path(self.directory.name) / "site"
        source = Path(self.directory.name) / "arabic-icon.svg"
        source.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
            '<path d="M0 0h10v10H0z" fill="#fff"/></svg>',
            encoding="utf-8",
        )

        relative_path = store_group_icon(source, root, "group-123456789abc")
        stored = root / relative_path

        self.assertEqual(stored.read_bytes(), source.read_bytes())
        self.assertEqual(relative_path.suffix, ".svg")
        self.assertTrue(relative_path.as_posix().startswith("assets/group-icons/"))

        invalid = Path(self.directory.name) / "icon.txt"
        invalid.write_text("not an image", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "PNG, SVG, or ICO"):
            store_group_icon(invalid, root, "group-123456789abc")


if __name__ == "__main__":
    unittest.main()
