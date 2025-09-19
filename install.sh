#!/bin/bash

# Создаем папку, куда скопируем приложение
mkdir -p ~/.local/share/sonora/

# Копируем туда плеер и иконку
cp dist/Sonora_Music_Player ~/.local/share/sonora/sonora-music-player
cp icon.png ~/.local/share/sonora/sonora-music-player-icon.png

# Создаем файл ярлыка
echo "[Desktop Entry]
Version=1.0
Type=Application
Name=Sonora Music Player
Comment=Музыкальный плеер Sonora
Exec=/home/$USER/.local/share/sonora/sonora-music-player
Icon=/home/$USER/.local/share/sonora/sonora-music-player-icon.png
Terminal=false
Categories=AudioVideo;Player;" > ~/.local/share/applications/sonora-music-player.desktop

echo "Установка завершена! Sonora Music Player теперь в меню приложений."
