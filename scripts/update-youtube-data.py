#!/usr/bin/env python3
"""Build the SQLite master catalog from a public YouTube channel.

No YouTube API key is required. The script uses yt-dlp for extraction and writes
SQLite first, then exports browser-friendly JSON and local thumbnail files.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from catalog_db import export_static_catalog, validate_refresh, write_catalog

DEFAULT_CHANNEL = "https://www.youtube.com/@arabicfutureacademy"


def yt_dlp_command() -> list[str]:
    executable = shutil.which("yt-dlp")
    if executable:
        return [executable]
    uvx = shutil.which("uvx")
    if uvx:
        return [uvx, "--from", "yt-dlp", "yt-dlp"]
    raise SystemExit("Install yt-dlp, or install uv so the script can run yt-dlp with uvx.")


def run_json(url: str, *, flat: bool = False) -> dict[str, Any]:
    command = [*yt_dlp_command(), "--dump-single-json", "--no-warnings"]
    if flat:
        command.append("--flat-playlist")
    command.append(url)
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"yt-dlp failed for {url}")
    return json.loads(result.stdout)


def run_video_batch(video_ids: list[str]) -> dict[str, dict[str, Any]]:
    command = [
        *yt_dlp_command(),
        "--dump-json",
        "--skip-download",
        "--ignore-errors",
        "--no-warnings",
        "--no-playlist",
        *[f"https://www.youtube.com/watch?v={video_id}" for video_id in video_ids],
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    videos: dict[str, dict[str, Any]] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item.get("id"):
            videos[item["id"]] = item
    if not videos:
        raise RuntimeError(result.stderr.strip() or "No video metadata was extracted")
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)
    return videos


def iso_date(value: str | None) -> str | None:
    if not value or len(value) != 8:
        return None
    return f"{value[:4]}-{value[4:6]}-{value[6:]}"


def iso_datetime(timestamp: int | float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")


def duration_text(seconds: int | float | None) -> str | None:
    if seconds is None:
        return None
    total = round(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def best_thumbnail(item: dict[str, Any]) -> str | None:
    thumbnails = item.get("thumbnails") or []
    usable = [thumb for thumb in thumbnails if thumb.get("url")]
    if usable:
        return max(
            usable,
            key=lambda thumb: (thumb.get("width") or 0) * (thumb.get("height") or 0),
        )["url"]
    video_id = item.get("id")
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else None


def video_record(
    item: dict[str, Any],
    memberships: list[dict[str, Any]],
    shorts: set[str],
) -> dict[str, Any]:
    video_id = item["id"]
    return {
        "id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "embedUrl": f"https://www.youtube-nocookie.com/embed/{video_id}",
        "title": item.get("title"),
        "description": item.get("description") or "",
        "publishedAt": iso_datetime(item.get("timestamp") or item.get("release_timestamp")),
        "uploadDate": iso_date(item.get("upload_date")),
        "durationSeconds": item.get("duration"),
        "durationText": duration_text(item.get("duration")),
        "thumbnail": best_thumbnail(item),
        "thumbnails": {
            "default": f"https://i.ytimg.com/vi/{video_id}/default.jpg",
            "medium": f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
            "high": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "standard": f"https://i.ytimg.com/vi/{video_id}/sddefault.jpg",
            "maxres": f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
        },
        "isShort": video_id in shorts,
        "tags": item.get("tags") or [],
        "categories": item.get("categories") or [],
        "language": item.get("language"),
        "availability": item.get("availability"),
        "liveStatus": item.get("live_status"),
        "ageLimit": item.get("age_limit"),
        "stats": {
            "views": item.get("view_count"),
            "likes": item.get("like_count"),
            "comments": item.get("comment_count"),
        },
        "playlists": memberships,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", default=DEFAULT_CHANNEL)
    parser.add_argument(
        "--allow-removals",
        action="store_true",
        help="Allow videos in the existing database to disappear after confirming the removal.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "youtube.sqlite3",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "youtube.json",
    )
    parser.add_argument(
        "--thumbnails",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "assets" / "thumbnails",
    )
    args = parser.parse_args()
    channel_url = args.channel.rstrip("/")

    print("Discovering channel uploads and playlists...")
    playlist_tab = run_json(f"{channel_url}/playlists", flat=True)
    videos_tab = run_json(f"{channel_url}/videos", flat=True)
    try:
        shorts_tab = run_json(f"{channel_url}/shorts", flat=True)
    except RuntimeError:
        shorts_tab = {"entries": []}

    playlist_summaries = playlist_tab.get("entries") or []
    playlist_details: list[dict[str, Any]] = []
    memberships: dict[str, list[dict[str, Any]]] = defaultdict(list)
    discovered_ids: set[str] = set()

    for order, summary in enumerate(playlist_summaries):
        playlist_id = summary["id"]
        details = run_json(f"https://www.youtube.com/playlist?list={playlist_id}", flat=True)
        entries = details.get("entries") or []
        video_ids: list[str] = []
        for position, entry in enumerate(entries):
            video_id = entry.get("id")
            if not video_id:
                continue
            video_ids.append(video_id)
            discovered_ids.add(video_id)
            memberships[video_id].append({"id": playlist_id, "position": position + 1})
        playlist_details.append(
            {
                "id": playlist_id,
                "url": f"https://www.youtube.com/playlist?list={playlist_id}",
                "title": details.get("title") or summary.get("title"),
                "description": details.get("description") or "",
                "thumbnail": best_thumbnail(summary),
                "videoCount": len(video_ids),
                "order": order + 1,
                "videoIds": video_ids,
            }
        )

    upload_ids = [entry["id"] for entry in videos_tab.get("entries") or [] if entry.get("id")]
    short_ids = {entry["id"] for entry in shorts_tab.get("entries") or [] if entry.get("id")}
    discovered_ids.update(upload_ids)
    discovered_ids.update(short_ids)

    print(f"Fetching detailed metadata for {len(discovered_ids)} unique videos...")
    raw_videos = run_video_batch(sorted(discovered_ids))
    validate_refresh(
        args.database,
        set(upload_ids) | short_ids,
        set(raw_videos),
        allow_removals=args.allow_removals,
    )
    missing_ids = sorted(discovered_ids - raw_videos.keys())
    missing_id_set = set(missing_ids)
    for playlist in playlist_details:
        source_video_ids = playlist["videoIds"]
        playlist["unavailableVideoIds"] = [
            video_id for video_id in source_video_ids if video_id in missing_id_set
        ]
        playlist["videoIds"] = [
            video_id for video_id in source_video_ids if video_id not in missing_id_set
        ]
        playlist["sourceVideoCount"] = playlist["videoCount"]
        playlist["videoCount"] = len(playlist["videoIds"])
    records = [
        video_record(raw_videos[video_id], memberships.get(video_id, []), short_ids)
        for video_id in raw_videos
    ]
    records.sort(key=lambda video: (video.get("publishedAt") or video.get("uploadDate") or "", video["id"]), reverse=True)

    channel_id = videos_tab.get("channel_id") or playlist_tab.get("channel_id")
    data = {
        "schemaVersion": 2,
        "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": "YouTube public channel data extracted with yt-dlp",
        "channel": {
            "id": channel_id,
            "handle": videos_tab.get("uploader_id") or playlist_tab.get("uploader_id"),
            "name": videos_tab.get("channel") or videos_tab.get("uploader"),
            "url": channel_url,
            "description": videos_tab.get("description") or playlist_tab.get("description") or "",
            "thumbnail": best_thumbnail(videos_tab),
        },
        "counts": {
            "playlists": len(playlist_details),
            "videos": len(records),
            "shorts": sum(1 for video in records if video["isShort"]),
            "playlistEntries": sum(playlist["videoCount"] for playlist in playlist_details),
            "unavailablePlaylistEntries": sum(
                len(playlist["unavailableVideoIds"]) for playlist in playlist_details
            ),
        },
        "playlists": playlist_details,
        "videos": records,
        "missingVideoIds": missing_ids,
    }

    print(f"Writing SQLite master database with {len(records)} thumbnail images...")
    write_catalog(data, args.database)
    print(f"Wrote {args.database}")
    export_static_catalog(args.database, args.output, args.thumbnails)
    print(f"Exported {args.output} and {args.thumbnails}")
    print(json.dumps(data["counts"], ensure_ascii=False))
    if missing_ids:
        print(f"Warning: metadata unavailable for {len(missing_ids)} videos", file=sys.stderr)


if __name__ == "__main__":
    main()
