@echo off
REM Run ONCE on a PC that has internet to create the standalone WeighLabel.exe
REM (afterwards the exe works on any Windows PC with NO internet and NO Python)
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconsole --onefile --name WeighLabel app.py
echo.
echo Done. Your program is: dist\WeighLabel.exe
pause
