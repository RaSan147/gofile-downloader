@echo off
setlocal

if "%PYTHON_BIN%"=="" set PYTHON_BIN=python

%PYTHON_BIN% -m pip install --upgrade pip
if errorlevel 1 goto :error

%PYTHON_BIN% -m pip install .
if errorlevel 1 goto :error

echo Setup complete.
exit /b 0

:error
echo Setup failed.
exit /b 1
