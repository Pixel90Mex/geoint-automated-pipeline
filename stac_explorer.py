"""
stac_explorer.py — Script didattico per esplorare un catalogo STAC.

Cos'è STAC?
    STAC (SpatioTemporal Asset Catalog) è uno standard aperto per descrivere
    dati geospaziali (immagini satellitari, DEM, ecc.) in modo uniforme.
    Un'API STAC espone:
      - Collection: insiemi omogenei di dati (es. "sentinel-2-l2a")
      - Item: una singola acquisizione (una "scena"), con geometria, data e metadati
      - Asset: i file veri e propri collegati all'Item (bande, thumbnail, metadati...)

Cosa fa questo script:
    1. Si connette all'API STAC pubblica di Earth Search (Element 84, su AWS).
    2. Legge da PostGIS (tabella target_areas) la geometria del target,
       di default Mischief Reef (Isole Spratly).
    3. Cerca scene Sentinel-2 Level-2A che intersecano quella geometria nel
       mese di settembre 2026.
    4. Stampa per ogni scena (max 5): data di acquisizione, copertura nuvolosa
       e URL della thumbnail.

Le coordinate non sono nel codice: il database è l'unica fonte di verità
(Single Source of Truth). L'accesso al DB è nel modulo condiviso db_utils.py,
lo stesso usato da stac_mosaic_engine.py (principio DRY).

Requisiti:
    pip install pystac-client psycopg2-binary python-dotenv

Esecuzione:
    .venv/bin/python stac_explorer.py ["Nome Target"]
"""

import sys

from pystac_client import Client

from db_utils import DEFAULT_TARGET, load_target_aoi

# --- Parametri di ricerca -----------------------------------------------------

# Endpoint pubblico dell'API STAC di Earth Search (nessuna autenticazione richiesta)
STAC_API_URL = "https://earth-search.aws.element84.com/v1"

# Collection Sentinel-2 Level-2A: riflettanza "Bottom Of Atmosphere",
# cioè già corretta per gli effetti atmosferici.
COLLECTION = "sentinel-2-l2a"

# NB: nessuna coordinata qui. L'area di interesse (AOI) arriva dal database.

# Intervallo temporale in formato ISO 8601 "inizio/fine": tutto settembre 2026
DATE_RANGE = "2026-09-01/2026-09-30"

# Numero massimo di Item da restituire
MAX_ITEMS = 5


def main() -> None:
    # 0) Dati: chiediamo a PostGIS la geometria del target.
    #    bbox è nel formato [min_lon, min_lat, max_lon, max_lat] (WGS84:
    #    prima la longitudine, poi la latitudine!); aoi_geojson è il poligono.
    target_name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TARGET
    bbox, aoi_geojson = load_target_aoi(target_name)
    print(f"Target '{target_name}' da PostGIS -> bbox {bbox}")

    # 1) Apertura del catalogo: pystac-client legge la "landing page" dell'API
    #    e scopre automaticamente quali funzionalità (es. /search) supporta.
    print(f"Connessione a {STAC_API_URL} ...")
    catalog = Client.open(STAC_API_URL)
    print(f"Catalogo: {catalog.title}\n")

    # 2) Costruzione della ricerca. Nessuna richiesta parte ancora:
    #    l'oggetto ItemSearch è "lazy" e interroga l'API solo quando iteriamo.
    search = catalog.search(
        collections=[COLLECTION],
        intersects=aoi_geojson,    # filtro spaziale: Item che INTERSECANO il poligono
        datetime=DATE_RANGE,       # filtro temporale
        max_items=MAX_ITEMS,       # limite totale di Item restituiti
        sortby=[{"field": "properties.datetime", "direction": "asc"}],  # ordine cronologico
    )

    # 3) Iterazione sui risultati. items() gestisce in automatico la paginazione.
    items = list(search.items())

    if not items:
        print("Nessuna scena trovata per i criteri indicati.")
        return

    print(f"Trovate {len(items)} scene (max {MAX_ITEMS}):\n")
    print("-" * 80)

    for i, item in enumerate(items, start=1):
        # item.datetime è un oggetto datetime Python (UTC) già parsato da pystac
        acquired = item.datetime.strftime("%Y-%m-%d %H:%M:%S UTC") if item.datetime else "n/d"

        # La copertura nuvolosa è definita dall'estensione STAC "eo" (Electro-Optical)
        cloud = item.properties.get("eo:cloud_cover")
        cloud_str = f"{cloud:.2f}%" if cloud is not None else "n/d"

        # Gli asset sono un dizionario {chiave: Asset}; su Earth Search la
        # preview si chiama "thumbnail" e ha un attributo href (l'URL del file).
        thumb = item.assets.get("thumbnail")
        thumb_url = thumb.href if thumb else "n/d"

        print(f"[{i}] ID scena        : {item.id}")
        print(f"    Data acquisizione : {acquired}")
        print(f"    Copertura nuvolosa: {cloud_str}")
        print(f"    Thumbnail         : {thumb_url}")
        print("-" * 80)


if __name__ == "__main__":
    main()
