import psycopg2

#connesione al database
def get_connection():
    return psycopg2.connect(
        host="localhost",
        port="5432",
        dbname="postgres",
        user="postgres",
        password="postgres"
    )

#registrazione dei prodotti
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
                (codice, titolo)
            )

            prodotto_id = cursor.fetchone()[0]

        conn.commit()
        return prodotto_id

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

#registrazione del match
def salva_match(codice_prodotto, nome_sito, prezzo, score, url_prodotto):
    conn = get_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO risultati_match (
                    prodotto_id,
                    sito_id,
                    prezzo,
                    score_affidabilita,
                    url_prodotto
                )
                SELECT
                    p.id,
                    s.id,
                    %s,
                    %s,
                    %s
                FROM prodotti AS p
                CROSS JOIN siti AS s
                WHERE p.codice = %s
                  AND s.nome = %s
                RETURNING id;
                """,
                (
                    prezzo,
                    score,
                    url_prodotto,
                    codice_prodotto,
                    nome_sito
                )
            )

            risultato = cursor.fetchone()

            if risultato is None:
                raise ValueError(
                    f"Prodotto o sito non trovato: "
                    f"codice={codice_prodotto!r}, sito={nome_sito!r}"
                )

            match_id = risultato[0]

        conn.commit()
        return match_id

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()