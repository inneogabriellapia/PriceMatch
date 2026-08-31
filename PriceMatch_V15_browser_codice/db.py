import json
import os

import psycopg2


_schema_ready = False


def get_connection():
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return psycopg2.connect(database_url)

    # Compatibilita con l'installazione locale esistente. In produzione va
    # sempre impostata DATABASE_URL e non vanno usate credenziali predefinite.
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ.get("PGDATABASE", "postgres"),
        user=os.environ.get("PGUSER", "postgres"),
        password=os.environ.get("PGPASSWORD", "postgres"),
    )


def ensure_schema():
    """Crea in modo idempotente lo schema minimo richiesto dall'applicazione."""
    global _schema_ready
    if _schema_ready:
        return
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS utenti (
                    id SERIAL PRIMARY KEY,
                    email VARCHAR(120) NOT NULL,
                    password_hash VARCHAR(256) NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS utenti_email_unique ON utenti(email);

                CREATE TABLE IF NOT EXISTS prodotti (
                    id SERIAL PRIMARY KEY,
                    codice VARCHAR(100) NOT NULL,
                    titolo TEXT NOT NULL,
                    creato_il TIMESTAMP DEFAULT NOW()
                );
                CREATE UNIQUE INDEX IF NOT EXISTS prodotti_codice_unique ON prodotti(codice);

                CREATE TABLE IF NOT EXISTS siti (
                    id SERIAL PRIMARY KEY,
                    nome VARCHAR(120) NOT NULL,
                    url_base TEXT,
                    attivo BOOLEAN DEFAULT TRUE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS siti_nome_unique ON siti(nome);

                CREATE TABLE IF NOT EXISTS risultati_match (
                    id SERIAL PRIMARY KEY,
                    prodotto_id INTEGER REFERENCES prodotti(id) ON DELETE CASCADE,
                    sito_id INTEGER REFERENCES siti(id) ON DELETE CASCADE,
                    prezzo NUMERIC,
                    score_affidabilita INTEGER,
                    url_prodotto TEXT,
                    data_rilevazione TIMESTAMP DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS risultati_match_prodotto_idx
                    ON risultati_match(prodotto_id);

                CREATE TABLE IF NOT EXISTS ricerche (
                    id BIGSERIAL PRIMARY KEY,
                    utente_id INTEGER NOT NULL REFERENCES utenti(id) ON DELETE CASCADE,
                    codice VARCHAR(100) NOT NULL,
                    risultato JSONB NOT NULL,
                    creata_il TIMESTAMP NOT NULL DEFAULT NOW(),
                    aggiornata_il TIMESTAMP NOT NULL DEFAULT NOW(),
                    UNIQUE (utente_id, codice)
                );
                """
            )
        conn.commit()
        _schema_ready = True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def salva_prodotto(codice, titolo):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO prodotti (codice, titolo)
                VALUES (%s, %s)
                ON CONFLICT (codice)
                DO UPDATE SET titolo = EXCLUDED.titolo
                RETURNING id;
                """,
                (codice, titolo),
            )
            prodotto_id = cursor.fetchone()[0]
        conn.commit()
        return prodotto_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def salva_match(codice_prodotto, nome_sito, prezzo, score, url_prodotto, url_base=None):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO siti (nome, url_base, attivo)
                VALUES (%s, %s, TRUE)
                ON CONFLICT (nome)
                DO UPDATE SET
                    url_base = COALESCE(EXCLUDED.url_base, siti.url_base),
                    attivo = TRUE;
                """,
                (nome_sito, url_base),
            )
            cursor.execute(
                """
                DELETE FROM risultati_match
                WHERE prodotto_id = (SELECT id FROM prodotti WHERE codice = %s)
                  AND sito_id = (SELECT id FROM siti WHERE nome = %s);
                """,
                (codice_prodotto, nome_sito),
            )
            cursor.execute(
                """
                INSERT INTO risultati_match (
                    prodotto_id, sito_id, prezzo, score_affidabilita, url_prodotto
                )
                SELECT p.id, s.id, %s, %s, %s
                FROM prodotti AS p
                CROSS JOIN siti AS s
                WHERE p.codice = %s AND s.nome = %s
                RETURNING id;
                """,
                (prezzo, score, url_prodotto, codice_prodotto, nome_sito),
            )
            risultato = cursor.fetchone()
            if risultato is None:
                raise ValueError(
                    f"Prodotto o sito non trovato: codice={codice_prodotto!r}, "
                    f"sito={nome_sito!r}"
                )
            match_id = risultato[0]
        conn.commit()
        return match_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def salva_ricerca(utente_id, codice, risultato):
    ensure_schema()
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ricerche (utente_id, codice, risultato)
                VALUES (%s, %s, %s::jsonb)
                ON CONFLICT (utente_id, codice)
                DO UPDATE SET risultato = EXCLUDED.risultato, aggiornata_il = NOW();
                """,
                (utente_id, codice.upper(), json.dumps(risultato, ensure_ascii=False)),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_ricerca_precedente(utente_id, codice):
    ensure_schema()
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT risultato
                FROM ricerche
                WHERE utente_id = %s AND codice = %s;
                """,
                (utente_id, codice.upper()),
            )
            row = cursor.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def get_match_precedenti(codice_prodotto):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT s.nome, r.prezzo, r.score_affidabilita, r.url_prodotto
                FROM risultati_match r
                JOIN prodotti p ON r.prodotto_id = p.id
                JOIN siti s ON r.sito_id = s.id
                WHERE p.codice = %s;
                """,
                (codice_prodotto,),
            )
            return cursor.fetchall()
    finally:
        conn.close()
