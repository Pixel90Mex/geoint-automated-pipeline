"""
db_utils.py — Accesso condiviso al database PostGIS geoint_core.

Perché un modulo a parte (principio DRY)
    Più script hanno bisogno della stessa informazione: "qual è la geometria
    di questo target?". Se ognuno avesse la sua copia della query e della
    configurazione di connessione, al primo cambiamento (porta, nome tabella,
    SRID...) le copie andrebbero fuori sincrono. Qui c'è un'unica
    implementazione che tutti importano.

Single Source of Truth
    Le coordinate dei target vivono SOLO nella tabella target_areas.
    Il codice contiene la logica, il database contiene i dati.

Configurazione (variabili d'ambiente libpq standard):
    PGHOST (default localhost), PGPORT (default 5433), PGDATABASE (default
    geoint_core), PGUSER (default geoint_admin).

Password (mai nel codice), in ordine di priorità:
    1. PGPASSWORD nell'ambiente, se impostata;
    2. POSTGRES_PASS nel file .env del progetto, lo stesso usato da
       docker-compose.yml: così la password sta in un unico posto;
    3. ~/.pgpass, letto direttamente da libpq.
"""

import json
import os
from pathlib import Path

import psycopg2
from dotenv import dotenv_values

DEFAULT_TARGET = "Mischief Reef"

# Percorso relativo a questo file, non alla directory corrente: gli script
# trovano .env anche se lanciati da un'altra cartella.
ENV_FILE = Path(__file__).resolve().parent / ".env"


def get_connection():
    """Apre una connessione a geoint_core usando le variabili PG* (con default)."""
    # Se PGPASSWORD è impostata, lasciamo password=None: psycopg2 omette il
    # parametro e libpq legge da solo PGPASSWORD (o, in mancanza, ~/.pgpass).
    password = None
    if "PGPASSWORD" not in os.environ:
        password = dotenv_values(ENV_FILE).get("POSTGRES_PASS")

    return psycopg2.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=int(os.environ.get("PGPORT", "5433")),
        dbname=os.environ.get("PGDATABASE", "geoint_core"),
        user=os.environ.get("PGUSER", "geoint_admin"),
        password=password,
    )


def load_target_aoi(target_name=DEFAULT_TARGET):
    """Legge da PostGIS la geometria di un target e ne ricava bbox e GeoJSON.

    Returns:
        (bbox, geojson):
          - bbox: [min_lon, min_lat, max_lon, max_lat] in WGS84 (per rasterio);
          - geojson: dict GeoJSON del poligono (per il parametro 'intersects'
            della ricerca STAC).

    Tutto il calcolo spaziale avviene lato database con PostGIS:
      - ST_Transform(geom, 4326): garantisce gradi WGS84 qualunque sia
        l'SRID con cui la geometria è salvata;
      - ST_XMin/YMin/XMax/YMax: gli estremi del bounding box della geometria;
      - ST_AsGeoJSON: il poligono nel formato accettato dall'API STAC.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Il nome è passato come parametro (%s), mai concatenato nella
            # stringa SQL: psycopg2 lo protegge da SQL injection.
            cur.execute(
                """
                SELECT ST_XMin(g), ST_YMin(g), ST_XMax(g), ST_YMax(g),
                       ST_AsGeoJSON(g)
                FROM (
                    SELECT ST_Transform(geom, 4326) AS g
                    FROM target_areas
                    WHERE name = %s
                ) AS t
                """,
                (target_name,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        raise LookupError(f"Target '{target_name}' non trovato in target_areas.")
    if len(rows) > 1:
        raise LookupError(f"Più record con name='{target_name}': nome ambiguo.")

    xmin, ymin, xmax, ymax, geojson = rows[0]
    return [xmin, ymin, xmax, ymax], json.loads(geojson)
