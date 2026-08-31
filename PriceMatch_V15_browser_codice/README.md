PRICEMATCH V15 - BROWSER-FIRST, SOLO CODICE

NON serve inserire il nome del prodotto.

Come funziona:
1. apre realmente ciascun ecommerce con Chromium;
2. se esiste una URL già trovata in precedenza, la apre ma la verifica di nuovo;
3. altrimenti entra nella homepage del sito;
4. cerca il campo Cerca/Search;
5. prova il codice e le varianti:
   MO9833
   MO-9833
   MO 9833
6. apre i risultati;
7. accetta una scheda SOLO se il codice è presente nel DOM renderizzato
   o nei campi SKU/MPN/data-sku della pagina;
8. legge nome e prezzi da quella stessa pagina;
9. gli URL candidati passano prima da una cache provvisoria;
10. solo dopo la verifica del codice vengono promossi nella cache definitiva;
11. le cache salvano soltanto URL/nome, MAI i prezzi.

Se la ricerca interna fallisce:
- apre Bing nel browser;
- trova una pagina dello stesso dominio;
- entra nella pagina reale;
- verifica di nuovo il codice prima di accettarla.

Questo privilegia l'accuratezza rispetto alla sola velocità.

Prima installazione:
1. esegui INSTALLA_PRICEMATCH.bat;
2. configura preferibilmente DATABASE_URL, SECRET_KEY e JWT_SECRET_KEY
   come indicato in .env.example;
3. verifica che PostgreSQL sia avviato.

Avvio successivo:
AVVIA_PRICEMATCH_V15.bat

Sicurezza e funzionamento:
- registrazione e login sono obbligatori;
- le API browser richiedono sessione e token CSRF;
- localhost, IP privati e protocolli diversi da HTTP/HTTPS non sono ammessi
  nei siti personalizzati;
- sono consentite al massimo 2 ricerche contemporanee per utente e 10 siti
  per confronto manuale;
- lo storico delle ricerche e salvato nella tabella PostgreSQL `ricerche`.

Test:
python -m unittest -v
