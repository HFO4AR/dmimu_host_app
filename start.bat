@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 exit /b 1
)
if not exist ".venv\.dmimu-ready" goto install
fc /b requirements.txt .venv\.dmimu-ready >nul 2>&1
if errorlevel 1 goto install
goto model
:install
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
copy /y requirements.txt .venv\.dmimu-ready >nul
:model
if "%DMIMU_SKIP_MODEL%"=="1" (
  echo Official CAD preparation skipped ^(DMIMU_SKIP_MODEL=1^).
  goto run
)
.venv\Scripts\python.exe scripts\prepare_model.py --install
if errorlevel 1 echo Official CAD preparation failed; starting without a model. Retry: .venv\Scripts\python.exe scripts\prepare_model.py --install
:run
.venv\Scripts\python.exe app.py %*
