#!/usr/bin/env python3
"""SQLite storage and static-site export for the YouTube catalog."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import sqlite3
import tempfile
import urllib.request
import uuid
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

ThumbnailFetcher = Callable[[str], tuple[bytes, str]]
MAX_THUMBNAIL_BYTES = 10 * 1024 * 1024
CATALOG_SCHEMA_VERSION = 3

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE catalog_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE channels (
    id TEXT PRIMARY KEY,
    handle TEXT,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    thumbnail_url TEXT
);

CREATE TABLE playlists (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    thumbnail_url TEXT,
    sort_order INTEGER NOT NULL CHECK (sort_order > 0),
    source_video_count INTEGER NOT NULL CHECK (source_video_count >= 0)
);

CREATE TABLE videos (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    embed_url TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    upload_date TEXT,
    duration_seconds REAL,
    duration_text TEXT,
    is_short INTEGER NOT NULL CHECK (is_short IN (0, 1)),
    language TEXT,
    availability TEXT,
    live_status TEXT,
    age_limit INTEGER,
    view_count INTEGER,
    like_count INTEGER,
    comment_count INTEGER,
    thumbnail_source_url TEXT NOT NULL,
    thumbnail_blob BLOB NOT NULL,
    thumbnail_mime TEXT NOT NULL,
    thumbnail_size INTEGER NOT NULL CHECK (thumbnail_size > 0),
    thumbnail_sha256 TEXT NOT NULL CHECK (length(thumbnail_sha256) = 64)
);

CREATE TABLE playlist_videos (
    playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position > 0),
    PRIMARY KEY (playlist_id, position)
);

CREATE TABLE missing_videos (
    video_id TEXT PRIMARY KEY
);

CREATE TABLE unavailable_playlist_videos (
    playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL REFERENCES missing_videos(video_id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position > 0),
    PRIMARY KEY (playlist_id, position)
);

CREATE TABLE video_tags (
    video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    sort_order INTEGER NOT NULL CHECK (sort_order >= 0),
    PRIMARY KEY (video_id, tag)
);

CREATE TABLE video_categories (
    video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    sort_order INTEGER NOT NULL CHECK (sort_order >= 0),
    PRIMARY KEY (video_id, category)
);

CREATE TABLE groups (
    id TEXT PRIMARY KEY CHECK (length(trim(id)) > 0),
    short_name TEXT NOT NULL UNIQUE CHECK (length(trim(short_name)) > 0),
    icon TEXT NOT NULL CHECK (length(trim(icon)) > 0),
    revision TEXT NOT NULL DEFAULT 'legacy' CHECK (length(trim(revision)) > 0)
);

CREATE TABLE video_groups (
    video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    PRIMARY KEY (video_id, group_id)
);

CREATE INDEX idx_videos_published_at ON videos(published_at DESC);
CREATE INDEX idx_playlist_videos_video ON playlist_videos(video_id);
CREATE INDEX idx_video_groups_group ON video_groups(group_id, video_id);
"""


def download_thumbnail(url: str) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_THUMBNAIL_BYTES:
            raise ValueError(f"Thumbnail exceeds {MAX_THUMBNAIL_BYTES} bytes: {url}")
        data = response.read(MAX_THUMBNAIL_BYTES + 1)
        mime = response.headers.get_content_type()
    if not data or len(data) > MAX_THUMBNAIL_BYTES:
        raise ValueError(f"Invalid thumbnail size for {url}")
    if not mime.startswith("image/"):
        raise ValueError(f"Expected an image from {url}, received {mime}")
    return data, mime


def _thumbnail_candidates(video: dict[str, Any]) -> list[str]:
    candidates = [video.get("thumbnail")]
    thumbnails = video.get("thumbnails") or {}
    candidates.extend(
        thumbnails.get(name)
        for name in ("maxres", "standard", "high", "medium", "default")
    )
    return list(dict.fromkeys(url for url in candidates if url))


def _fetch_best_thumbnail(
    video: dict[str, Any], fetch_thumbnail: ThumbnailFetcher
) -> tuple[str, bytes, str]:
    errors: list[str] = []
    for url in _thumbnail_candidates(video):
        try:
            data, mime = fetch_thumbnail(url)
            if not data or not mime.startswith("image/"):
                raise ValueError("thumbnail fetcher returned invalid image data")
            return url, data, mime.lower().split(";", 1)[0]
        except Exception as error:  # Try the next known YouTube resolution.
            errors.append(f"{url}: {error}")
    raise RuntimeError(
        f"Unable to download a thumbnail for video {video.get('id')}: "
        + "; ".join(errors)
    )


def validate_refresh(
    database_path: Path,
    discovered_public_ids: set[str],
    extracted_ids: set[str],
    *,
    allow_removals: bool = False,
) -> None:
    """Fail closed when extraction is partial or public records disappear."""
    failed_ids = sorted(discovered_public_ids - extracted_ids)
    if failed_ids:
        raise RuntimeError(
            "yt-dlp failed to extract public videos; the existing database was not changed: "
            + ", ".join(failed_ids)
        )

    database_path = Path(database_path)
    if not database_path.exists() or allow_removals:
        return
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            existing_ids = {
                row[0] for row in connection.execute("SELECT id FROM videos")
            }
    except sqlite3.DatabaseError as error:
        raise RuntimeError(f"Cannot validate the existing database: {error}") from error
    removed_ids = sorted(existing_ids - discovered_public_ids)
    if removed_ids:
        raise RuntimeError(
            "Refresh would remove existing public videos; rerun with --allow-removals "
            "after confirming they were intentionally removed or made private: "
            + ", ".join(removed_ids)
        )


def ensure_group_schema(database_path: Path) -> None:
    """Upgrade an existing catalog so groups can be edited manually."""
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS groups (
                id TEXT PRIMARY KEY CHECK (length(trim(id)) > 0),
                short_name TEXT NOT NULL UNIQUE CHECK (length(trim(short_name)) > 0),
                icon TEXT NOT NULL CHECK (length(trim(icon)) > 0),
                revision TEXT NOT NULL DEFAULT 'legacy' CHECK (length(trim(revision)) > 0)
            );
            CREATE TABLE IF NOT EXISTS video_groups (
                video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                PRIMARY KEY (video_id, group_id)
            );
            CREATE INDEX IF NOT EXISTS idx_video_groups_group
                ON video_groups(group_id, video_id);
            """
        )
        group_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(groups)")
        }
        if "revision" not in group_columns:
            connection.execute(
                "ALTER TABLE groups ADD COLUMN revision TEXT NOT NULL DEFAULT 'legacy'"
            )
        connection.execute(
            "UPDATE groups SET revision = lower(hex(randomblob(16))) "
            "WHERE revision = 'legacy' OR length(trim(revision)) = 0"
        )
        connection.execute(
            "UPDATE catalog_meta SET value = ? "
            "WHERE key = 'schema_version' AND CAST(value AS INTEGER) < ?",
            (str(CATALOG_SCHEMA_VERSION), CATALOG_SCHEMA_VERSION),
        )


def _merge_refreshed_catalog(
    database_path: Path,
    refreshed_path: Path,
    refresh_locked_hook: Callable[[], None] | None,
) -> None:
    """Replace extracted data in-place while serializing manual group edits."""
    delete_order = (
        "unavailable_playlist_videos",
        "playlist_videos",
        "video_tags",
        "video_categories",
        "video_groups",
        "videos",
        "playlists",
        "missing_videos",
        "channels",
        "catalog_meta",
    )
    insert_order = (
        "catalog_meta",
        "missing_videos",
        "channels",
        "playlists",
        "videos",
        "playlist_videos",
        "unavailable_playlist_videos",
        "video_tags",
        "video_categories",
    )

    with closing(sqlite3.connect(database_path, timeout=30)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("ATTACH DATABASE ? AS refreshed", (str(refreshed_path),))
        try:
            connection.execute("BEGIN IMMEDIATE")
            if refresh_locked_hook is not None:
                refresh_locked_hook()
            manual_memberships = list(
                connection.execute(
                    "SELECT video_id, group_id FROM video_groups "
                    "ORDER BY video_id, group_id"
                )
            )
            manual_playlist_order = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM playlists ORDER BY sort_order, id"
                )
            ]
            for table in delete_order:
                connection.execute(f'DELETE FROM main."{table}"')
            for table in insert_order:
                connection.execute(
                    f'INSERT INTO main."{table}" SELECT * FROM refreshed."{table}"'
                )
            refreshed_playlist_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM playlists ORDER BY sort_order, id"
                )
            ]
            refreshed_playlist_id_set = set(refreshed_playlist_ids)
            ordered_playlist_ids = [
                playlist_id
                for playlist_id in manual_playlist_order
                if playlist_id in refreshed_playlist_id_set
            ]
            ordered_playlist_ids.extend(
                playlist_id
                for playlist_id in refreshed_playlist_ids
                if playlist_id not in set(ordered_playlist_ids)
            )
            connection.executemany(
                "UPDATE playlists SET sort_order = ? WHERE id = ?",
                (
                    (sort_order, playlist_id)
                    for sort_order, playlist_id in enumerate(
                        ordered_playlist_ids, start=1
                    )
                ),
            )
            refreshed_video_ids = {
                row[0] for row in connection.execute("SELECT id FROM videos")
            }
            connection.executemany(
                "INSERT INTO video_groups(video_id, group_id) VALUES (?, ?)",
                (
                    (video_id, group_id)
                    for video_id, group_id in manual_memberships
                    if video_id in refreshed_video_ids
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("DETACH DATABASE refreshed")


def write_catalog(
    catalog: dict[str, Any],
    database_path: Path,
    *,
    fetch_thumbnail: ThumbnailFetcher = download_thumbnail,
    refresh_locked_hook: Callable[[], None] | None = None,
) -> None:
    """Atomically refresh extracted data while preserving manual group data."""
    database_path = Path(database_path)
    database_existed = database_path.exists()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{database_path.name}.", suffix=".tmp", dir=database_path.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)

    try:
        with closing(sqlite3.connect(temporary_path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(SCHEMA)
            meta = {
                "schema_version": str(
                    max(CATALOG_SCHEMA_VERSION, int(catalog["schemaVersion"]))
                ),
                "generated_at": catalog["generatedAt"],
                "source": catalog["source"],
            }
            connection.executemany(
                "INSERT INTO catalog_meta(key, value) VALUES (?, ?)", meta.items()
            )

            missing_ids = set(catalog.get("missingVideoIds") or [])
            missing_ids.update(
                video_id
                for playlist in catalog["playlists"]
                for video_id in playlist.get("unavailableVideoIds") or []
            )
            connection.executemany(
                "INSERT INTO missing_videos(video_id) VALUES (?)",
                ((video_id,) for video_id in sorted(missing_ids)),
            )

            channel = catalog["channel"]
            connection.execute(
                "INSERT INTO channels "
                "(id, handle, name, url, description, thumbnail_url) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    channel["id"],
                    channel.get("handle"),
                    channel["name"],
                    channel["url"],
                    channel.get("description") or "",
                    channel.get("thumbnail"),
                ),
            )

            for playlist in catalog["playlists"]:
                connection.execute(
                    "INSERT INTO playlists "
                    "(id, channel_id, url, title, description, thumbnail_url, "
                    "sort_order, source_video_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        playlist["id"],
                        channel["id"],
                        playlist["url"],
                        playlist["title"],
                        playlist.get("description") or "",
                        playlist.get("thumbnail"),
                        playlist["order"],
                        playlist.get("sourceVideoCount", playlist["videoCount"]),
                    ),
                )

            videos_by_id = {video["id"]: video for video in catalog["videos"]}
            for video in catalog["videos"]:
                thumbnail_url, thumbnail_blob, thumbnail_mime = _fetch_best_thumbnail(
                    video, fetch_thumbnail
                )
                stats = video.get("stats") or {}
                connection.execute(
                    "INSERT INTO videos "
                    "(id, channel_id, url, embed_url, title, description, published_at, "
                    "upload_date, duration_seconds, duration_text, is_short, language, "
                    "availability, live_status, age_limit, view_count, like_count, "
                    "comment_count, thumbnail_source_url, thumbnail_blob, thumbnail_mime, "
                    "thumbnail_size, thumbnail_sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        video["id"], channel["id"], video["url"], video["embedUrl"],
                        video["title"], video.get("description") or "",
                        video.get("publishedAt"), video.get("uploadDate"),
                        video.get("durationSeconds"), video.get("durationText"),
                        int(bool(video.get("isShort"))), video.get("language"),
                        video.get("availability"), video.get("liveStatus"),
                        video.get("ageLimit"), stats.get("views"), stats.get("likes"),
                        stats.get("comments"), thumbnail_url, thumbnail_blob, thumbnail_mime,
                        len(thumbnail_blob), hashlib.sha256(thumbnail_blob).hexdigest(),
                    ),
                )
                connection.executemany(
                    "INSERT INTO video_tags(video_id, tag, sort_order) VALUES (?, ?, ?)",
                    ((video["id"], tag, order) for order, tag in enumerate(video.get("tags") or [])),
                )
                connection.executemany(
                    "INSERT INTO video_categories(video_id, category, sort_order) VALUES (?, ?, ?)",
                    ((video["id"], category, order) for order, category in enumerate(video.get("categories") or [])),
                )

            for playlist in catalog["playlists"]:
                available_entries = sorted(
                    (membership["position"], video["id"])
                    for video in catalog["videos"]
                    for membership in video.get("playlists") or []
                    if membership["id"] == playlist["id"]
                )
                if [video_id for _, video_id in available_entries] != playlist["videoIds"]:
                    raise ValueError(
                        f"Playlist order/membership mismatch for {playlist['id']}"
                    )
                for position, video_id in available_entries:
                    if video_id not in videos_by_id:
                        raise ValueError(f"Playlist references unknown video {video_id}")
                    connection.execute(
                        "INSERT INTO playlist_videos(playlist_id, video_id, position) VALUES (?, ?, ?)",
                        (playlist["id"], video_id, position),
                    )
                unavailable_ids = playlist.get("unavailableVideoIds") or []
                available_position_values = {position for position, _ in available_entries}
                remaining_positions = iter(
                    position
                    for position in range(1, playlist.get("sourceVideoCount", 0) + 1)
                    if position not in available_position_values
                )
                for video_id in unavailable_ids:
                    connection.execute(
                        "INSERT INTO unavailable_playlist_videos "
                        "(playlist_id, video_id, position) VALUES (?, ?, ?)",
                        (playlist["id"], video_id, next(remaining_positions)),
                    )

            connection.execute("PRAGMA optimize")
        if database_existed:
            ensure_group_schema(database_path)
            _merge_refreshed_catalog(
                database_path, temporary_path, refresh_locked_hook
            )
            temporary_path.unlink()
        else:
            os.replace(temporary_path, database_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _extension_for_mime(mime: str) -> str:
    overrides = {"image/jpeg": ".jpg", "image/svg+xml": ".svg"}
    return overrides.get(mime) or mimetypes.guess_extension(mime) or ".img"


def _safe_video_filename_stem(video_id: str) -> str:
    """Keep normal YouTube IDs readable and hash unsafe database values."""
    windows_reserved_names = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
    if (
        video_id
        and len(video_id) <= 120
        and video_id.upper() not in windows_reserved_names
        and all(
        character.isascii() and (character.isalnum() or character in "-_")
        for character in video_id
        )
    ):
        return video_id
    return "video-" + hashlib.sha256(video_id.encode("utf-8")).hexdigest()


def _is_export_generation_directory(path: Path) -> bool:
    """Return whether a directory name is one created by this exporter."""
    if not path.is_dir():
        return False
    name = path.name
    if name.startswith("generation-"):
        token = name.removeprefix("generation-")
    elif name.startswith(".generation-") and name.endswith(".tmp"):
        token = name.removeprefix(".generation-").removesuffix(".tmp")
    else:
        return False
    return len(token) == 32 and all(character in "0123456789abcdef" for character in token)


def export_static_catalog(
    database_path: Path,
    output_json: Path,
    thumbnail_directory: Path,
    *,
    snapshot_hook: Callable[[], None] | None = None,
) -> None:
    """Export browser-friendly JSON and image files from the master database."""
    database_path = Path(database_path)
    output_json = Path(output_json)
    thumbnail_directory = Path(thumbnail_directory)
    ensure_group_schema(database_path)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    thumbnail_directory.mkdir(parents=True, exist_ok=True)

    generation_name = f"generation-{uuid.uuid4().hex}"
    generation_directory = thumbnail_directory / generation_name
    temporary_generation = thumbnail_directory / f".{generation_name}.tmp"
    temporary_json = output_json.with_name(
        f".{output_json.name}.{uuid.uuid4().hex}.tmp"
    )
    json_switched = False
    lock_connection: sqlite3.Connection | None = None
    destination_lock_connections: list[sqlite3.Connection] = []

    try:
        destination_lock_paths = {
            output_json.parent / f".{output_json.name}.export-lock.sqlite3",
            thumbnail_directory / ".catalog-export-lock.sqlite3",
        }
        for lock_path in sorted(destination_lock_paths, key=lambda path: str(path.resolve())):
            destination_lock_connection = sqlite3.connect(lock_path, timeout=30)
            destination_lock_connections.append(destination_lock_connection)
            destination_lock_connection.execute(
                "CREATE TABLE IF NOT EXISTS export_lock (id INTEGER PRIMARY KEY)"
            )
            destination_lock_connection.commit()
            destination_lock_connection.execute("BEGIN IMMEDIATE")
        lock_connection = sqlite3.connect(database_path, timeout=30)
        # Hold a reserved lock for the entire read, stage, publish, and cleanup
        # sequence. This creates one read snapshot and serializes exporters.
        lock_connection.execute("BEGIN IMMEDIATE")
        if snapshot_hook is not None:
            snapshot_hook()
        temporary_generation.mkdir()
        with closing(sqlite3.connect(database_path, timeout=30)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN")
            meta = dict(connection.execute("SELECT key, value FROM catalog_meta"))
            channel_row = connection.execute("SELECT * FROM channels LIMIT 1").fetchone()
            if channel_row is None:
                raise ValueError("The database has no channel")
            channel = {
                "id": channel_row["id"],
                "handle": channel_row["handle"],
                "name": channel_row["name"],
                "url": channel_row["url"],
                "description": channel_row["description"],
                "thumbnail": channel_row["thumbnail_url"],
            }

            playlists = []
            for row in connection.execute("SELECT * FROM playlists ORDER BY sort_order"):
                video_ids = [
                    item[0]
                    for item in connection.execute(
                        "SELECT video_id FROM playlist_videos "
                        "WHERE playlist_id = ? ORDER BY position",
                        (row["id"],),
                    )
                ]
                unavailable_ids = [
                    item[0]
                    for item in connection.execute(
                        "SELECT video_id FROM unavailable_playlist_videos "
                        "WHERE playlist_id = ? ORDER BY position",
                        (row["id"],),
                    )
                ]
                playlists.append(
                    {
                        "id": row["id"],
                        "url": row["url"],
                        "title": row["title"],
                        "description": row["description"],
                        "thumbnail": row["thumbnail_url"],
                        "videoCount": len(video_ids),
                        "sourceVideoCount": row["source_video_count"],
                        "order": row["sort_order"],
                        "videoIds": video_ids,
                        "unavailableVideoIds": unavailable_ids,
                    }
                )

            videos = []
            for row in connection.execute(
                "SELECT * FROM videos ORDER BY COALESCE(published_at, upload_date, '') DESC, id DESC"
            ):
                thumbnail_blob = row["thumbnail_blob"]
                if len(thumbnail_blob) != row["thumbnail_size"]:
                    raise ValueError(f"Thumbnail size mismatch for {row['id']}")
                if hashlib.sha256(thumbnail_blob).hexdigest() != row["thumbnail_sha256"]:
                    raise ValueError(f"Thumbnail checksum mismatch for {row['id']}")
                extension = _extension_for_mime(row["thumbnail_mime"])
                filename_stem = _safe_video_filename_stem(row["id"])
                staged_image = temporary_generation / f"{filename_stem}{extension}"
                final_image = generation_directory / staged_image.name
                staged_image.write_bytes(thumbnail_blob)
                website_root = output_json.parent.parent
                relative_image = Path(os.path.relpath(final_image, website_root)).as_posix()
                tags = [
                    item[0]
                    for item in connection.execute(
                        "SELECT tag FROM video_tags WHERE video_id = ? ORDER BY sort_order",
                        (row["id"],),
                    )
                ]
                categories = [
                    item[0]
                    for item in connection.execute(
                        "SELECT category FROM video_categories "
                        "WHERE video_id = ? ORDER BY sort_order",
                        (row["id"],),
                    )
                ]
                memberships = [
                    {"id": item[0], "position": item[1]}
                    for item in connection.execute(
                        "SELECT playlist_id, position FROM playlist_videos "
                        "WHERE video_id = ? ORDER BY playlist_id, position",
                        (row["id"],),
                    )
                ]
                group_ids = [
                    item[0]
                    for item in connection.execute(
                        "SELECT video_groups.group_id FROM video_groups "
                        "JOIN groups ON groups.id = video_groups.group_id "
                        "WHERE video_groups.video_id = ? "
                        "ORDER BY groups.short_name COLLATE NOCASE, groups.id",
                        (row["id"],),
                    )
                ]
                videos.append(
                    {
                        "id": row["id"],
                        "url": row["url"],
                        "embedUrl": row["embed_url"],
                        "title": row["title"],
                        "description": row["description"],
                        "publishedAt": row["published_at"],
                        "uploadDate": row["upload_date"],
                        "durationSeconds": row["duration_seconds"],
                        "durationText": row["duration_text"],
                        "thumbnail": relative_image,
                        "thumbnailMime": row["thumbnail_mime"],
                        "isShort": bool(row["is_short"]),
                        "tags": tags,
                        "categories": categories,
                        "language": row["language"],
                        "availability": row["availability"],
                        "liveStatus": row["live_status"],
                        "ageLimit": row["age_limit"],
                        "stats": {
                            "views": row["view_count"],
                            "likes": row["like_count"],
                            "comments": row["comment_count"],
                        },
                        "playlists": memberships,
                        "groups": group_ids,
                    }
                )

            groups = [
                {
                    "id": row["id"],
                    "shortName": row["short_name"],
                    "icon": row["icon"],
                    "videoCount": row["video_count"],
                }
                for row in connection.execute(
                    "SELECT groups.id, groups.short_name, groups.icon, "
                    "COUNT(video_groups.video_id) AS video_count "
                    "FROM groups LEFT JOIN video_groups "
                    "ON video_groups.group_id = groups.id "
                    "GROUP BY groups.id, groups.short_name, groups.icon "
                    "ORDER BY groups.short_name COLLATE NOCASE, groups.id"
                )
            ]

            missing_ids = [
                item[0]
                for item in connection.execute(
                    "SELECT video_id FROM missing_videos ORDER BY video_id"
                )
            ]
            counts = {
                "playlists": len(playlists),
                "videos": len(videos),
                "groups": len(groups),
                "shorts": sum(video["isShort"] for video in videos),
                "playlistEntries": sum(
                    playlist["videoCount"] for playlist in playlists
                ),
                "unavailablePlaylistEntries": sum(
                    len(playlist["unavailableVideoIds"]) for playlist in playlists
                ),
            }
            catalog = {
                "schemaVersion": int(meta["schema_version"]),
                "generatedAt": meta["generated_at"],
                "source": meta["source"],
                "channel": channel,
                "counts": counts,
                "playlists": playlists,
                "groups": groups,
                "videos": videos,
                "missingVideoIds": missing_ids,
            }

        temporary_json.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary_generation, generation_directory)
        os.replace(temporary_json, output_json)
        json_switched = True

        for stale_path in thumbnail_directory.iterdir():
            if stale_path == generation_directory:
                continue
            if _is_export_generation_directory(stale_path):
                shutil.rmtree(stale_path)
    except Exception:
        shutil.rmtree(temporary_generation, ignore_errors=True)
        temporary_json.unlink(missing_ok=True)
        if not json_switched:
            shutil.rmtree(generation_directory, ignore_errors=True)
        raise
    finally:
        if lock_connection is not None:
            lock_connection.rollback()
            lock_connection.close()
        for destination_lock_connection in reversed(destination_lock_connections):
            destination_lock_connection.rollback()
            destination_lock_connection.close()
