@echo off
title PriceMatch
python -c "import flask, playwright, psycopg2"
if errorlevel 1 goto missing
start "" /B powershell -NoProfile -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:5000/'"
python app.py
goto end
:missing
echo Dipendenze mancanti. Esegui prima INSTALLA_PRICEMATCH.bat.
pause
:end
