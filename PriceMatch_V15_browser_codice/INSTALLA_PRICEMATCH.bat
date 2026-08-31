@echo off
title Installazione PriceMatch
python -m pip install -r requirements.txt
if errorlevel 1 goto error
python -m playwright install chromium
if errorlevel 1 goto error
echo Installazione completata.
pause
goto end
:error
echo Installazione non riuscita: controlla Python e la connessione Internet.
pause
:end
