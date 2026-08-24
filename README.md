# Arabic Future Academy website

Static website data for the **Arabic Future Academy** YouTube channel.

## Run the website locally

The first website version is an Arabic RTL video library with playlist filtering, newest/oldest video sorting, ranked title/description search, and inline card playback. Date sorting applies while browsing; search results remain ordered by their word-match positions. Shorts and Shorts-only playlists are excluded from the interface and playback queues.

```bash
python -m http.server 8000
```

Then open `http://localhost:8000/`. The page reads `data/youtube.json`; regenerate that export after catalog or group changes.

Playlist display order is managed in the desktop editor. Select a playlist under **Playlist display order**, use **Move Up** or **Move Down**, then select **Export website JSON** to publish the new order. Manual playlist order survives later catalog refreshes; newly discovered playlists are appended after the existing order.

## YouTube catalog

The master catalog is [`data/youtube.sqlite3`](data/youtube.sqlite3). It contains normalized channel, playlist, video, tag, category, manually curated group, and playlist-membership tables. Each public video's best available thumbnail is stored inside the `videos` table as a binary BLOB, together with its MIME type, byte size, SHA-256 checksum, and original source URL.

For GitHub Pages, the updater exports browser-ready [`data/youtube.json`](data/youtube.json) and local images under `assets/thumbnails/`. Plain JavaScript can load the export with:

```js
const catalog = await fetch('./data/youtube.json').then(response => response.json());
```

The catalog contains:

- Channel metadata
- All public playlists and their ordered video IDs
- One canonical record per public video or Short
- Descriptions, dates, durations, thumbnails, tags, categories, statistics, and embed URLs
- Playlist membership and position for each video
- Private playlist slots recorded separately as unavailable IDs
- Actual thumbnail image bytes in SQLite and exported local image files for the website
- Manually curated groups and each video's group IDs for filtering and search

A video appearing in several playlists is stored once and references every playlist it belongs to.

## Curating video groups

Groups are editor-owned data. Each group has an automatically generated internal ID, an Arabic display name, and a PNG, SVG, or ICO image. A video can belong to any number of groups through the `video_groups` table.

### Group editor

Install PySide6 once, then launch the desktop editor:

```bash
python -m pip install -r requirements-editor.txt
python scripts/group_editor.py
```

The editor lets you add, rename, and delete groups; search videos by title or ID; and check every video assigned to the selected group. Enter the Arabic group name, click **Browse…** to choose a transparent PNG, SVG, or ICO image, and click **Add group**. The internal group ID is generated automatically and is never entered by the user. Assignment checkbox changes save immediately in individual database transactions, so switching groups cannot discard pending edits and separate editor instances do not replace each other's unrelated assignments. Click **Export website JSON** afterward to regenerate `data/youtube.json` and the thumbnail export.

The generated group ID remains unchanged when the Arabic name or icon is edited. This prevents existing assignments and website filters from breaking. Selected icons are copied into `assets/group-icons/`. By default the editor opens `data/youtube.sqlite3`. Use `--database`, `--output`, and `--thumbnails` to work with different paths.

You can edit both tables with a SQLite editor such as DB Browser for SQLite. For example:

```sql
INSERT INTO groups (id, short_name, icon)
VALUES ('video-generation', 'Video Generation', '🎬');

INSERT INTO video_groups (video_id, group_id)
VALUES ('VIDEO_ID_HERE', 'video-generation');
```

To remove an assignment without deleting the group:

```sql
DELETE FROM video_groups
WHERE video_id = 'VIDEO_ID_HERE' AND group_id = 'video-generation';
```

After editing, run `python scripts/export-site-data.py`. The JSON contains top-level group definitions in `groups` and an array of group IDs in every video's `groups` field. Automated YouTube refreshes preserve group definitions and assignments for videos that still exist.

## Refreshing the data

Python 3 is required. Either install `yt-dlp`, or install `uv` and the script will run `yt-dlp` through `uvx` automatically.

```bash
python scripts/update-youtube-data.py
```

The updater uses public YouTube data and does not require a YouTube API key. It builds and validates a temporary snapshot, applies the extracted tables to SQLite in one transaction while preserving manually curated groups, and then regenerates the static website files. View, like, and comment counts are snapshots from the time shown in `generatedAt`; rerun the updater to refresh them.

Refreshes fail safely if a public video cannot be extracted or if videos disappear from the channel tabs. After confirming an intentional deletion or privacy change, allow the removal explicitly with `python scripts/update-youtube-data.py --allow-removals`.

To regenerate only the GitHub Pages files from your existing local SQLite database, without contacting YouTube, run:

```bash
python scripts/export-site-data.py
```

The local `data/youtube.sqlite3` file is ignored by Git, while the generated JSON and `assets/thumbnails/` files are suitable for publishing on GitHub Pages.
