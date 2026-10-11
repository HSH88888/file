@echo off
chcp 65001 > nul
cd /d "%~dp0"
where python > nul 2>&1
if errorlevel 1 (
  echo Python 이 설치되어 있지 않습니다. https://www.python.org/downloads/ 에서 설치하세요.
  echo 설치할 때 "Add python.exe to PATH" 를 꼭 체크하세요.
  pause
  exit /b
)
python -m pip install --quiet --disable-pip-version-check Pillow pillow-heif send2trash
python photo_dedup.py %*
pause
