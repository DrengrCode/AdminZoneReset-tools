@echo off
REM Builds a double-click app: dist\AreaReset.exe  (run this once on your PC)
python -m pip install --upgrade pip
python -m pip install paramiko pyinstaller
python -m PyInstaller --onefile --windowed --name AreaReset area_reset_gui.py
echo.
echo Done. Your app is in the "dist" folder: AreaReset.exe
pause
