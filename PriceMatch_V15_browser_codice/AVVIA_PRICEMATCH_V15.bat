@echo off
title PriceMatch V15
python -m pip install -r requirements.txt
if errorlevel 1 goto error
python -m playwright install chromium
if errorlevel 1 goto error
start "" http://127.0.0.1:5000/automatico
python app.py
goto end
:error
echo Errore: controlla Python e la connessione Internet.
pause
:end
