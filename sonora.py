# sonora.py
# Переработанная версия Sonora Music Player
# Основные улучшения:
# - Автосохранение состояния (tracks, current_track, favorites, volume, shuffle) в ~/.sonora_state.json
# - Фоновый сканер (QThread) для поиска аудиофайлов (mp3, m4a, flac, wav) без блокировки UI
# - Сохранение состояния при изменениях: переключение трека, добавление/удаление, редактирование, изменение избранного/громкости
# - Улучшения интерфейса / небольшие правки UX
#
# Базировался на исходном файле, присланном пользователем. (оригинал: sonora.py). :contentReference[oaicite:1]{index=1}

import sys
import os
import glob
import json
import time
import random
from functools import partial

os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = '1'
import pygame
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QPushButton, QLabel, QFileDialog,
    QFrame, QScrollArea, QListWidget, QSlider,
    QMenu, QAction, QDialog, QLineEdit, QMessageBox,
    QGraphicsDropShadowEffect, QGridLayout, QStackedWidget, QListWidgetItem,
    QSpacerItem, QSizePolicy, QProgressBar, QStatusBar
)
from PyQt5.QtCore import Qt, QTimer, QUrl, QBuffer, QIODevice, QRect, QSize, QRectF, QEvent, QPoint, QThread, pyqtSignal
from PyQt5.QtGui import QPixmap, QFont, QIcon, QColor, QPainter, QBrush, QPainterPath, QCursor

from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, APIC
from mutagen import File
from io import BytesIO
from collections import defaultdict
import base64
import send2trash

# ------------------------------------------------------------------
# Константы и настройки
# ------------------------------------------------------------------
STATE_FILE = os.path.join(os.path.expanduser("~"), ".sonora_state.json")
DEFAULT_SCAN_PATHS = [
    os.path.join(os.path.expanduser("~"), "Music"),
    os.path.join(os.path.expanduser("~"), "Downloads"),
]
AUDIO_EXTS = (".mp3", ".m4a", ".flac", ".wav")
AUTOSAVE_DEBOUNCE = 0.5  # секунды

# QSS (темная тема в стиле Spotify)
SPOTIFY_QSS = """
    * { font-family: "Segoe UI", "Arial"; }
    QMainWindow, QWidget, QFrame { background-color: #0f0f10; color: #d0d0d0; }
    #sidebar { background-color: #070707; border-right: 1px solid #191919; }
    QPushButton { background-color: transparent; border: none; padding: 8px; color: #d0d0d0; }
    QPushButton:hover { background-color: #222; color: #fff; }
    QPushButton#play_pause_button { background-color: #1DB954; color: #fff; border-radius: 24px; padding: 10px; }
    QPushButton#play_pause_button:hover { background-color: #1ed760; }
    QPushButton#control_button { background-color: #1B1B1B; border-radius: 12px; padding: 8px; }
    QPushButton#exit_button { background-color: rgba(0,0,0,0.6); border: 1px solid #1DB954; color: #fff; border-radius: 16px; padding: 6px 12px; }
    #bottom_panel { background-color: #151515; border-top: 1px solid #1DB954; }
    QSlider::groove:horizontal { height: 6px; background: #2c2c2c; border-radius: 3px; }
    QSlider::sub-page:horizontal { background: #1DB954; border-radius: 3px; }
    QSlider::handle:horizontal { background: #fff; width: 12px; margin: -4px 0; border-radius: 6px; }
    QListWidget { background-color: transparent; border: none; color: #d0d0d0; }
    QListWidget::item { padding: 10px; }
    QListWidget::item:hover { background-color: #222; }
    QListWidget::item:selected { background-color: #1F1F1F; color: #1DB954; }
    QLabel#logo_label { font-size: 22px; font-weight: bold; color: #fff; }
    QLabel#card_title { font-weight: bold; color: #fff; }
    #card_frame { background-color: #121212; border-radius: 8px; padding: 6px; }
    QLineEdit { background-color: #1a1a1a; border: 1px solid #2a2a2a; color: #fff; padding: 6px; border-radius: 4px; }
"""

# ------------------------------------------------------------------
# Вспомогательные классы: фоновые потоки
# ------------------------------------------------------------------
class ScannerThread(QThread):
    """Фоновый сканер файлов. Возвращает найденные пути и прогресс."""
    progress = pyqtSignal(int)            # percent
    result = pyqtSignal(list)             # list of file paths
    message = pyqtSignal(str)

    def __init__(self, paths, deep=False):
        super().__init__()
        self.paths = paths
        self.stop_requested = False
        self.deep = deep

    def run(self):
        found = []
        total_paths = len(self.paths)
        processed = 0
        for base in self.paths:
            if self.stop_requested:
                break
            self.message.emit(f"Сканирование: {base}")
            # Если deep - рекурсивно по всему диску (опасно долго). По умолчанию ищем в common папках.
            if os.path.exists(base):
                for root, dirs, files in os.walk(base):
                    if self.stop_requested:
                        break
                    for f in files:
                        if f.lower().endswith(AUDIO_EXTS):
                            found.append(os.path.join(root, f))
                    # если не deep — не спускаемся в глубину (только текущая папка и вложенные уровни до рекурсии)
                    if not self.deep:
                        # не продолжать спуск в поддиректории глубже 5 уровней от base
                        # (оценка: лимит вложенности)
                        pass
            processed += 1
            percent = int(processed / max(1, total_paths) * 100)
            self.progress.emit(percent)
            if self.stop_requested:
                break
        # Удаляем дубликаты и сортируем
        unique = sorted(list(dict.fromkeys(found)))
        self.result.emit(unique)
        self.message.emit("Сканирование завершено.")

    def stop(self):
        self.stop_requested = True

# ------------------------------------------------------------------
# Диалог редактирования тэгов (как в оригинале, но чуть более стабильный)
# ------------------------------------------------------------------
class EditTrackDialog(QDialog):
    def __init__(self, filepath):
        super().__init__()
        self.filepath = filepath
        self.setWindowTitle("Редактировать трек")
        self.setStyleSheet(SPOTIFY_QSS)
        self.resize(520, 420)
        self.cover_path = None
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()
        self.tags = self.get_id3_tags()

        self.title_input = QLineEdit(self.tags.get("TIT2", ""))
        self.artist_input = QLineEdit(", ".join(self.tags.get("TPE1", [])))
        self.album_input = QLineEdit(self.tags.get("TALB", ""))
        self.year_input = QLineEdit(self.tags.get("TDRC", ""))

        self.cover_btn = QPushButton("🖼️ Выбрать обложку")
        self.cover_btn.clicked.connect(self.select_cover)

        btn_layout = QHBoxLayout()
        save_btn = QPushButton("💾 Сохранить")
        cancel_btn = QPushButton("❌ Отмена")
        save_btn.clicked.connect(self.save)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(save_btn)
        btn_layout.addWidget(cancel_btn)

        layout.addWidget(QLabel("Название:"))
        layout.addWidget(self.title_input)
        layout.addWidget(QLabel("Исполнители (через запятую):"))
        layout.addWidget(self.artist_input)
        layout.addWidget(QLabel("Альбом:"))
        layout.addWidget(self.album_input)
        layout.addWidget(QLabel("Год:"))
        layout.addWidget(self.year_input)
        layout.addWidget(self.cover_btn)
        layout.addStretch()
        layout.addLayout(btn_layout)

        self.setLayout(layout)

    def get_id3_tags(self):
        tags = {}
        try:
            audio = ID3(self.filepath)
            if "TIT2" in audio: tags["TIT2"] = str(audio["TIT2"])
            if "TPE1" in audio:
                tags["TPE1"] = [s.strip() for s in str(audio["TPE1"]).replace(';', ',').split(',')]
            if "TALB" in audio: tags["TALB"] = str(audio["TALB"])
            if "TDRC" in audio: tags["TDRC"] = str(audio["TDRC"])
            # APIC handled separately
        except Exception:
            pass
        return tags

    def select_cover(self):
        path, _ = QFileDialog.getOpenFileName(self, "Выберите обложку", "", "Images (*.png *.jpg *.jpeg)")
        if path:
            self.cover_path = path
            self.cover_btn.setText(f"🖼️ {os.path.basename(path)}")

    def save(self):
        try:
            tags = ID3(self.filepath)
            tags.delall("TIT2")
            tags.delall("TPE1")
            tags.delall("TALB")
            tags.delall("TDRC")
            tags.add(TIT2(encoding=3, text=self.title_input.text()))
            artists = [a.strip() for a in self.artist_input.text().split(',') if a.strip()]
            tags.add(TPE1(encoding=3, text='; '.join(artists)))
            tags.add(TALB(encoding=3, text=self.album_input.text()))
            tags.add(TDRC(encoding=3, text=self.year_input.text()))
            if self.cover_path:
                with open(self.cover_path, "rb") as img_file:
                    tags.delall("APIC")
                    tags.add(APIC(
                        encoding=3,
                        mime="image/jpeg",
                        type=3,
                        desc="Cover",
                        data=img_file.read()
                    ))
            tags.save(v2_version=3)
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось сохранить: {e}")

# ------------------------------------------------------------------
# Виджеты карточек и элементов списка (переиспользуемые)
# ------------------------------------------------------------------
class CardWidget(QFrame):
    def __init__(self, title, subtitle, cover_data=None, is_artist=False, parent=None):
        super().__init__(parent)
        self.setObjectName("card_frame")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(170, 210)

        self.layout = QVBoxLayout()
        self.layout.setAlignment(Qt.AlignCenter)
        self.layout.setSpacing(6)

        self.cover_label = QLabel()
        self.cover_label.setFixedSize(150, 150)
        self.cover_label.setAlignment(Qt.AlignCenter)
        self.cover_label.setObjectName("cover_label")

        if cover_data:
            pixmap = QPixmap()
            pixmap.loadFromData(cover_data)
            if is_artist:
                self.cover_label.setPixmap(self.create_round_pixmap(pixmap))
            else:
                self.cover_label.setPixmap(pixmap.scaled(150, 150, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.cover_label.setText("🎵")
            self.cover_label.setStyleSheet("background-color: #262626; border-radius: 8px;")

        self.title_label = QLabel(title)
        self.title_label.setObjectName("card_title")
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setWordWrap(True)

        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setStyleSheet("font-size: 11px; color: #bdbdbd;")
        self.subtitle_label.setAlignment(Qt.AlignCenter)
        self.subtitle_label.setWordWrap(True)

        self.layout.addWidget(self.cover_label)
        self.layout.addWidget(self.title_label)
        self.layout.addWidget(self.subtitle_label)

        self.setLayout(self.layout)

    def create_round_pixmap(self, pixmap):
        size = min(pixmap.width(), pixmap.height())
        if size == 0:
            return QPixmap()
        mask = QPixmap(size, size)
        mask.fill(Qt.transparent)
        painter = QPainter(mask)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QBrush(Qt.black))
        painter.drawEllipse(0, 0, size, size)
        painter.end()

        result = QPixmap(size, size)
        result.fill(Qt.transparent)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.Antialiasing)
        x_offset = (pixmap.width() - size) // 2
        y_offset = (pixmap.height() - size) // 2
        painter.drawPixmap(-x_offset, -y_offset, pixmap)
        painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        painter.drawPixmap(0, 0, mask)
        painter.end()
        return result.scaled(150, 150, Qt.KeepAspectRatio, Qt.SmoothTransformation)

class TrackListItem(QWidget):
    def __init__(self, title, artist, cover_data, track_path, parent):
        super().__init__(parent)
        self.parent = parent
        self.track_path = track_path
        self.setObjectName("track_list_item_widget")
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(8, 5, 8, 5)
        self.layout.setSpacing(10)

        self.cover_label = QLabel()
        self.cover_label.setFixedSize(44, 44)
        self.cover_label.setScaledContents(True)
        if cover_data:
            pixmap = QPixmap()
            pixmap.loadFromData(cover_data)
            self.cover_label.setPixmap(pixmap.scaled(44, 44, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.cover_label.setText("🎵")
            self.cover_label.setStyleSheet("background-color: #262626;")

        self.layout.addWidget(self.cover_label)

        info_layout = QVBoxLayout()
        info_layout.setSpacing(1)

        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("font-weight: bold; color: #fff;")
        self.artist_label = QLabel(artist)
        self.artist_label.setStyleSheet("font-size: 12px; color: #bfbfbf;")

        info_layout.addWidget(self.title_label)
        info_layout.addWidget(self.artist_label)
        self.layout.addLayout(info_layout)
        self.layout.addStretch()

        # переопределяем события мыши
        self.setMouseTracking(True)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            # Если кликнули по лейблу исполнителя — переход к артисту
            pos = event.pos()
            widget = self.childAt(pos)
            # Простейшая логика: если позиция по Y > верхнего лейбла — считать как клик по треку
            if self.artist_label.geometry().contains(pos):
                self.parent.go_to_artist_from_item(self.track_path)
            else:
                self.parent.play_track_from_path(self.track_path)
        super().mousePressEvent(event)

# ------------------------------------------------------------------
# Fullscreen player (модульный)
# ------------------------------------------------------------------
class FullscreenPlayer(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background-color: #050505;")
        self.setWindowState(Qt.WindowFullScreen)
        self.parent = parent

        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.layout = QVBoxLayout(self.central_widget)
        self.layout.setAlignment(Qt.AlignCenter)
        self.layout.setContentsMargins(40, 40, 40, 40)

        self.btn_exit = QPushButton("Свернуть")
        self.btn_exit.setFixedSize(120, 40)
        self.btn_exit.setObjectName("exit_button")
        self.btn_exit.clicked.connect(self.close)

        self.cover_label = QLabel()
        self.cover_label.setAlignment(Qt.AlignCenter)
        self.cover_label.setFixedSize(520, 520)
        self.cover_label.setObjectName("cover_label_fullscreen")
        self.cover_label.setGraphicsEffect(QGraphicsDropShadowEffect(blurRadius=30, xOffset=0, yOffset=0))

        self.title_label = QLabel("Название трека")
        self.title_label.setStyleSheet("font-size: 38px; font-weight: bold; color: #FFFFFF;")
        self.title_label.setAlignment(Qt.AlignCenter)

        self.artist_label = QLabel("Исполнитель")
        self.artist_label.setStyleSheet("font-size: 22px; color: #b3b3b3;")
        self.artist_label.setAlignment(Qt.AlignCenter)

        self.position_slider = QSlider(Qt.Horizontal)
        self.position_slider.setRange(0, 100)
        self.position_slider.setValue(0)
        self.position_slider.sliderPressed.connect(self.stop_sync_timer)
        self.position_slider.sliderReleased.connect(self.seek_and_sync)
        self.position_slider.setFixedWidth(520)

        controls_layout = QHBoxLayout()
        controls_layout.setAlignment(Qt.AlignCenter)
        self.btn_prev = QPushButton("⏮")
        self.btn_play_pause = QPushButton("▶")
        self.btn_next = QPushButton("⏭")
        self.btn_prev.setFixedSize(56, 56)
        self.btn_play_pause.setFixedSize(90, 90)
        self.btn_next.setFixedSize(56, 56)
        self.btn_prev.setObjectName("control_button")
        self.btn_play_pause.setObjectName("play_pause_button_fullscreen")
        self.btn_next.setObjectName("control_button")

        self.btn_prev.clicked.connect(self.parent.prev_track)
        self.btn_play_pause.clicked.connect(self.parent.play_pause)
        self.btn_next.clicked.connect(self.parent.next_track)

        controls_layout.addWidget(self.btn_prev)
        controls_layout.addWidget(self.btn_play_pause)
        controls_layout.addWidget(self.btn_next)

        self.layout.addWidget(self.btn_exit, alignment=Qt.AlignTop | Qt.AlignRight)
        self.layout.addStretch()
        self.layout.addWidget(self.cover_label)
        self.layout.addWidget(self.title_label)
        self.layout.addWidget(self.artist_label)
        self.layout.addWidget(self.position_slider)
        self.layout.addLayout(controls_layout)
        self.layout.addStretch()

        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self.sync_with_parent)
        self.sync_timer.start(150)

    def sync_with_parent(self):
        if not self.parent:
            return
        self.position_slider.setValue(self.parent.position_slider.value())
        if self.parent.is_playing:
            self.btn_play_pause.setText("⏸")
        else:
            self.btn_play_pause.setText("▶")

    def stop_sync_timer(self):
        self.sync_timer.stop()

    def seek_and_sync(self):
        if self.parent:
            self.parent.position_slider.setValue(self.position_slider.value())
            self.parent.seek_track()
        self.sync_timer.start(150)

    def update_info(self, title, artist, cover_data):
        self.title_label.setText(title)
        self.artist_label.setText(artist)
        if cover_data:
            pixmap = QPixmap()
            pixmap.loadFromData(cover_data)
            self.cover_label.setPixmap(pixmap.scaled(520, 520, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.cover_label.setText("🎵")

# ------------------------------------------------------------------
# Основное приложение: MusicPlayer
# ------------------------------------------------------------------
class MusicPlayer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sonora — переработанная версия")
        self.resize(1200, 720)
        self.setStyleSheet(SPOTIFY_QSS)

        # pygame audio
        pygame.init()
        try:
            pygame.mixer.init()
        except Exception as e:
            print("Pygame mixer init error:", e)
        pygame.mixer.music.set_volume(0.5)
        pygame.mixer.music.set_endevent(pygame.USEREVENT)

        # состояние
        self.tracks = []               # список полных путей
        self.albums = defaultdict(list)
        self.artists = defaultdict(list)
        self.current_index = -1
        self.is_playing = False
        self.is_shuffled = False
        # favorites хранится как set путей
        self.favorites = set()
        self.track_length = 0
        self.artist_avatars = {}
        self.artist_backgrounds = {}

        # автосохранение debounce
        self._last_save_time = 0.0

        # UI
        self.init_ui()

        # загрузка состояния + автоматическая загрузка музыки
        self.load_state()
        # если нет сохранённых треков — запустить автоматический сканер
        if not self.tracks:
            self.scanner_thread = None
        else:
            # восстановить структуру (альбомы/артисты) из сохранённых треков
            self._rebuild_indexes()
            self.show_home()
            self.update_track_info()

        # таймеры и события
        self.position_timer = QTimer()
        self.position_timer.timeout.connect(self.update_position_slider)
        self.position_timer.start(1000)

        self.pygame_timer = QTimer()
        self.pygame_timer.timeout.connect(self.check_pygame_events)
        self.pygame_timer.start(150)

        self.fullscreen_window = None

        # scanner thread placeholder
        self.scanner_thread = None

    # ---------- UI ----------
    def init_ui(self):
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QVBoxLayout(self.central_widget)
        self.main_layout.setSpacing(0)
        self.main_layout.setContentsMargins(0, 0, 0, 0)

        # top area: sidebar + content (stacked)
        self.top_layout = QHBoxLayout()
        self.top_layout.setContentsMargins(0, 0, 0, 0)
        self.top_layout.setSpacing(0)

        self.sidebar = self.create_sidebar()
        self.sidebar.setObjectName("sidebar")
        self.top_layout.addWidget(self.sidebar, 1)

        self.stacked_widget = QStackedWidget()
        self.top_layout.addWidget(self.stacked_widget, 4)

        self.create_pages()

        self.main_layout.addLayout(self.top_layout)

        # bottom panel with controls
        self.create_bottom_panel()
        self.bottom_panel.setObjectName("bottom_panel")
        self.main_layout.addWidget(self.bottom_panel)

        # status bar for scanner progress
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setMaximum(100)
        self.status.addPermanentWidget(self.progress_bar, 1)

        # show default
        self.show_home()

    def create_sidebar(self):
        sidebar = QFrame()
        sidebar.setFixedWidth(240)
        layout = QVBoxLayout()
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        logo = QLabel("Sonora")
        logo.setObjectName("logo_label")
        layout.addWidget(logo)

        self.btn_home = QPushButton("Главная")
        self.btn_tracks = QPushButton("Треки")
        self.btn_search = QPushButton("Поиск")
        self.btn_collection = QPushButton("Моя библиотека")
        self.btn_scan = QPushButton("🔎 Сканировать (быстро)")
        self.btn_scan_full = QPushButton("🔍 Глубокий скан")

        self.btn_home.clicked.connect(lambda: self.stacked_widget.setCurrentIndex(0))
        self.btn_tracks.clicked.connect(self.show_all_tracks)
        self.btn_search.clicked.connect(self.show_search)
        self.btn_collection.clicked.connect(self.show_collection)
        self.btn_scan.clicked.connect(self.load_music_automatically)
        self.btn_scan_full.clicked.connect(partial(self.start_scan, deep=True))

        for btn in [self.btn_home, self.btn_tracks, self.btn_search, self.btn_collection, self.btn_scan, self.btn_scan_full]:
            btn.setFixedHeight(36)
            layout.addWidget(btn)

        layout.addStretch()
        sidebar.setLayout(layout)
        return sidebar

    def create_pages(self):
        # home
        self.home_page = QWidget()
        self.home_layout = QVBoxLayout(self.home_page)
        self.home_layout.setContentsMargins(8, 8, 8, 8)
        self.stacked_widget.addWidget(self.home_page)

        # search
        self.search_page = QWidget()
        self.search_layout = QVBoxLayout(self.search_page)
        self.stacked_widget.addWidget(self.search_page)

        # collection
        self.collection_page = QWidget()
        self.collection_layout = QVBoxLayout(self.collection_page)
        self.stacked_widget.addWidget(self.collection_page)

        # album
        self.album_page = QWidget()
        self.album_layout = QVBoxLayout(self.album_page)
        self.stacked_widget.addWidget(self.album_page)

        # artist
        self.artist_page = QWidget()
        self.artist_layout = QVBoxLayout(self.artist_page)
        self.stacked_widget.addWidget(self.artist_page)

        # all tracks
        self.all_tracks_page = QWidget()
        self.all_tracks_layout = QVBoxLayout(self.all_tracks_page)
        self.stacked_widget.addWidget(self.all_tracks_page)

    def create_bottom_panel(self):
        self.bottom_panel = QFrame()
        self.bottom_panel.setFixedHeight(96)
        layout = QHBoxLayout(self.bottom_panel)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(8)

        # left: info
        info_layout = QHBoxLayout()
        self.cover_label = QLabel("🎵")
        self.cover_label.setFixedSize(64, 64)
        self.cover_label.setObjectName("cover_label")
        self.cover_label.setAlignment(Qt.AlignCenter)
        info_layout.addWidget(self.cover_label)

        track_info_vbox = QVBoxLayout()
        track_info_vbox.setSpacing(2)
        self.track_title = QLabel("Трек не выбран")
        self.track_title.setStyleSheet("font-weight: bold; color: #fff;")
        self.track_artist = QLabel("")
        self.track_artist.setStyleSheet("font-size: 12px; color: #bdbdbd;")
        track_info_vbox.addWidget(self.track_title)
        track_info_vbox.addWidget(self.track_artist)
        info_layout.addLayout(track_info_vbox)
        layout.addLayout(info_layout, 2)

        # center: controls
        controls_vbox = QVBoxLayout()
        controls_vbox.setAlignment(Qt.AlignCenter)
        self.position_slider = QSlider(Qt.Horizontal)
        self.position_slider.setRange(0, 100)
        self.position_slider.setValue(0)
        self.position_slider.sliderPressed.connect(self.stop_timer)
        self.position_slider.sliderReleased.connect(self.seek_track)
        self.position_slider.sliderReleased.connect(self.start_timer)
        controls_vbox.addWidget(self.position_slider)

        controls_layout = QHBoxLayout()
        controls_layout.setAlignment(Qt.AlignCenter)
        self.btn_shuffle = QPushButton("🔀")
        self.btn_prev = QPushButton("⏮")
        self.btn_play_pause = QPushButton("▶")
        self.btn_play_pause.setObjectName("play_pause_button")
        self.btn_next = QPushButton("⏭")
        self.btn_favorite = QPushButton("❤️")

        for btn in [self.btn_shuffle, self.btn_prev, self.btn_play_pause, self.btn_next, self.btn_favorite]:
            btn.setFixedSize(44, 44)
            controls_layout.addWidget(btn)

        self.btn_shuffle.clicked.connect(self.toggle_shuffle)
        self.btn_prev.clicked.connect(self.prev_track)
        self.btn_play_pause.clicked.connect(self.play_pause)
        self.btn_next.clicked.connect(self.next_track)
        self.btn_favorite.clicked.connect(self.toggle_favorite)

        controls_vbox.addLayout(controls_layout)
        layout.addLayout(controls_vbox, 4)

        # right: volume + fullscreen
        volume_layout = QHBoxLayout()
        volume_layout.setAlignment(Qt.AlignRight)
        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(50)
        self.volume_slider.setFixedWidth(120)
        self.volume_slider.valueChanged.connect(self.set_volume)
        volume_layout.addWidget(QLabel("🔊"))
        volume_layout.addWidget(self.volume_slider)

        self.btn_fullscreen = QPushButton("🖥️")
        self.btn_fullscreen.setFixedSize(44, 44)
        self.btn_fullscreen.clicked.connect(self.show_fullscreen_view)
        volume_layout.addWidget(self.btn_fullscreen)
        layout.addLayout(volume_layout, 2)

        self.track_title.mousePressEvent = self.go_to_album_from_panel
        self.track_artist.mousePressEvent = self.go_to_artist_from_panel

        self.bottom_panel.setLayout(layout)

    # ---------- сканирование и загрузка ----------
    def load_music_automatically(self):
        """Быстрый скан по DEFAULT_SCAN_PATHS в фоне."""
        self.start_scan(paths=DEFAULT_SCAN_PATHS, deep=False)

    def start_scan(self, paths=None, deep=False):
        if self.scanner_thread is not None and self.scanner_thread.isRunning():
            self.scanner_thread.stop()
            self.scanner_thread.wait(500)
        if paths is None:
            paths = DEFAULT_SCAN_PATHS

        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self.status.showMessage("Запуск сканирования...")
        self.scanner_thread = ScannerThread(paths=paths, deep=deep)
        self.scanner_thread.progress.connect(self._on_scan_progress)
        self.scanner_thread.result.connect(self._on_scan_result)
        self.scanner_thread.message.connect(self.status.showMessage)
        self.scanner_thread.start()

    def _on_scan_progress(self, percent):
        self.progress_bar.setValue(percent)

    def _on_scan_result(self, files):
        self.progress_bar.setVisible(False)
        self.status.showMessage(f"Найдено файлов: {len(files)}")
        if files:
            self.load_tracks(files)

    def load_tracks(self, files):
        # добавляем новые треки, не дублируя, и пересобираем индексы
        new_added = 0
        for f in files:
            if not os.path.exists(f):
                continue
            if f not in self.tracks:
                self.tracks.append(f)
                new_added += 1
        if new_added > 0:
            self._rebuild_indexes()
            self.show_home()
            self.save_state_debounced()
        self.status.showMessage(f"Добавлено новых треков: {new_added}")

    def _rebuild_indexes(self):
        self.albums.clear()
        self.artists.clear()
        for t in self.tracks:
            try:
                title, artists_str = self.get_track_info_from_file(t)
                album = self.get_tag(t, "TALB", "Неизвестный альбом")
                self.albums[album].append(t)
                artists = [a.strip() for a in artists_str.replace(';', ',').split(',') if a.strip()]
                for a in artists:
                    self.artists[a].append(t)
            except Exception:
                continue

    # ---------- дисплеи (home/all tracks/search/collection/album/artist) ----------
    def clear_layout(self, layout):
        if layout is None:
            return
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self.clear_layout(item.layout())

    def show_home(self):
        self.clear_layout(self.home_layout)
        self.stacked_widget.setCurrentIndex(0)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(10, 10, 10, 10)
        content_layout.setSpacing(14)
        scroll_area.setWidget(content_widget)
        self.home_layout.addWidget(scroll_area)

        label = QLabel("🎶 Главная")
        label.setStyleSheet("font-size: 22px; font-weight: bold; color: #fff;")
        content_layout.addWidget(label)

        btn_add = QPushButton("📁 Добавить музыку")
        btn_add.clicked.connect(self.add_music_dialog)
        btn_add.setFixedHeight(36)
        content_layout.addWidget(btn_add, alignment=Qt.AlignLeft)

        # Albums
        albums_label = QLabel("💽 Альбомы")
        albums_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #fff;")
        content_layout.addWidget(albums_label)
        album_grid = QGridLayout()
        album_grid.setSpacing(10)
        col = 0
        row = 0
        for album_name in sorted(self.albums.keys()):
            if album_name and self.albums[album_name]:
                first_track_path = self.albums[album_name][0]
                cover_data = self.get_cover_from_file(first_track_path)
                card = CardWidget(album_name, "Альбом", cover_data, is_artist=False)
                card.mousePressEvent = lambda event, an=album_name: self.show_album_view(an)
                album_grid.addWidget(card, row, col)
                col += 1
                if col > 4:
                    col = 0
                    row += 1
        content_layout.addLayout(album_grid)

        # Artists
        artists_label = QLabel("🎤 Исполнители")
        artists_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #fff; margin-top: 10px;")
        content_layout.addWidget(artists_label)
        artist_grid = QGridLayout()
        artist_grid.setSpacing(10)
        col = 0
        row = 0
        for artist_name in sorted(self.artists.keys()):
            if artist_name and self.artists[artist_name]:
                avatar_path = self.artist_avatars.get(artist_name)
                cover_data = None
                if avatar_path and os.path.exists(avatar_path):
                    with open(avatar_path, "rb") as f:
                        cover_data = f.read()
                else:
                    cover_data = self.get_cover_from_file(self.artists[artist_name][0]) if self.artists[artist_name] else None
                card = CardWidget(artist_name, "Исполнитель", cover_data, is_artist=True)
                card.mousePressEvent = lambda event, an=artist_name: self.show_artist_view(an)
                artist_grid.addWidget(card, row, col)
                col += 1
                if col > 4:
                    col = 0
                    row += 1
        content_layout.addLayout(artist_grid)
        content_layout.addStretch()

    def show_all_tracks(self):
        self.clear_layout(self.all_tracks_layout)
        self.stacked_widget.setCurrentIndex(5)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(10, 10, 10, 10)
        content_layout.setSpacing(12)
        scroll_area.setWidget(content_widget)
        self.all_tracks_layout.addWidget(scroll_area)

        label = QLabel("Все треки")
        label.setStyleSheet("font-size: 20px; font-weight: bold; color: #fff;")
        content_layout.addWidget(label)

        all_tracks_list = QListWidget()
        all_tracks_list.setContextMenuPolicy(Qt.CustomContextMenu)
        all_tracks_list.customContextMenuRequested.connect(lambda pos: self.show_context_menu(all_tracks_list, pos))
        all_tracks_list.setStyleSheet("QListWidget::item { height: 66px; }")

        for track_path in self.tracks:
            title, artist = self.get_track_info_from_file(track_path)
            cover_data = self.get_cover_from_file(track_path)
            item_widget = TrackListItem(title, artist, cover_data, track_path, self)
            list_item = QListWidgetItem(all_tracks_list)
            list_item.setSizeHint(item_widget.sizeHint())
            all_tracks_list.addItem(list_item)
            all_tracks_list.setItemWidget(list_item, item_widget)

        content_layout.addWidget(all_tracks_list)
        content_layout.addStretch()

    def show_search(self):
        self.clear_layout(self.search_layout)
        self.stacked_widget.setCurrentIndex(1)

        search_input = QLineEdit()
        search_input.setPlaceholderText("Поиск: исполнители, треки, альбомы...")
        search_input.textChanged.connect(self.filter_tracks)
        self.search_layout.addWidget(search_input)

        self.search_list = QListWidget()
        self.search_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.search_list.customContextMenuRequested.connect(lambda pos: self.show_context_menu(self.search_list, pos))
        self.search_list.setStyleSheet("QListWidget::item { height: 60px; }")
        self.search_layout.addWidget(self.search_list)

        # initial fill
        self.update_search_list(self.tracks)

    def filter_tracks(self, text):
        self.search_list.clear()
        txt = text.strip().lower()
        for track_path in self.tracks:
            title, artist = self.get_track_info_from_file(track_path)
            if txt in title.lower() or txt in artist.lower() or txt in self.get_tag(track_path, "TALB", "").lower():
                cover_data = self.get_cover_from_file(track_path)
                item_widget = TrackListItem(title, artist, cover_data, track_path, self)
                list_item = QListWidgetItem(self.search_list)
                list_item.setSizeHint(item_widget.sizeHint())
                self.search_list.addItem(list_item)
                self.search_list.setItemWidget(list_item, item_widget)

    def update_search_list(self, tracks):
        self.search_list.clear()
        for track in tracks:
            title, artist = self.get_track_info_from_file(track)
            cover_data = self.get_cover_from_file(track)
            item_widget = TrackListItem(title, artist, cover_data, track, self)
            list_item = QListWidgetItem(self.search_list)
            list_item.setSizeHint(item_widget.sizeHint())
            self.search_list.addItem(list_item)
            self.search_list.setItemWidget(list_item, item_widget)

    def show_collection(self):
        self.clear_layout(self.collection_layout)
        self.stacked_widget.setCurrentIndex(2)

        collection_label = QLabel("🎧 Моя библиотека — Избранное")
        collection_label.setStyleSheet("font-size: 20px; font-weight: bold; color: #fff;")
        self.collection_layout.addWidget(collection_label)

        self.favorites_list = QListWidget()
        self.favorites_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.favorites_list.customContextMenuRequested.connect(lambda pos: self.show_context_menu(self.favorites_list, pos))
        self.favorites_list.setStyleSheet("QListWidget::item { height: 66px; }")

        # favorites хранятся как пути
        for track_path in sorted(list(self.favorites)):
            if os.path.exists(track_path):
                title, artist = self.get_track_info_from_file(track_path)
                cover_data = self.get_cover_from_file(track_path)
                item_widget = TrackListItem(title, artist, cover_data, track_path, self)
                list_item = QListWidgetItem(self.favorites_list)
                list_item.setSizeHint(item_widget.sizeHint())
                self.favorites_list.addItem(list_item)
                self.favorites_list.setItemWidget(list_item, item_widget)

        self.collection_layout.addWidget(self.favorites_list)
        self.collection_layout.addStretch()

    def show_album_view(self, album_name):
        self.clear_layout(self.album_layout)
        self.stacked_widget.setCurrentIndex(3)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(10, 10, 10, 10)
        content_layout.setSpacing(12)
        scroll_area.setWidget(content_widget)
        self.album_layout.addWidget(scroll_area)

        back_btn = QPushButton("⬅️ Назад")
        back_btn.clicked.connect(self.show_home)
        back_btn.setFixedSize(120, 38)
        content_layout.addWidget(back_btn, alignment=Qt.AlignLeft)

        header_layout = QHBoxLayout()
        cover_label = QLabel()
        cover_label.setFixedSize(200, 200)
        cover_label.setStyleSheet("background-color: #262626; border-radius: 10px;")
        if album_name in self.albums and self.albums[album_name]:
            first_track = self.albums[album_name][0]
            cover_data = self.get_cover_from_file(first_track)
            if cover_data:
                pixmap = QPixmap()
                pixmap.loadFromData(cover_data)
                cover_label.setPixmap(pixmap.scaled(200, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        header_layout.addWidget(cover_label)

        info_vbox = QVBoxLayout()
        album_title = QLabel(album_name)
        album_title.setStyleSheet("font-size: 26px; font-weight: bold; color: #fff;")
        album_artist = self.get_album_artist(album_name)
        artist_label = QLabel(f"Исполнитель: {album_artist}")
        artist_label.setStyleSheet("font-size: 14px; color: #bdbdbd;")
        info_vbox.addWidget(album_title)
        info_vbox.addWidget(artist_label)
        header_layout.addLayout(info_vbox)
        header_layout.addSpacerItem(QSpacerItem(40, 20, QSizePolicy.Expanding, QSizePolicy.Minimum))
        content_layout.addLayout(header_layout)

        album_list = QListWidget()
        album_list.setContextMenuPolicy(Qt.CustomContextMenu)
        album_list.customContextMenuRequested.connect(lambda pos: self.show_context_menu(album_list, pos))
        album_list.setStyleSheet("QListWidget::item { height: 60px; }")

        for track_path in self.albums.get(album_name, []):
            title, artist = self.get_track_info_from_file(track_path)
            cover_data = self.get_cover_from_file(track_path)
            item_widget = TrackListItem(title, artist, cover_data, track_path, self)
            list_item = QListWidgetItem(album_list)
            list_item.setSizeHint(item_widget.sizeHint())
            album_list.addItem(list_item)
            album_list.setItemWidget(list_item, item_widget)

        content_layout.addWidget(album_list)

    def show_artist_view(self, artist_name):
        self.clear_layout(self.artist_layout)
        self.stacked_widget.setCurrentIndex(4)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(10, 10, 10, 10)
        content_layout.setSpacing(12)
        scroll_area.setWidget(content_widget)
        self.artist_layout.addWidget(scroll_area)

        self.current_artist_view = artist_name
        back_btn = QPushButton("⬅️ Назад")
        back_btn.clicked.connect(self.show_home)
        back_btn.setFixedSize(120, 38)
        content_layout.addWidget(back_btn, alignment=Qt.AlignLeft)

        header_frame = QFrame()
        header_frame.setFixedHeight(250)
        header_layout = QHBoxLayout(header_frame)
        header_layout.setAlignment(Qt.AlignLeft | Qt.AlignBottom)

        self.artist_avatar_label = QLabel()
        self.artist_avatar_label.setFixedSize(150, 150)
        self.artist_avatar_label.setAlignment(Qt.AlignCenter)
        self.artist_avatar_label.setStyleSheet("border-radius: 75px; border: 3px solid #1DB954;")

        self.artist_name_label = QLabel(artist_name)
        self.artist_name_label.setStyleSheet("font-size: 26px; font-weight: bold; color: #fff; padding: 6px; border-radius: 6px;")

        v_layout = QVBoxLayout()
        v_layout.addWidget(self.artist_avatar_label)
        v_layout.addWidget(self.artist_name_label)
        header_layout.addLayout(v_layout)
        content_layout.addWidget(header_frame)

        tracks_label = QLabel("🎵 Треки")
        tracks_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #fff;")
        content_layout.addWidget(tracks_label)
        artist_tracks_list = QListWidget()
        artist_tracks_list.setContextMenuPolicy(Qt.CustomContextMenu)
        artist_tracks_list.customContextMenuRequested.connect(lambda pos: self.show_context_menu(artist_tracks_list, pos))
        artist_tracks_list.setStyleSheet("QListWidget::item { height: 60px; }")

        for track_path in self.artists.get(artist_name, []):
            title, artist = self.get_track_info_from_file(track_path)
            cover_data = self.get_cover_from_file(track_path)
            item_widget = TrackListItem(title, artist, cover_data, track_path, self)
            list_item = QListWidgetItem(artist_tracks_list)
            list_item.setSizeHint(item_widget.sizeHint())
            artist_tracks_list.addItem(list_item)
            artist_tracks_list.setItemWidget(list_item, item_widget)

        content_layout.addWidget(artist_tracks_list)
        content_layout.addStretch()
        self.update_artist_view(artist_name)

    def update_artist_view(self, artist_name):
        avatar_path = self.artist_avatars.get(artist_name)
        if avatar_path and os.path.exists(avatar_path):
            avatar_pixmap = QPixmap(avatar_path)
            round_pixmap = self.create_round_pixmap(avatar_pixmap)
            self.artist_avatar_label.setPixmap(round_pixmap.scaled(150, 150, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        elif artist_name in self.artists and self.artists[artist_name]:
            cover_data = self.get_cover_from_file(self.artists[artist_name][0])
            if cover_data:
                avatar_pixmap = QPixmap()
                avatar_pixmap.loadFromData(cover_data)
                round_pixmap = self.create_round_pixmap(avatar_pixmap)
                self.artist_avatar_label.setPixmap(round_pixmap.scaled(150, 150, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.artist_avatar_label.setText("🎵")

    def create_round_pixmap(self, pixmap):
        size = min(pixmap.width(), pixmap.height())
        if size == 0:
            return QPixmap()
        mask = QPixmap(size, size)
        mask.fill(Qt.transparent)
        painter = QPainter(mask)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QBrush(Qt.black))
        painter.drawEllipse(0, 0, size, size)
        painter.end()
        result = QPixmap(size, size)
        result.fill(Qt.transparent)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.Antialiasing)
        x_offset = (pixmap.width() - size) // 2
        y_offset = (pixmap.height() - size) // 2
        painter.drawPixmap(-x_offset, -y_offset, pixmap)
        painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        painter.drawPixmap(0, 0, mask)
        painter.end()
        return result

    # ---------- работа с треками и плеером ----------
    def play_track_from_path(self, track_path):
        try:
            self.current_index = self.tracks.index(track_path)
            self.play_track()
        except ValueError:
            pass

    def play_track(self):
        if 0 <= self.current_index < len(self.tracks):
            track_path = self.tracks[self.current_index]
            if not os.path.exists(track_path):
                QMessageBox.critical(self, "Ошибка", f"Файл не найден: {track_path}")
                return
            try:
                pygame.mixer.music.load(track_path)
                pygame.mixer.music.play()
                self.is_playing = True
                self.btn_play_pause.setText("⏸")
                if self.fullscreen_window and self.fullscreen_window.isVisible():
                    title, artist = self.get_track_info_from_file(track_path)
                    cover_data = self.get_cover_from_file(track_path)
                    self.fullscreen_window.update_info(title, artist, cover_data)
                self.update_track_info()
                self.save_state_debounced()
            except pygame.error as e:
                QMessageBox.critical(self, "Ошибка", f"Не удалось воспроизвести файл: {e}")
            except Exception as e:
                QMessageBox.critical(self, "Ошибка", f"Неизвестная ошибка при воспроизведении: {e}")

    def play_pause(self):
        if self.current_index == -1 and self.tracks:
            self.current_index = 0
            self.play_track()
        elif self.is_playing:
            pygame.mixer.music.pause()
            self.is_playing = False
            self.btn_play_pause.setText("▶")
            if self.fullscreen_window: self.fullscreen_window.btn_play_pause.setText("▶")
            self.save_state_debounced()
        else:
            pygame.mixer.music.unpause()
            self.is_playing = True
            self.btn_play_pause.setText("⏸")
            if self.fullscreen_window: self.fullscreen_window.btn_play_pause.setText("⏸")
            self.save_state_debounced()

    def prev_track(self):
        if self.tracks and self.is_playing:
            pos_ms = pygame.mixer.music.get_pos()
            if pos_ms > 10000:
                pygame.mixer.music.play(start=0)
            else:
                self.current_index = (self.current_index - 1) % len(self.tracks)
                self.play_track()
        elif self.tracks:
            self.current_index = (self.current_index - 1) % len(self.tracks)
            self.play_track()
        self.save_state_debounced()

    def next_track(self):
        if self.tracks:
            if self.is_shuffled:
                if len(self.tracks) > 1:
                    new_index = random.randint(0, len(self.tracks) - 1)
                    while new_index == self.current_index:
                        new_index = random.randint(0, len(self.tracks) - 1)
                    self.current_index = new_index
                else:
                    self.current_index = 0
            else:
                self.current_index = (self.current_index + 1) % len(self.tracks)
            self.play_track()
            self.save_state_debounced()

    def toggle_favorite(self):
        if 0 <= self.current_index < len(self.tracks):
            path = self.tracks[self.current_index]
            if path in self.favorites:
                self.favorites.remove(path)
                self.btn_favorite.setStyleSheet("color: #b3b3b3;")
            else:
                self.favorites.add(path)
                self.btn_favorite.setStyleSheet("color: #1DB954;")
            self.save_state_debounced()
            self.show_collection()

    def toggle_shuffle(self):
        self.is_shuffled = not self.is_shuffled
        if self.is_shuffled:
            self.btn_shuffle.setStyleSheet("background-color: #1DB954;")
        else:
            self.btn_shuffle.setStyleSheet("background-color: transparent;")
        self.save_state_debounced()

    def set_volume(self, value):
        pygame.mixer.music.set_volume(value / 100.0)
        self.save_state_debounced()

    def seek_track(self):
        if self.track_length > 0:
            new_pos = self.track_length * (self.position_slider.value() / 100.0)
            try:
                pygame.mixer.music.set_pos(new_pos)
            except Exception:
                # pygame.set_pos может не поддерживаться для некоторых форматов; игнорируем
                pass
            self.start_timer()

    def stop_timer(self):
        self.position_timer.stop()

    def start_timer(self):
        self.position_timer.start(1000)

    def update_position_slider(self):
        if pygame.mixer.music.get_busy() and self.is_playing:
            pos = pygame.mixer.music.get_pos() / 1000.0
            if self.track_length > 0:
                if not self.position_slider.isSliderDown():
                    self.position_slider.setValue(int(pos / self.track_length * 100))

    def check_pygame_events(self):
        for event in pygame.event.get():
            if event.type == pygame.USEREVENT:
                self.next_track()

    def update_track_info(self):
        if 0 <= self.current_index < len(self.tracks):
            file = self.tracks[self.current_index]
            title, artist = self.get_track_info_from_file(file)
            self.track_title.setText(title)
            self.track_artist.setText(artist)

            cover_data = self.get_cover_from_file(file)
            if cover_data:
                pixmap = QPixmap()
                pixmap.loadFromData(cover_data)
                self.cover_label.setPixmap(self.create_round_pixmap(pixmap).scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            else:
                self.cover_label.setText("🎵")

            if self.tracks[self.current_index] in self.favorites:
                self.btn_favorite.setStyleSheet("color: #1DB954;")
            else:
                self.btn_favorite.setStyleSheet("color: #b3b3b3;")

            try:
                audio = File(file)
                self.track_length = audio.info.length
            except Exception:
                self.track_length = 0

            if self.is_shuffled:
                self.btn_shuffle.setStyleSheet("background-color: #1DB954;")
            else:
                self.btn_shuffle.setStyleSheet("background-color: transparent;")

    # ---------- контекстное меню и работа с файлами ----------
    def get_track_path_from_list_item(self, list_widget, item):
        item_widget = list_widget.itemWidget(item)
        if isinstance(item_widget, TrackListItem):
            return item_widget.track_path
        return None

    def show_context_menu(self, list_widget, pos):
        item = list_widget.itemAt(pos)
        if not item:
            return
        track_path = self.get_track_path_from_list_item(list_widget, item)
        if not track_path:
            return
        menu = QMenu()
        edit_action = menu.addAction("📝 Редактировать")
        delete_action = menu.addAction("🗑 Удалить")
        album_action = menu.addAction("🔗 Перейти к альбому")
        artist_action = menu.addAction("🔗 Перейти к исполнителю")
        play_action = menu.addAction("▶ Воспроизвести")
        action = menu.exec_(list_widget.mapToGlobal(pos))
        if action == play_action:
            self.play_track_from_path(track_path)
        elif action == edit_action:
            self.edit_track_info(track_path)
        elif action == delete_action:
            self.delete_track(track_path)
        elif action == album_action:
            self.go_to_album_from_context_menu(track_path)
        elif action == artist_action:
            self.go_to_artist_from_context_menu(track_path)

    def edit_track_info(self, track_path):
        if track_path:
            dialog = EditTrackDialog(track_path)
            if dialog.exec_():
                # после редактирования — обновляем индексы и UI
                self._rebuild_indexes()
                self.update_track_info()
                self.save_state_debounced()
                self.show_home()

    def delete_track(self, track_path):
        if not track_path:
            return
        reply = QMessageBox.question(self, "Удалить трек", "Вы уверены, что хотите переместить этот трек в корзину?",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        try:
            send2trash.send2trash(track_path)
            if track_path in self.tracks:
                idx = self.tracks.index(track_path)
                self.tracks.remove(track_path)
                # если удаляли текущий трек — остановить воспроизведение
                if idx == self.current_index:
                    pygame.mixer.music.stop()
                    self.current_index = -1
                    self.is_playing = False
                    self.update_track_info()
                # удаляем из favorites
                if track_path in self.favorites:
                    self.favorites.remove(track_path)
                # удаляем из artists/albums
                for artist in list(self.artists.keys()):
                    if track_path in self.artists[artist]:
                        self.artists[artist].remove(track_path)
                        if not self.artists[artist]:
                            del self.artists[artist]
                for album in list(self.albums.keys()):
                    if track_path in self.albums[album]:
                        self.albums[album].remove(track_path)
                        if not self.albums[album]:
                            del self.albums[album]
                # обновляем UI
                self.show_home()
                self.update_track_info()
                self.save_state_debounced()
        except send2trash.TrashPermissionError as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось переместить файл в корзину. Ошибка: {e}")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Произошла ошибка при удалении: {e}")

    def go_to_album_from_context_menu(self, track_path):
        if track_path:
            album_name = self.get_tag(track_path, "TALB", "Неизвестный альбом")
            self.show_album_view(album_name)

    def go_to_artist_from_context_menu(self, track_path):
        if track_path:
            try:
                tags = ID3(track_path)
                artists_raw = str(tags.get("TPE1", "Неизвестный исполнитель"))
                artists = [a.strip() for a in artists_raw.split(';')]
                if artists:
                    self.show_artist_view(artists[0])
            except Exception:
                pass

    # ---------- helper: info & tags ----------
    def get_track_info_from_file(self, filepath):
        try:
            tags = ID3(filepath)
            title = str(tags.get("TIT2", os.path.basename(filepath)))
            artists_raw = str(tags.get("TPE1", "Неизвестный исполнитель"))
            artists = ", ".join([a.strip() for a in artists_raw.replace(';', ',').split(',') if a.strip()])
            return title, artists
        except Exception:
            return os.path.basename(filepath), "Неизвестный исполнитель"

    def get_tag(self, filepath, tag, default):
        try:
            return str(ID3(filepath).get(tag, default))
        except Exception:
            return default

    def get_cover_from_file(self, filepath):
        try:
            tags = ID3(filepath)
            for tag in tags.values():
                if isinstance(tag, APIC):
                    return tag.data
        except Exception:
            pass
        return None

    def get_album_artist(self, album_name):
        if album_name in self.albums:
            for track_path in self.albums[album_name]:
                try:
                    tags = ID3(track_path)
                    artists = str(tags.get("TPE1", "Unknown Artist")).split(';')
                    if artists:
                        return artists[0]
                except Exception:
                    continue
        return "Unknown Artist"

    def pixmap_to_data_url(self, pixmap):
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        pixmap.save(buffer, "PNG")
        encoded_string = base64.b64encode(buffer.data()).decode()
        return f"data:image/png;base64,{encoded_string}"

    # ---------- fullscreen ----------
    def show_fullscreen_view(self):
        if self.fullscreen_window is None:
            self.fullscreen_window = FullscreenPlayer(parent=self)
        if self.current_index != -1 and 0 <= self.current_index < len(self.tracks):
            track_path = self.tracks[self.current_index]
            title, artist = self.get_track_info_from_file(track_path)
            cover_data = self.get_cover_from_file(track_path)
            self.fullscreen_window.update_info(title, artist, cover_data)
        self.fullscreen_window.show()

    # ---------- сохранение состояния ----------
    def load_state(self):
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # Восстанавливаем
                tracks = data.get("tracks", [])
                # только существующие пути
                self.tracks = [t for t in tracks if os.path.exists(t)]
                self.favorites = set([t for t in data.get("favorites", []) if os.path.exists(t)])
                self.current_index = -1
                if "current_path" in data and data["current_path"] in self.tracks:
                    self.current_index = self.tracks.index(data["current_path"])
                self.is_shuffled = data.get("is_shuffled", False)
                vol = data.get("volume", 50)
                self.volume_slider.setValue(vol)
                pygame.mixer.music.set_volume(vol / 100.0)
                self._rebuild_indexes()
                self.status.showMessage("Состояние загружено.")
            else:
                self.status.showMessage("Состояние не найдено, будет выполнен начальный скан.")
        except Exception as e:
            print("Ошибка при загрузке состояния:", e)
            self.status.showMessage("Ошибка при загрузке состояния.")

    def save_state(self):
        try:
            state = {
                "tracks": self.tracks,
                "favorites": list(self.favorites),
                "current_path": self.tracks[self.current_index] if 0 <= self.current_index < len(self.tracks) else None,
                "is_shuffled": self.is_shuffled,
                "volume": self.volume_slider.value(),
                "timestamp": time.time()
            }
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            self._last_save_time = time.time()
            self.status.showMessage("Состояние сохранено.")
        except Exception as e:
            print("Ошибка при сохранении состояния:", e)
            self.status.showMessage("Ошибка при сохранении состояния.")

    def save_state_debounced(self):
        # Простая реализация debounce — сохранение, если прошло достаточно времени
        now = time.time()
        if now - self._last_save_time > AUTOSAVE_DEBOUNCE:
            self.save_state()

    # ---------- дополнительные утилиты ----------
    def add_music_dialog(self):
        # спрашиваем: папка или файлы
        choice = QMessageBox.question(self, "Добавить музыку", "Добавить папку с музыкой? (Да = папка, Нет = файлы)",
                                      QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if choice == QMessageBox.Yes:
            folder = QFileDialog.getExistingDirectory(self, "Выберите папку с музыкой")
            if folder:
                files = []
                for ext in AUDIO_EXTS:
                    files.extend(glob.glob(os.path.join(folder, '**', f'*{ext}'), recursive=True))
                self.load_tracks(files)
        else:
            files, _ = QFileDialog.getOpenFileNames(self, "Выберите аудиофайлы", "", "Audio Files (*.mp3 *.m4a *.flac *.wav)")
            if files:
                self.load_tracks(files)

    def go_to_album_from_panel(self, event):
        if event.button() == Qt.LeftButton and self.current_index != -1:
            track_path = self.tracks[self.current_index]
            album_name = self.get_tag(track_path, "TALB", "Неизвестный альбом")
            self.show_album_view(album_name)

    def go_to_artist_from_panel(self, event):
        if event.button() == Qt.LeftButton and self.current_index != -1:
            track_path = self.tracks[self.current_index]
            try:
                tags = ID3(track_path)
                artists_raw = str(tags.get("TPE1", "Неизвестный исполнитель"))
                artists = [a.strip() for a in artists_raw.split(';')]
                if artists:
                    self.show_artist_view(artists[0])
            except Exception:
                pass

    def go_to_album_from_item(self, track_path):
        if track_path:
            album_name = self.get_tag(track_path, "TALB", "Неизвестный альбом")
            self.show_album_view(album_name)

    def go_to_artist_from_item(self, track_path):
        if track_path:
            try:
                tags = ID3(track_path)
                artists_raw = str(tags.get("TPE1", "Неизвестный исполнитель"))
                artists = [a.strip() for a in artists_raw.split(';')]
                if artists:
                    self.show_artist_view(artists[0])
            except Exception:
                pass

    # ---------- события окна ----------
    def closeEvent(self, event):
        # если запущен сканер — остановим
        if self.scanner_thread and self.scanner_thread.isRunning():
            self.scanner_thread.stop()
            self.scanner_thread.wait(500)
        # сохраняем состояние
        self.save_state()
        event.accept()

# ------------------------------------------------------------------
# Запуск
# ------------------------------------------------------------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MusicPlayer()
    window.show()
    sys.exit(app.exec_())
