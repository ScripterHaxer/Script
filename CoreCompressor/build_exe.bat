@echo off
rem Builds "Core Compressor.exe" (needs Python 3.8+ from python.org).
rem Double-click this file; the .exe appears in the "dist" folder.
cd /d "%~dp0"
py -m pip install --upgrade pyinstaller || python -m pip install --upgrade pyinstaller
py -m PyInstaller --noconfirm --clean --onefile --console --hide-console hide-early --name "Core Compressor" --icon core_compressor.ico --add-data "core_compressor.ico;." core_compressor.py || python -m PyInstaller --noconfirm --clean --onefile --console --hide-console hide-early --name "Core Compressor" --icon core_compressor.ico --add-data "core_compressor.ico;." core_compressor.py
echo.
echo Done. Your program is in the dist folder.
pause
