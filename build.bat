@echo off
cd /d "%~dp0"
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name CountdownReminder ^
  --icon app.ico ^
  --version-file version.txt ^
  --add-data "app.ico;." ^
  --noupx ^
  reminder_improved.py
pause
