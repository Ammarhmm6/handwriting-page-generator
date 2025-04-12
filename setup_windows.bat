@echo off
cd /d "%~dp0"
echo Creating the environment in .venv ...
python -m venv .venv || (echo Python was not found. Install Python 3.9, 3.10 or 3.11 from python.org and tick "Add Python to PATH". & pause & exit /b 1)
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt || (echo Installation failed. & pause & exit /b 1)
echo.
echo Setup complete. Try:
echo    .venv\Scripts\python handwrite.py examples\letter.txt --settings examples\settings_letter.json
pause
