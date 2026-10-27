import os
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

from PySide6.QtCore import Qt, Signal, QUrl, QByteArray, QSize, QRectF
from PySide6.QtGui import QPixmap, QImage, QIcon, QDesktopServices, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QFrame,
    QLabel,
    QPushButton,
    QWidget,
    QScrollArea,
)

ABOUT_HEADERS = {"User-Agent": "CoomerDL-About"}


class AboutWindow(QDialog):
    _github_loaded = Signal(str, int, int, object)
    _contributors_loaded = Signal(list)
    _contributors_failed = Signal(str)

    def __init__(self, parent, translate, version):
        super().__init__(parent)
        self.parent = parent
        self.translate = translate
        self.version = version

        self.setWindowTitle(self.translate("ABOUT_WINDOW_TITLE"))
        self.setModal(True)
        self.setFixedSize(380, 560)

        if self.parent is not None:
            self.setWindowModality(Qt.WindowModal)

        self.downloads_label = None
        self.date_label = None
        self.stars_button = None
        self.dev_icon_label = None
        self.contributors_strip_layout = None

        self._github_loaded.connect(self._update_github_labels)
        self._contributors_loaded.connect(self._render_contributors)
        self._contributors_failed.connect(self._render_contributors_error)

        self._build_ui()
        threading.Thread(target=self._fetch_github_data, daemon=True).start()
        threading.Thread(target=self._fetch_contributors, daemon=True).start()

    def _build_ui(self):
        self.setStyleSheet(
            """
            QDialog {
                background-color: #1f1f1f;
                color: white;
            }
            QFrame#card {
                background-color: #2b2b2b;
                border: 1px solid #3a3a3a;
                border-radius: 14px;
            }
            QLabel {
                color: white;
                background: transparent;
            }
            QLabel[role="title"] {
                font-size: 22px;
                font-weight: 700;
            }
            QLabel[role="section"] {
                font-size: 16px;
                font-weight: 700;
            }
            QLabel[role="body"] {
                font-size: 14px;
            }
            QLabel[role="footer"] {
                font-size: 12px;
                font-style: italic;
                color: #cfcfcf;
            }
            QFrame[role="separator"] {
                background-color: #555555;
                max-height: 1px;
                min-height: 1px;
            }
            QPushButton {
                min-height: 36px;
                padding: 6px 12px;
                border-radius: 8px;
                border: 1px solid #4a4a4a;
                background-color: #3a3a3a;
                color: white;
                font-size: 14px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: #4a4a4a;
            }
            QPushButton[role="link"] {
                background-color: transparent;
                color: white;
                border: none;
                text-align: left;
                padding: 4px 8px;
                font-size: 14px;
                font-weight: 400;
            }
            QPushButton[role="link"]:hover {
                background-color: #3a3a3a;
                border-radius: 6px;
            }
            QPushButton[role="social"] {
                min-height: 30px;
                padding: 4px 10px;
                border-radius: 6px;
                border: 1px solid #4a4a4a;
                background-color: #3a3a3a;
                color: white;
                font-size: 12px;
                font-weight: 600;
            }
            QPushButton[role="social"]:hover {
                background-color: #4a4a4a;
            }
            """
        )

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(0)

        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(10)

        self.title_label = QLabel(self.translate("ABOUT_THIS_APP"))
        self.title_label.setProperty("role", "title")
        self.title_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        card_layout.addWidget(self.title_label)

        details = [
            (
                "resources/img/iconos/about/user-account-solid-24.png",
                f"{self.translate('DEVELOPER_LABEL')}: Emy69",
                "developer",
            ),
            (
                "resources/img/iconos/about/git-branch-line.png",
                f"{self.translate('VERSION_LABEL')}: {self.version}",
                "version",
            ),
            (
                "resources/img/iconos/about/download_icon.png",
                f"{self.translate('TOTAL_DOWNLOADS_LABEL')}: {self.translate('LOADING')}",
                "downloads",
            ),
            (
                "resources/img/iconos/about/calendar-event-line.png",
                f"{self.translate('RELEASE_DATE_LABEL')}: {self.translate('LOADING')}",
                "date",
            ),
        ]

        for icon_path, text, key in details:
            row, text_label = self._make_detail_row(icon_path, text)
            card_layout.addWidget(row)

            if key == "downloads":
                self.downloads_label = text_label
            elif key == "date":
                self.date_label = text_label
            elif key == "developer":
                row.setCursor(Qt.PointingHandCursor)
                row.setToolTip("https://github.com/Emy69")
                text_label.setStyleSheet(
                    "color: #7aa7ff; text-decoration: underline;"
                )
                row.mousePressEvent = (
                    lambda _ev: self._open_url("https://github.com/Emy69")
                )
                self.dev_icon_label = row.icon_label

        separator = QFrame()
        separator.setProperty("role", "separator")
        card_layout.addWidget(separator)

        links_separator = QFrame()
        links_separator.setProperty("role", "separator")
        card_layout.addWidget(links_separator)

        self.community_label = QLabel(self.translate("COMMUNITY_SECTION"))
        self.community_label.setProperty("role", "section")
        self.community_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        card_layout.addWidget(self.community_label)

        socials_row = QHBoxLayout()
        socials_row.setContentsMargins(0, 0, 0, 0)
        socials_row.setSpacing(6)

        social_icon_dir = os.path.join("resources", "img", "iconos", "ui", "social")

        def _make_social(icon_file, tooltip, url, text=None):
            btn = QPushButton()
            btn.setProperty("role", "social")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(tooltip)
            icon_path = os.path.join(social_icon_dir, icon_file)
            if os.path.exists(icon_path):
                btn.setIcon(QIcon(icon_path))
                btn.setIconSize(QSize(18, 18))
            if text:
                btn.setText(text)
            btn.clicked.connect(lambda: self._open_url(url))
            return btn

        self.discord_button = _make_social(
            "discord-alt-logo-24.png", "Discord", "https://discord.gg/6zbjrJbJ3Q"
        )
        self.stars_button = _make_social(
            "github-logo-24.png",
            "GitHub",
            "https://github.com/Emy69/CoomerDL",
            text=f"★ {self.translate('LOADING')}",
        )
        self.patron_button = _make_social(
            "patreon-logo-24.png", "Patreon", "https://www.patreon.com/Emy69"
        )
        self.x_button = _make_social(
            "twitter-x-fill.png", "X (Twitter)", "https://x.com/dev_emy"
        )

        for btn in (self.discord_button, self.stars_button, self.patron_button, self.x_button):
            socials_row.addWidget(btn, 1)

        card_layout.addLayout(socials_row)

        contributors_separator = QFrame()
        contributors_separator.setProperty("role", "separator")
        card_layout.addWidget(contributors_separator)

        self.contributors_label = QLabel(self.translate("CONTRIBUTORS_SECTION"))
        self.contributors_label.setProperty("role", "section")
        self.contributors_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.contributors_label.setCursor(Qt.PointingHandCursor)
        self.contributors_label.mousePressEvent = lambda _ev: self._open_url(
            "https://github.com/Emy69/CoomerDL/graphs/contributors"
        )
        card_layout.addWidget(self.contributors_label)

        self.contributors_strip = QScrollArea()
        self.contributors_strip.setWidgetResizable(True)
        self.contributors_strip.setFixedHeight(96)
        self.contributors_strip.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.contributors_strip.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        strip_content = QWidget()
        self.contributors_strip_layout = QHBoxLayout(strip_content)
        self.contributors_strip_layout.setContentsMargins(4, 4, 4, 4)
        self.contributors_strip_layout.setSpacing(10)
        self.contributors_strip_layout.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self.contributors_status_label = QLabel(self.translate("LOADING"))
        self.contributors_status_label.setProperty("role", "body")
        self.contributors_status_label.setAlignment(Qt.AlignCenter)
        self.contributors_strip_layout.addWidget(self.contributors_status_label)
        self.contributors_strip_layout.addStretch()

        self.contributors_strip.setWidget(strip_content)
        card_layout.addWidget(self.contributors_strip)

        card_layout.addStretch()

        self.footer_label = QLabel(self.translate("THANK_YOU_FOR_USING_APP"))
        self.footer_label.setProperty("role", "footer")
        self.footer_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        card_layout.addWidget(self.footer_label)

        root_layout.addWidget(card)

    def _make_detail_row(self, icon_path: str, text: str):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(10)

        icon_label = QLabel()
        pixmap = QPixmap(icon_path)
        if not pixmap.isNull():
            icon_label.setPixmap(
                pixmap.scaled(20, 20, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        icon_label.setFixedSize(20, 20)
        icon_label.setAlignment(Qt.AlignCenter)

        text_label = QLabel(text)
        text_label.setProperty("role", "body")
        text_label.setWordWrap(True)
        text_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        layout.addWidget(icon_label, 0, Qt.AlignTop)
        layout.addWidget(text_label, 1)

        row.icon_label = icon_label
        return row, text_label

    def _open_url(self, url: str):
        QDesktopServices.openUrl(QUrl(url))

    def _fetch_github_data(self):
        avatar_bytes = None
        try:
            repo_data = requests.get("https://api.github.com/repos/Emy69/CoomerDL", timeout=15).json()
            created_at = repo_data.get("created_at", "N/A")
            created_date = created_at.split("T")[0] if created_at != "N/A" else "N/A"
            stars = int(repo_data.get("stargazers_count", 0))
            releases_url = repo_data.get("releases_url", "").replace("{/id}", "")
            total_downloads = 0
            if releases_url:
                releases_data = requests.get(releases_url, timeout=15).json() or []
                total_downloads = sum(
                    asset.get("download_count", 0)
                    for release in releases_data
                    for asset in release.get("assets", [])
                )
            try:
                av = requests.get("https://github.com/Emy69.png", headers=ABOUT_HEADERS, timeout=15)
                av.raise_for_status()
                avatar_bytes = bytes(av.content)
            except Exception:
                pass
            self._github_loaded.emit(created_date, total_downloads, stars, avatar_bytes)
        except Exception:
            self._github_loaded.emit("N/A", 0, 0, avatar_bytes)

    def _fetch_contributors(self):
        try:
            response = requests.get(
                "https://api.github.com/repos/Emy69/CoomerDL/contributors",
                headers={**ABOUT_HEADERS, "Accept": "application/vnd.github+json"},
                params={"per_page": 100},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            contributors = [
                {
                    "login": c.get("login") or "",
                    "avatar_url": c.get("avatar_url") or "",
                    "html_url": c.get("html_url") or f"https://github.com/{c.get('login') or ''}",
                    "contributions": int(c.get("contributions") or 0),
                    "avatar_bytes": None,
                }
                for c in (data if isinstance(data, list) else [])
                if c.get("type") != "Bot" and (c.get("login") or "").lower() != "emy69"
            ]

            def fetch_avatar(item):
                url = item["avatar_url"]
                if url:
                    try:
                        r = requests.get(url, headers=ABOUT_HEADERS, timeout=15)
                        r.raise_for_status()
                        item["avatar_bytes"] = bytes(r.content)
                    except Exception:
                        pass
                return item

            if contributors:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    contributors = list(pool.map(fetch_avatar, contributors))
            self._contributors_loaded.emit(contributors)
        except Exception as exc:
            self._contributors_failed.emit(str(exc))

    def _update_github_labels(self, created_date: str, total_downloads: int, stars: int, avatar_bytes):
        if self.date_label is not None:
            self.date_label.setText(f"{self.translate('RELEASE_DATE_LABEL')}: {created_date}")
        if self.downloads_label is not None:
            self.downloads_label.setText(f"{self.translate('TOTAL_DOWNLOADS_LABEL')}: {total_downloads:,}")
        if self.stars_button is not None:
            self.stars_button.setText(f"★ {stars:,}")
        if avatar_bytes and self.dev_icon_label is not None:
            pixmap = self._build_circular_avatar(avatar_bytes, size=20)
            if pixmap is not None:
                self.dev_icon_label.setPixmap(pixmap)

    def _clear_contributors_strip(self):
        if self.contributors_strip_layout is None:
            return
        while self.contributors_strip_layout.count():
            item = self.contributors_strip_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _render_contributors_error(self, _err):
        self._clear_contributors_strip()
        label = QLabel(self.translate("CONTRIBUTORS_LOAD_FAILED"))
        label.setProperty("role", "body")
        label.setAlignment(Qt.AlignCenter)
        self.contributors_strip_layout.addWidget(label)
        self.contributors_strip_layout.addStretch()

    def _render_contributors(self, contributors):
        self._clear_contributors_strip()

        if not contributors:
            label = QLabel(self.translate("NO_CONTRIBUTORS_FOUND"))
            label.setProperty("role", "body")
            label.setAlignment(Qt.AlignCenter)
            self.contributors_strip_layout.addWidget(label)
            self.contributors_strip_layout.addStretch()
            return

        for contributor in contributors:
            login = contributor.get("login") or "?"
            profile_url = contributor.get("html_url") or f"https://github.com/{login}"
            contributions = contributor.get("contributions") or 0
            avatar_bytes = contributor.get("avatar_bytes")

            cell = QWidget()
            cell.setCursor(Qt.PointingHandCursor)
            cell.setToolTip(f"{login} — {contributions} commits")
            cell.mousePressEvent = lambda _ev, u=profile_url: self._open_url(u)

            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(2, 2, 2, 2)
            cell_layout.setSpacing(4)
            cell_layout.setAlignment(Qt.AlignCenter)

            avatar_label = QLabel()
            avatar_label.setFixedSize(48, 48)
            avatar_label.setAlignment(Qt.AlignCenter)

            pixmap = self._build_circular_avatar(avatar_bytes)
            if pixmap is not None:
                avatar_label.setPixmap(pixmap)
                avatar_label.setStyleSheet("background: transparent;")
            else:
                avatar_label.setStyleSheet(
                    "background-color: #3a3a3a; border-radius: 24px;"
                )

            name_label = QLabel(login if len(login) <= 10 else login[:9] + "…")
            name_label.setStyleSheet("font-size: 11px; color: #e0e0e0;")
            name_label.setAlignment(Qt.AlignCenter)
            name_label.setFixedWidth(76)

            cell_layout.addWidget(avatar_label, 0, Qt.AlignHCenter)
            cell_layout.addWidget(name_label, 0, Qt.AlignHCenter)

            self.contributors_strip_layout.addWidget(cell)

        self.contributors_strip_layout.addStretch()

    @staticmethod
    def _build_circular_avatar(data, size=48):
        if not data:
            return None

        image = QImage()
        if not image.loadFromData(QByteArray(data)):
            return None

        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            return None

        scaled = pixmap.scaled(
            size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
        )

        rounded = QPixmap(size, size)
        rounded.fill(Qt.transparent)
        painter = QPainter(rounded)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        path = QPainterPath()
        path.addEllipse(QRectF(0, 0, size, size))
        painter.setClipPath(path)
        x = (size - scaled.width()) // 2
        y = (size - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)
        painter.end()

        return rounded

    def show_about(self):
        self.exec()
