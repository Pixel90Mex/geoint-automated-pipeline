"""
stac_mosaic_engine.py — Mosaico "in streaming" di scene Sentinel-2 da COG remoti.

PROBLEMA
    Mischief Reef cade sul confine tra due tile MGRS di Sentinel-2: 50PLS e 50PLR.
    Nessuna delle due, da sola, copre per intero l'area di interesse (AOI).
    Dobbiamo quindi "cucire" (mosaicare) le due tile e ritagliare solo il reef.

PERCHÉ NON SCARICARE TUTTO?
    Ogni tile Sentinel-2 copre circa 110 x 110 km. L'asset 'visual' (RGB a 10 m)
    è un raster di ~10980 x 10980 pixel x 3 bande: centinaia di MB per tile.
    Il nostro reef è grande circa 11 x 7 km: meno dell'1% della tile.
    Scaricare tutto per tenerne l'1% è uno spreco di banda, tempo e disco.

LA SOLUZIONE: COG (Cloud Optimized GeoTIFF)
    Un COG è un GeoTIFF normale, ma organizzato internamente in modo furbo:
      1. Header all'inizio del file: contiene la georeferenziazione e
         l'"indice" dei blocchi (offset in byte di ogni blocco nel file).
      2. Tiling interno: l'immagine è divisa in blocchi (es. 512x512 pixel)
         invece che in righe intere. Ogni blocco è compresso a parte.
      3. Overview (piramidi): versioni a risoluzione ridotta, utili per
         anteprime veloci.
    Grazie a questa struttura un client può leggere SOLO i blocchi che gli
    servono usando richieste HTTP "Range" (es. "Range: bytes=123456-234567"),
    senza mai scaricare il file intero.

COME RASTERIO GESTISCE I COG IN STREAMING
    rasterio è un wrapper Python attorno a GDAL. Quando gli passi un URL https://
    GDAL usa il suo file system virtuale /vsicurl/:
      - rasterio.open(url) -> GDAL fa 1-2 richieste Range per leggere l'header
        (pochi KB). A questo punto conosce CRS, transform, dimensioni e indice
        dei blocchi, ma NON ha ancora letto nessun pixel.
      - dataset.read(window=...) -> GDAL calcola quali blocchi interni
        intersecano la finestra richiesta e scarica solo quelli (unendo le
        richieste di blocchi contigui in un'unica Range request).
    È lo stesso principio per cui guardi un video in streaming senza scaricarlo.

PERCHÉ rasterio.merge?
    rasterio.merge.merge() prende N dataset e produce un unico array:
      - calcola una griglia di output comune (stesso CRS, stessa risoluzione);
      - per ogni dataset legge solo la porzione che cade dentro i 'bounds'
        richiesti (quindi, con i COG, solo i blocchi necessari);
      - dove le tile si sovrappongono applica una regola (default 'first':
        vince il primo dataset con un pixel valido, cioè non-nodata).
    Passandogli i bounds del reef otteniamo in un colpo solo mosaico + ritaglio,
    scaricando qualche MB invece di centinaia.

ATTENZIONE AI SISTEMI DI RIFERIMENTO (qui entra pyproj)
    Il nostro bbox è in gradi WGS84 (EPSG:4326), ma i raster Sentinel-2 sono
    in UTM (metri). 50PLS e 50PLR sono entrambe nella zona UTM 50N
    (EPSG:32650). merge() vuole i bounds nel CRS dei raster, quindi
    convertiamo il bbox da gradi a metri UTM con pyproj.

SINGLE SOURCE OF TRUTH: LE COORDINATE ARRIVANO DAL DATABASE
    In origine il bbox era scritto a mano in questo file. Era un anti-pattern:
    la stessa informazione viveva in più posti (DB, script, altri script) e
    poteva andare fuori sincrono, come è successo quando abbiamo corretto il
    bordo sud del reef nel DB ma gli script continuavano a usare il vecchio.
    Ora questo modulo contiene solo la LOGICA (cerca, mosaica, ritaglia) e
    riceve i DATI (quale target, con quale geometria) dalla tabella
    target_areas di PostGIS. Per correggere o aggiungere un target basta un
    UPDATE/INSERT nel DB, senza toccare il codice.

    L'accesso al DB (connessione + query) sta in db_utils.py, condiviso con
    stac_explorer.py: vedi lì la configurazione (variabili PG*, password da .env).

Esecuzione:
    .venv/bin/python stac_mosaic_engine.py ["Nome Target"]
"""

import sys
from collections import defaultdict

import rasterio
from pyproj import Transformer
from pystac_client import Client
from rasterio.merge import merge
from rasterio.vrt import WarpedVRT

from db_utils import DEFAULT_TARGET, load_target_aoi

STAC_API_URL = "https://earth-search.aws.element84.com/v1"
COLLECTION = "sentinel-2-l2a"
DATE_RANGE = "2026-09-01/2026-09-30"
OUTPUT_PATH = "mischief_reef_snapshot.tif"

# NB: qui NON c'è più nessuna coordinata. La geometria del target è un DATO
# e vive solo nel database (tabella target_areas). Vedi db_utils.load_target_aoi().

# Opzioni di GDAL che ottimizzano la lettura di COG via HTTP.
GDAL_COG_OPTIONS = {
    # Quando apre un file remoto, di default GDAL prova a listare la "cartella"
    # per cercare file accessori (.ovr, .aux.xml...). Su S3/HTTP sono richieste
    # inutili e lente: le disattiviamo.
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    # Accetta solo questi file su /vsicurl/ (evita richieste per i sidecar).
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff",
    # Unisce le Range request di blocchi adiacenti: meno round-trip HTTP.
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    # Il bucket sentinel-cogs è pubblico: nessuna credenziale AWS.
    "AWS_NO_SIGN_REQUEST": "YES",
}


def bbox_to_crs(bbox_wgs84, dst_crs):
    """Converte un bbox [min_lon, min_lat, max_lon, max_lat] da WGS84 a dst_crs.

    Usiamo transform_bounds (e non solo i 2 angoli) perché un rettangolo in
    gradi, proiettato in UTM, non è più esattamente un rettangolo: i bordi si
    incurvano leggermente. transform_bounds "densifica" i bordi (campiona molti
    punti lungo il perimetro) e restituisce il rettangolo che li contiene tutti.
    always_xy=True forza l'ordine (lon, lat) / (x, y), evitando la classica
    trappola dell'ordine (lat, lon) di EPSG:4326.
    """
    transformer = Transformer.from_crs("EPSG:4326", dst_crs, always_xy=True)
    return transformer.transform_bounds(*bbox_wgs84, densify_pts=21)


def build_mosaic_snapshot(items, bbox_wgs84, output_path=OUTPUT_PATH, asset_key="visual"):
    """Crea un mosaico ritagliato sul bbox a partire da Item STAC della stessa data.

    Args:
        items: lista di pystac.Item (es. le tile 50PLS e 50PLR dello stesso giorno).
        bbox_wgs84: area di interesse in gradi [min_lon, min_lat, max_lon, max_lat].
        output_path: GeoTIFF di output.
        asset_key: asset da mosaicare ('visual' = RGB True Color a 10 m, uint8).

    Returns:
        Il percorso del file scritto.
    """
    if not items:
        raise ValueError("Lista di Item vuota: niente da mosaicare.")

    # 1) Raccogliamo gli URL dei COG. Sono semplici URL https: nessun download.
    hrefs = [item.assets[asset_key].href for item in items if asset_key in item.assets]
    if not hrefs:
        raise ValueError(f"Nessun Item contiene l'asset '{asset_key}'.")
    print(f"Mosaico di {len(hrefs)} COG:")
    for h in hrefs:
        print(f"  - {h}")

    # rasterio.Env applica le opzioni GDAL a tutto ciò che accade nel blocco.
    with rasterio.Env(**GDAL_COG_OPTIONS):
        # 2) Apriamo i dataset remoti. Qui GDAL legge SOLO l'header di ogni COG.
        sources = [rasterio.open(h) for h in hrefs]
        try:
            # 3) Scegliamo il CRS di riferimento: quello della prima tile.
            dst_crs = sources[0].crs

            # merge() richiede che tutti i dataset abbiano lo stesso CRS.
            # Per Mischief Reef è sempre così (zona UTM 50N), ma se l'AOI
            # cadesse a cavallo di due zone UTM (es. 50N e 51N) dovremmo
            # riproiettare. WarpedVRT lo fa "al volo" e in modo lazy: è una
            # vista riproiettata del COG remoto, che continua a scaricare solo
            # i blocchi necessari quando merge() la legge.
            datasets = [
                src if src.crs == dst_crs else WarpedVRT(src, crs=dst_crs)
                for src in sources
            ]

            # 4) Convertiamo il bbox del reef da gradi a metri UTM.
            bounds = bbox_to_crs(bbox_wgs84, dst_crs.to_string())
            print(f"CRS raster: {dst_crs}  |  bounds AOI: "
                  f"{tuple(round(b, 1) for b in bounds)}")

            # 5) Il cuore: mosaico + ritaglio in una sola operazione.
            #    - bounds: merge legge da ogni COG solo la finestra che
            #      interseca l'AOI (-> poche Range request per pochi blocchi).
            #    - nodata=0: nell'asset 'visual' lo 0 indica "fuori orbita".
            #      È fondamentale: senza nodata, i pixel neri del bordo di una
            #      tile coprirebbero i pixel validi dell'altra.
            #    - method='first': nelle zone di sovrapposizione vince il primo
            #      dataset con un pixel valido.
            mosaic, transform = merge(datasets, bounds=bounds, nodata=0, method="first")

            # 6) Prepariamo il profilo del GeoTIFF di output partendo da quello
            #    della prima sorgente e aggiornando dimensioni e georeferenziazione.
            profile = sources[0].profile.copy()
            profile.update(
                driver="GTiff",
                height=mosaic.shape[1],
                width=mosaic.shape[2],
                count=mosaic.shape[0],
                transform=transform,  # nuova origine: l'angolo in alto a sx dell'AOI
                crs=dst_crs,
                nodata=0,
                compress="deflate",
                tiled=True,
                blockxsize=256,
                blockysize=256,
                photometric="RGB",
            )
        finally:
            for src in sources:
                src.close()

    # 7) Scriviamo il "pezzetto" in locale: questo è l'unico file su disco.
    with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(mosaic)

    print(f"Salvato {output_path}: {mosaic.shape[2]}x{mosaic.shape[1]} px, "
          f"{mosaic.shape[0]} bande, {mosaic.dtype}")
    return output_path


def find_items_by_date(aoi_geojson):
    """Cerca le scene che intersecano l'AOI e le raggruppa per data (YYYY-MM-DD).

    Le tile 50PLS e 50PLR vengono acquisite nello stesso passaggio del
    satellite, a pochi secondi di distanza: raggrupparle per giorno significa
    raggruppare le tile da cucire insieme.

    Usiamo 'intersects' con il poligono vero invece di 'bbox': oggi il
    poligono è un rettangolo e il risultato è identico, ma se nel DB un
    target avesse una forma irregolare, la ricerca resterebbe precisa.
    """
    catalog = Client.open(STAC_API_URL)
    search = catalog.search(
        collections=[COLLECTION],
        intersects=aoi_geojson,
        datetime=DATE_RANGE,
        sortby=[{"field": "properties.datetime", "direction": "asc"}],
    )
    by_date = defaultdict(list)
    for item in search.items():
        by_date[item.datetime.strftime("%Y-%m-%d")].append(item)
    return by_date


def main() -> None:
    # 1) DATI: chiediamo al database "dove" guardare.
    target_name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TARGET
    bbox, aoi_geojson = load_target_aoi(target_name)
    print(f"Target '{target_name}' da PostGIS -> bbox {bbox}\n")

    # 2) LOGICA: da qui in poi il codice è identico per qualunque target.
    by_date = find_items_by_date(aoi_geojson)
    if not by_date:
        print("Nessuna scena trovata.")
        return

    print("Date disponibili (tile | nuvolosità media):")
    for day, items in by_date.items():
        tiles = ", ".join(i.id.split("_")[1] for i in items)
        cloud = sum(i.properties.get("eo:cloud_cover", 100) for i in items) / len(items)
        print(f"  {day}: {tiles}  |  {cloud:.1f}%")

    # Scegliamo il giorno che ha entrambe le tile e la nuvolosità media più bassa.
    # NB: eo:cloud_cover è calcolato sull'intera tile, non sul reef: è solo
    # un'indicazione di massima.
    candidates = {d: its for d, its in by_date.items() if len(its) >= 2} or by_date
    best_day = min(
        candidates,
        key=lambda d: sum(i.properties.get("eo:cloud_cover", 100) for i in candidates[d])
        / len(candidates[d]),
    )
    print(f"\nData scelta: {best_day}\n")
    build_mosaic_snapshot(candidates[best_day], bbox)


if __name__ == "__main__":
    main()
