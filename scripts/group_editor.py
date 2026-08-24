#!/usr/bin/env python3
"""Small PySide6 editor for manually curated catalog groups."""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
import uuid
import xml.etree.ElementTree as ET
from contextlib import closing
from pathlib import Path
from typing import Any

from catalog_db import ensure_group_schema, export_static_catalog


def generate_group_id() -> str:
    """Generate an opaque permanent ID that users never need to type."""
    return f"group-{uuid.uuid4().hex[:12]}"


def store_group_icon(source_path: Path, website_root: Path, group_id: str) -> Path:
    """Validate and atomically copy a group icon into website assets."""
    source_path = Path(source_path)
    website_root = Path(website_root)
    suffix = source_path.suffix.lower()
    if suffix not in {".png", ".svg", ".ico"}:
        raise ValueError("Choose a PNG, SVG, or ICO image file.")
    data = source_path.read_bytes()
    if not data:
        raise ValueError("The selected icon file is empty.")
    if suffix == ".png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("The selected file is not a valid PNG image.")
    if suffix == ".ico" and not data.startswith(b"\x00\x00\x01\x00"):
        raise ValueError("The selected file is not a valid ICO image.")
    if suffix == ".svg":
        try:
            root = ET.fromstring(data)
        except ET.ParseError as error:
            raise ValueError("The selected file is not valid SVG XML.") from error
        if root.tag.rsplit("}", 1)[-1].lower() != "svg":
            raise ValueError("The selected file is not a valid SVG image.")
        if any(element.tag.rsplit("}", 1)[-1].lower() == "script" for element in root.iter()):
            raise ValueError("SVG icons cannot contain scripts.")

    safe_group_id = "".join(
        character
        for character in group_id
        if character.isascii() and (character.isalnum() or character in "-_")
    ) or "group"
    digest = hashlib.sha256(data).hexdigest()[:12]
    relative_path = Path("assets") / "group-icons" / f"{safe_group_id}-{digest}{suffix}"
    destination = website_root / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return relative_path


class GroupRepository:
    """Transactional access to group definitions and video assignments."""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        if not self.database_path.is_file():
            raise FileNotFoundError(f"Catalog database not found: {self.database_path}")
        ensure_group_schema(self.database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _validated_group_values(group_id: str, short_name: str, icon: str) -> tuple[str, str, str]:
        values = tuple(value.strip() for value in (group_id, short_name, icon))
        if not all(values):
            raise ValueError("Group ID, short name, and icon are required.")
        return values

    def list_playlists(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT id, title, sort_order FROM playlists "
                "ORDER BY sort_order, id"
            )
            return [dict(row) for row in rows]

    def move_playlist(self, playlist_id: str, direction: int) -> bool:
        """Move one playlist by one position using the latest committed order."""
        if direction not in {-1, 1}:
            raise ValueError("Playlist direction must be -1 or 1.")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                playlist_ids = [
                    row[0]
                    for row in connection.execute(
                        "SELECT id FROM playlists ORDER BY sort_order, id"
                    )
                ]
                try:
                    current_index = playlist_ids.index(playlist_id)
                except ValueError as error:
                    raise ValueError(f"Unknown playlist: {playlist_id}") from error
                target_index = current_index + direction
                if target_index < 0 or target_index >= len(playlist_ids):
                    connection.rollback()
                    return False
                playlist_ids[current_index], playlist_ids[target_index] = (
                    playlist_ids[target_index],
                    playlist_ids[current_index],
                )
                connection.executemany(
                    "UPDATE playlists SET sort_order = ? WHERE id = ?",
                    (
                        (sort_order, ordered_id)
                        for sort_order, ordered_id in enumerate(playlist_ids, start=1)
                    ),
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def list_groups(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT groups.id, groups.short_name, groups.icon, groups.revision, "
                "COUNT(video_groups.video_id) AS video_count "
                "FROM groups LEFT JOIN video_groups ON video_groups.group_id = groups.id "
                "GROUP BY groups.id, groups.short_name, groups.icon, groups.revision "
                "ORDER BY groups.short_name COLLATE NOCASE, groups.id"
            )
            return [dict(row) for row in rows]

    def create_group(self, group_id: str, short_name: str, icon: str) -> None:
        group_id, short_name, icon = self._validated_group_values(group_id, short_name, icon)
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "INSERT INTO groups(id, short_name, icon, revision) VALUES (?, ?, ?, ?)",
                    (group_id, short_name, icon, uuid.uuid4().hex),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("A group with that ID or short name already exists.") from error

    def update_group(
        self,
        group_id: str,
        short_name: str,
        icon: str,
        *,
        expected_short_name: str,
        expected_icon: str,
        expected_revision: str,
    ) -> None:
        group_id, short_name, icon = self._validated_group_values(group_id, short_name, icon)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "UPDATE groups SET short_name = ?, icon = ?, revision = ? "
                    "WHERE id = ? AND short_name = ? AND icon = ? AND revision = ?",
                    (
                        short_name,
                        icon,
                        uuid.uuid4().hex,
                        group_id,
                        expected_short_name,
                        expected_icon,
                        expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    if connection.execute(
                        "SELECT 1 FROM groups WHERE id = ?", (group_id,)
                    ).fetchone() is None:
                        raise ValueError(f"Unknown group: {group_id}")
                    raise ValueError(
                        "This group changed by another editor; select it again to reload."
                    )
                connection.commit()
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise ValueError("A group with that short name already exists.") from error
            except Exception:
                connection.rollback()
                raise

    def delete_group(self, group_id: str, *, expected_revision: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "DELETE FROM groups WHERE id = ? AND revision = ?",
                    (group_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    if connection.execute(
                        "SELECT 1 FROM groups WHERE id = ?", (group_id,)
                    ).fetchone() is None:
                        raise ValueError(f"Unknown group: {group_id}")
                    raise ValueError(
                        "This group changed by another editor; select it again to reload."
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def list_videos(self, group_id: str, search: str = "") -> list[dict[str, Any]]:
        pattern = f"%{search.strip()}%"
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT videos.id, videos.title, videos.published_at, "
                "CASE WHEN video_groups.video_id IS NULL THEN 0 ELSE 1 END AS assigned "
                "FROM videos LEFT JOIN video_groups "
                "ON video_groups.video_id = videos.id AND video_groups.group_id = ? "
                "WHERE videos.id LIKE ? COLLATE NOCASE OR videos.title LIKE ? COLLATE NOCASE "
                "ORDER BY COALESCE(videos.published_at, videos.upload_date, '') DESC, videos.id DESC",
                (group_id, pattern, pattern),
            )
            return [
                {**dict(row), "assigned": bool(row["assigned"])}
                for row in rows
            ]

    def set_video_assignment(self, group_id: str, video_id: str, assigned: bool) -> int:
        """Change one membership without replacing other editors' assignments."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if connection.execute(
                    "SELECT 1 FROM groups WHERE id = ?", (group_id,)
                ).fetchone() is None:
                    raise ValueError(f"Unknown group: {group_id}")
                if connection.execute(
                    "SELECT 1 FROM videos WHERE id = ?", (video_id,)
                ).fetchone() is None:
                    raise ValueError(f"Unknown video: {video_id}")
                if assigned:
                    connection.execute(
                        "INSERT OR IGNORE INTO video_groups(video_id, group_id) VALUES (?, ?)",
                        (video_id, group_id),
                    )
                else:
                    connection.execute(
                        "DELETE FROM video_groups WHERE video_id = ? AND group_id = ?",
                        (video_id, group_id),
                    )
                video_count = connection.execute(
                    "SELECT COUNT(*) FROM video_groups WHERE group_id = ?", (group_id,)
                ).fetchone()[0]
                connection.commit()
                return video_count
            except Exception:
                connection.rollback()
                raise


def _parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=root / "data" / "youtube.sqlite3")
    parser.add_argument("--output", type=Path, default=root / "data" / "youtube.json")
    parser.add_argument("--thumbnails", type=Path, default=root / "assets" / "thumbnails")
    return parser


def run_editor(database: Path, output: Path, thumbnails: Path) -> int:
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QIcon, QPixmap
        from PySide6.QtWidgets import (
            QApplication,
            QFileDialog,
            QFormLayout,
            QHBoxLayout,
            QHeaderView,
            QLabel,
            QLineEdit,
            QListWidget,
            QListWidgetItem,
            QMainWindow,
            QMessageBox,
            QPushButton,
            QSplitter,
            QTableWidget,
            QTableWidgetItem,
            QVBoxLayout,
            QWidget,
        )
    except ImportError:
        print(
            "PySide6 is required. Install it with: python -m pip install PySide6",
            file=sys.stderr,
        )
        return 2

    try:
        repository = GroupRepository(database)
    except Exception as error:  # noqa: BLE001 - report startup failures cleanly.
        print(error, file=sys.stderr)
        return 2
    website_root = Path(output).parent.parent

    class GroupEditorWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.selected_group_id: str | None = None
            self.loaded_short_name = ""
            self.loaded_icon = ""
            self.loaded_revision = ""
            self.pending_icon_source: Path | None = None
            self.loading_videos = False
            self.setWindowTitle("AFA Video Group Editor")
            self.resize(1050, 680)

            self.group_list = QListWidget()
            self.group_list.currentItemChanged.connect(self._group_selected)
            self.name_edit = QLineEdit()
            self.name_edit.setLayoutDirection(Qt.RightToLeft)
            self.name_edit.setAlignment(Qt.AlignRight)
            self.icon_preview = QLabel("No icon")
            self.icon_preview.setAlignment(Qt.AlignCenter)
            self.icon_preview.setFixedSize(72, 72)
            self.icon_preview.setStyleSheet("border: 1px solid #aaa;")
            self.icon_file_label = QLabel("No image selected")
            self.icon_file_label.setWordWrap(True)
            self.browse_icon_button = QPushButton("Browse…")
            self.browse_icon_button.clicked.connect(self._browse_icon)
            icon_controls = QWidget()
            icon_layout = QVBoxLayout(icon_controls)
            icon_layout.setContentsMargins(0, 0, 0, 0)
            icon_layout.addWidget(self.icon_preview, 0, Qt.AlignHCenter)
            icon_layout.addWidget(self.icon_file_label)
            icon_layout.addWidget(self.browse_icon_button)
            form = QFormLayout()
            form.addRow("Arabic name", self.name_edit)
            form.addRow("Icon image", icon_controls)

            self.new_button = QPushButton("New")
            self.new_button.clicked.connect(self._new_group)
            self.save_group_button = QPushButton("Add group")
            self.save_group_button.clicked.connect(self._save_group)
            self.delete_button = QPushButton("Delete")
            self.delete_button.clicked.connect(self._delete_group)
            group_buttons = QHBoxLayout()
            group_buttons.addWidget(self.new_button)
            group_buttons.addWidget(self.save_group_button)
            group_buttons.addWidget(self.delete_button)

            self.playlist_list = QListWidget()
            self.playlist_list.currentItemChanged.connect(
                lambda _current, _previous: self._update_playlist_buttons()
            )
            self.playlist_up_button = QPushButton("Move Up")
            self.playlist_up_button.clicked.connect(lambda: self._move_playlist(-1))
            self.playlist_down_button = QPushButton("Move Down")
            self.playlist_down_button.clicked.connect(lambda: self._move_playlist(1))
            playlist_buttons = QHBoxLayout()
            playlist_buttons.addWidget(self.playlist_up_button)
            playlist_buttons.addWidget(self.playlist_down_button)

            left = QWidget()
            left_layout = QVBoxLayout(left)
            left_layout.addWidget(QLabel("Playlist display order"))
            left_layout.addWidget(self.playlist_list, 1)
            left_layout.addLayout(playlist_buttons)
            left_layout.addWidget(QLabel("Groups"))
            left_layout.addWidget(self.group_list, 1)
            left_layout.addLayout(form)
            left_layout.addLayout(group_buttons)

            self.search_edit = QLineEdit()
            self.search_edit.setPlaceholderText("Filter by video title or ID…")
            self.search_edit.textChanged.connect(self._filter_videos)
            self.video_table = QTableWidget(0, 3)
            self.video_table.setHorizontalHeaderLabels(["Assigned", "Video title", "Video ID"])
            self.video_table.verticalHeader().setVisible(False)
            self.video_table.setAlternatingRowColors(True)
            self.video_table.itemChanged.connect(self._assignment_changed)
            header = self.video_table.horizontalHeader()
            header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.Stretch)
            header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
            self.assignment_status = QLabel("Assignment changes save immediately")
            self.export_button = QPushButton("Export website JSON")
            self.export_button.clicked.connect(self._export)
            video_buttons = QHBoxLayout()
            video_buttons.addWidget(self.assignment_status)
            video_buttons.addStretch()
            video_buttons.addWidget(self.export_button)

            right = QWidget()
            right_layout = QVBoxLayout(right)
            right_layout.addWidget(QLabel("Videos in selected group"))
            right_layout.addWidget(self.search_edit)
            right_layout.addWidget(self.video_table, 1)
            right_layout.addLayout(video_buttons)

            splitter = QSplitter()
            splitter.addWidget(left)
            splitter.addWidget(right)
            splitter.setSizes([320, 730])
            self.setCentralWidget(splitter)
            self.statusBar().showMessage(f"Database: {database}")
            self._reload_playlists()
            self._reload_groups()
            self._update_enabled_state()

        def _show_error(self, error: Exception) -> None:
            QMessageBox.critical(self, "Group editor", str(error))

        def _reload_playlists(self, select_id: str | None = None) -> None:
            self.playlist_list.clear()
            selected_item = None
            for playlist in repository.list_playlists():
                item = QListWidgetItem(playlist["title"])
                item.setData(Qt.UserRole, playlist["id"])
                self.playlist_list.addItem(item)
                if playlist["id"] == select_id:
                    selected_item = item
            if selected_item is not None:
                self.playlist_list.setCurrentItem(selected_item)
            self._update_playlist_buttons()

        def _update_playlist_buttons(self) -> None:
            row = self.playlist_list.currentRow()
            self.playlist_up_button.setEnabled(row > 0)
            self.playlist_down_button.setEnabled(
                row >= 0 and row < self.playlist_list.count() - 1
            )

        def _move_playlist(self, direction: int) -> None:
            current = self.playlist_list.currentItem()
            if current is None:
                return
            playlist_id = current.data(Qt.UserRole)
            try:
                if repository.move_playlist(playlist_id, direction):
                    self._reload_playlists(playlist_id)
                    self.statusBar().showMessage(
                        "Playlist order saved; export website JSON to publish it.",
                        6000,
                    )
            except Exception as error:  # noqa: BLE001 - GUI action boundary.
                self._show_error(error)

        def _reload_groups(self, select_id: str | None = None) -> None:
            self.selected_group_id = None
            self.group_list.clear()
            selected_item = None
            for group in repository.list_groups():
                item = QListWidgetItem(
                    f"{group['short_name']}  ({group['video_count']})"
                )
                icon_path = website_root / group["icon"]
                if icon_path.is_file():
                    item.setIcon(QIcon(str(icon_path)))
                item.setData(Qt.UserRole, group)
                self.group_list.addItem(item)
                if group["id"] == select_id:
                    selected_item = item
            if selected_item is not None:
                self.group_list.setCurrentItem(selected_item)
            elif select_id is not None:
                self._new_group()
            else:
                self._update_enabled_state()

        def _new_group(self) -> None:
            self.group_list.clearSelection()
            self.group_list.setCurrentItem(None)
            self.selected_group_id = None
            self.name_edit.clear()
            self.pending_icon_source = None
            self.icon_file_label.setText("No image selected")
            self.icon_preview.setPixmap(QPixmap())
            self.icon_preview.setText("No icon")
            self.video_table.setRowCount(0)
            self._update_enabled_state()
            self.name_edit.setFocus()

        def _group_selected(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
            if current is None:
                self._update_enabled_state()
                return
            group = current.data(Qt.UserRole)
            self.selected_group_id = group["id"]
            self.name_edit.setText(group["short_name"])
            self.loaded_short_name = group["short_name"]
            self.loaded_icon = group["icon"]
            self.loaded_revision = group["revision"]
            self.pending_icon_source = None
            self.icon_file_label.setText(group["icon"])
            self._show_icon_preview(website_root / group["icon"])
            self.save_group_button.setText("Save group")
            self._load_videos()
            self._update_enabled_state()

        def _update_enabled_state(self) -> None:
            selected = self.selected_group_id is not None
            self.delete_button.setEnabled(selected)
            self.search_edit.setEnabled(selected)
            self.video_table.setEnabled(selected)
            self.assignment_status.setEnabled(selected)
            if not selected:
                self.save_group_button.setText("Add group")

        def _show_icon_preview(self, path: Path) -> None:
            pixmap = QPixmap(str(path)) if path.is_file() else QPixmap()
            if pixmap.isNull():
                self.icon_preview.setPixmap(QPixmap())
                self.icon_preview.setText("No icon")
                return
            self.icon_preview.setText("")
            self.icon_preview.setPixmap(
                pixmap.scaled(
                    self.icon_preview.size(),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
            )

        def _browse_icon(self) -> None:
            filename, _selected_filter = QFileDialog.getOpenFileName(
                self,
                "Choose group icon",
                "",
                "Icon images (*.png *.svg *.ico)",
            )
            if not filename:
                return
            source = Path(filename)
            if source.suffix.lower() not in {".png", ".svg", ".ico"}:
                self._show_error(ValueError("Choose a PNG, SVG, or ICO image file."))
                return
            if QPixmap(str(source)).isNull():
                self._show_error(ValueError("The selected image cannot be opened."))
                return
            self.pending_icon_source = source
            self.icon_file_label.setText(source.name)
            self._show_icon_preview(source)

        def _save_group(self) -> None:
            try:
                if self.selected_group_id is None:
                    if self.pending_icon_source is None:
                        raise ValueError("Choose a PNG, SVG, or ICO icon image first.")
                    group_id = generate_group_id()
                    icon = store_group_icon(
                        self.pending_icon_source, website_root, group_id
                    ).as_posix()
                    repository.create_group(group_id, self.name_edit.text(), icon)
                    self._reload_groups(group_id)
                    self.statusBar().showMessage("Added group", 5000)
                else:
                    icon = self.loaded_icon
                    if self.pending_icon_source is not None:
                        icon = store_group_icon(
                            self.pending_icon_source,
                            website_root,
                            self.selected_group_id,
                        ).as_posix()
                    elif not (website_root / icon).is_file():
                        raise ValueError("Choose a PNG, SVG, or ICO icon image first.")
                    repository.update_group(
                        self.selected_group_id,
                        self.name_edit.text(),
                        icon,
                        expected_short_name=self.loaded_short_name,
                        expected_icon=self.loaded_icon,
                        expected_revision=self.loaded_revision,
                    )
                    group_id = self.selected_group_id
                    self._reload_groups(group_id)
                    self.statusBar().showMessage(f"Saved group {group_id}", 5000)
            except Exception as error:  # noqa: BLE001 - GUI action boundary.
                self._show_error(error)

        def _delete_group(self) -> None:
            if self.selected_group_id is None:
                return
            answer = QMessageBox.question(
                self,
                "Delete group",
                f"Delete group '{self.selected_group_id}' and all its video assignments?",
            )
            if answer != QMessageBox.Yes:
                return
            try:
                deleted_id = self.selected_group_id
                repository.delete_group(
                    deleted_id, expected_revision=self.loaded_revision
                )
                self._new_group()
                self._reload_groups()
                self.statusBar().showMessage(f"Deleted group {deleted_id}", 5000)
            except Exception as error:  # noqa: BLE001 - GUI action boundary.
                self._show_error(error)

        def _load_videos(self) -> None:
            if self.selected_group_id is None:
                return
            videos = repository.list_videos(self.selected_group_id)
            self.loading_videos = True
            try:
                self.video_table.setRowCount(len(videos))
                for row, video in enumerate(videos):
                    assigned = QTableWidgetItem()
                    assigned.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                    assigned.setCheckState(Qt.Checked if video["assigned"] else Qt.Unchecked)
                    title = QTableWidgetItem(video["title"])
                    title.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    video_id = QTableWidgetItem(video["id"])
                    video_id.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    self.video_table.setItem(row, 0, assigned)
                    self.video_table.setItem(row, 1, title)
                    self.video_table.setItem(row, 2, video_id)
            finally:
                self.loading_videos = False
            self._filter_videos(self.search_edit.text())

        def _filter_videos(self, text: str) -> None:
            needle = text.strip().casefold()
            for row in range(self.video_table.rowCount()):
                title = self.video_table.item(row, 1).text().casefold()
                video_id = self.video_table.item(row, 2).text().casefold()
                self.video_table.setRowHidden(row, bool(needle) and needle not in title and needle not in video_id)

        def _assignment_changed(self, item: QTableWidgetItem) -> None:
            if self.loading_videos or item.column() != 0 or self.selected_group_id is None:
                return
            video_id = self.video_table.item(item.row(), 2).text()
            assigned = item.checkState() == Qt.Checked
            try:
                video_count = repository.set_video_assignment(
                    self.selected_group_id, video_id, assigned
                )
                current = self.group_list.currentItem()
                if current is not None:
                    group = dict(current.data(Qt.UserRole))
                    group["video_count"] = video_count
                    current.setData(Qt.UserRole, group)
                    current.setText(
                        f"{group['short_name']}  ({group['video_count']})"
                    )
                action = "Assigned" if assigned else "Unassigned"
                self.statusBar().showMessage(f"{action} {video_id}", 3000)
            except Exception as error:  # noqa: BLE001 - GUI action boundary.
                self.loading_videos = True
                item.setCheckState(Qt.Unchecked if assigned else Qt.Checked)
                self.loading_videos = False
                self._show_error(error)

        def _export(self) -> None:
            try:
                export_static_catalog(database, output, thumbnails)
                self.statusBar().showMessage(f"Exported {output}", 8000)
                QMessageBox.information(self, "Export complete", f"Exported:\n{output}")
            except Exception as error:  # noqa: BLE001 - GUI action boundary.
                self._show_error(error)

    app = QApplication.instance() or QApplication(sys.argv)
    window = GroupEditorWindow()
    window.show()
    return app.exec()


def main() -> int:
    args = _parser().parse_args()
    return run_editor(args.database, args.output, args.thumbnails)


if __name__ == "__main__":
    raise SystemExit(main())
