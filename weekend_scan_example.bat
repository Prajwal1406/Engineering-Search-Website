@echo off
chcp 65001 >nul
setlocal

rem Configure these paths locally. Do not commit private server paths.
if not defined ENGINEERING_SEARCH_SCRIPT set "ENGINEERING_SEARCH_SCRIPT=%~dp0engineering_search_v2.py"
if not defined ENGINEERING_SEARCH_DB_FOLDER set "ENGINEERING_SEARCH_DB_FOLDER=%~dp0databases"

if not exist "%ENGINEERING_SEARCH_DB_FOLDER%" mkdir "%ENGINEERING_SEARCH_DB_FOLDER%"

if not defined SEARCH_ROOT_AREA1 set "SEARCH_ROOT_AREA1=%~dp0sample_documents\Area1"
if not defined SEARCH_ROOT_AREA2 set "SEARCH_ROOT_AREA2=%~dp0sample_documents\Area2"
if not defined SEARCH_ROOT_AREA3 set "SEARCH_ROOT_AREA3=%~dp0sample_documents\Area3"
if not defined SEARCH_ROOT_AREA4 set "SEARCH_ROOT_AREA4=%~dp0sample_documents\Area4"

call :scan "Area1" "%SEARCH_ROOT_AREA1%" "Engineering_Search_V2_Area1.db" || goto scan_error
call :scan "Area2" "%SEARCH_ROOT_AREA2%" "Engineering_Search_V2_Area2.db" || goto scan_error
call :scan "Area3" "%SEARCH_ROOT_AREA3%" "Engineering_Search_V2_Area3.db" || goto scan_error
call :scan "Area4" "%SEARCH_ROOT_AREA4%" "Engineering_Search_V2_Area4.db" || goto scan_error

echo.
echo ==================================================
echo All scans completed successfully.
echo ==================================================
exit /b 0

:scan
echo ==================================================
echo Starting %~1
echo ==================================================
py "%ENGINEERING_SEARCH_SCRIPT%" --scan --root "%~2" --db "%ENGINEERING_SEARCH_DB_FOLDER%\%~3"
exit /b %errorlevel%

:scan_error
echo.
echo ==================================================
echo A scan stopped with an error. Check the output above.
echo Previously completed databases are safe.
echo ==================================================
exit /b 1
