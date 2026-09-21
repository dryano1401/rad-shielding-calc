@echo off
rem ======================================================================
rem  Radiation Shielding Calculator -- Windows launcher
rem
rem  Activates the conda environment, makes sure the package and its web
rem  dependencies are present, then starts the app and opens it in your
rem  browser.
rem
rem  Double-click it, or from a prompt:   run.bat        (port 8000)
rem                                       run.bat 8001   (another port)
rem
rem  To use a differently named environment, either edit the default a few
rem  lines below or set RADSHIELD_ENV before calling this.
rem ======================================================================
setlocal enabledelayedexpansion

if not defined RADSHIELD_ENV set "RADSHIELD_ENV=theradiomics"

set "PORT=%~1"
if not defined PORT set "PORT=8000"

rem Explorer starts a double-clicked .bat in system32, so move to the folder
rem this file lives in -- which is the repository root.
pushd "%~dp0"

echo.
echo  Radiation Shielding Calculator
echo  ------------------------------
echo   repository:  %CD%
echo   environment: %RADSHIELD_ENV%
echo.

rem ----------------------------------------------------------------- conda
rem `conda activate` only works in a shell conda has initialised, and a fresh
rem cmd.exe has not been unless `conda init cmd.exe` was run at some point.
rem Calling conda's own conda.bat is what that init would have arranged, so
rem do it explicitly rather than depending on the machine being set up.
set "CONDA_BAT="

if defined CONDA_EXE (
  for %%I in ("%CONDA_EXE%") do set "CONDA_ROOT=%%~dpI.."
  if exist "!CONDA_ROOT!\condabin\conda.bat" set "CONDA_BAT=!CONDA_ROOT!\condabin\conda.bat"
)

if not defined CONDA_BAT (
  for /f "delims=" %%I in ('where conda.bat 2^>nul') do (
    if not defined CONDA_BAT set "CONDA_BAT=%%I"
  )
)

if not defined CONDA_BAT (
  for %%R in (
    "%USERPROFILE%\anaconda3"
    "%USERPROFILE%\miniconda3"
    "%LOCALAPPDATA%\anaconda3"
    "%LOCALAPPDATA%\miniconda3"
    "%LOCALAPPDATA%\Continuum\anaconda3"
    "C:\ProgramData\Anaconda3"
    "C:\ProgramData\miniconda3"
    "C:\Anaconda3"
    "C:\miniconda3"
  ) do (
    if not defined CONDA_BAT if exist "%%~R\condabin\conda.bat" set "CONDA_BAT=%%~R\condabin\conda.bat"
  )
)

if not defined CONDA_BAT (
  echo  ERROR: could not find conda.
  echo.
  echo  Checked CONDA_EXE, PATH, and the usual Anaconda/Miniconda folders.
  echo  Either open an Anaconda Prompt and run this file from there, or set
  echo  CONDA_EXE to the full path of your conda.exe and try again.
  goto :fail
)

echo  Activating %RADSHIELD_ENV% ...
call "%CONDA_BAT%" activate %RADSHIELD_ENV%
if errorlevel 1 (
  echo.
  echo  ERROR: could not activate "%RADSHIELD_ENV%".
  echo  The environments on this machine are:
  echo.
  call "%CONDA_BAT%" env list
  goto :fail
)

rem --------------------------------------------------------------- package
rem The code lives under src\, so the package has to be on the import path
rem before `python -m radshield.web` can find it. The base dependency list is
rem empty, so this installs nothing into the environment -- it only links the
rem source folder.
python -c "import radshield" 2>nul
if errorlevel 1 (
  echo  Linking the package into the environment ^(installs no dependencies^) ...
  python -m pip install -e . --no-deps --quiet
  if errorlevel 1 goto :fail
)

rem ---------------------------------------------------------- dependencies
rem Checked by import name rather than by pip, because that is what actually
rem has to work, and because a missing one fails late and confusingly:
rem python-multipart only breaks on a floor-plan upload, python-docx only on
rem a report export.
python -c "import importlib.util as u, sys; missing = [p for n, p in [('fastapi','fastapi'), ('uvicorn','uvicorn'), ('multipart','python-multipart'), ('fitz','pymupdf'), ('docx','python-docx')] if u.find_spec(n) is None]; [print('   missing: ' + p) for p in missing]; sys.exit(1 if missing else 0)"
if errorlevel 1 (
  echo.
  echo  Installing the missing packages ...
  python -m pip install -e ".[web]"
  if errorlevel 1 goto :fail
  echo.
)

rem -------------------------------------------------------------- run it
echo.
echo  Starting on http://127.0.0.1:%PORT%/
echo  A browser tab will open. Press Ctrl+C here, or close this window, to stop.
echo.
python -m radshield.web --port %PORT%
if errorlevel 1 (
  rem Not necessarily a failure: answering N to Windows' "Terminate batch
  rem job?" after Ctrl+C lands here too. Say what is actually known rather
  rem than calling a normal shutdown an error.
  echo.
  echo  The server has stopped. If that was not deliberate, the message above
  echo  says why -- the usual cause is port %PORT% already being in use, in
  echo  which case try a different one:   run.bat 8001
  echo.
  pause
)

popd
endlocal
exit /b 0

:fail
echo.
echo  Startup failed -- the message above says why.
echo.
pause
popd
endlocal
exit /b 1
