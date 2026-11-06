-- =============================================================================
-- 01_schema.sql — Schema spaziale del database geoint_core (KAN-2)
-- =============================================================================
-- Ricrea da zero le tabelle della pipeline GEOINT. Estratto dallo schema live
-- (pg_dump --schema-only) e ripulito per essere leggibile e idempotente:
-- si può rieseguire senza errori su un DB dove gli oggetti esistono già.
--
-- Target: PostgreSQL 15 + PostGIS 3.4 (immagine kartoza/postgis:15-3.4).
-- Esecuzione:
--   PGPASSWORD=... psql -h localhost -p 5433 -U geoint_admin -d geoint_core \
--        -v ON_ERROR_STOP=1 -f database/01_schema.sql
-- =============================================================================

BEGIN;

-- Estensione spaziale: tipi geometry/geography, funzioni ST_*, indici GiST.
CREATE EXTENSION IF NOT EXISTS postgis;

-- -----------------------------------------------------------------------------
-- target_areas: i siti da sorvegliare. Unica fonte di verità (SSOT) della
-- geometria AOI letta dagli script tramite db_utils.load_target_aoi().
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.target_areas (
    id                serial                      PRIMARY KEY,   -- crea target_areas_id_seq
    name              varchar(100)                NOT NULL,      -- chiave di business usata dagli script
    admiralty_rating  varchar(2),                                -- rating Admiralty (es. 'B2')
    baseline_area_km2 numeric(10, 2),                            -- area della Target Feature (terre emerse), NON dell'AOI
    geom              geometry(Polygon, 4326),                   -- AOI in WGS84
    created_at        timestamp without time zone DEFAULT CURRENT_TIMESTAMP,

    -- Unicità del nome: db_utils cerca per name, e il seed usa ON CONFLICT (name).
    -- Aggiunto al DB live il 2026-11-06 con:
    --   ALTER TABLE target_areas ADD CONSTRAINT target_areas_name_key UNIQUE (name);
    CONSTRAINT target_areas_name_key UNIQUE (name)
);

-- -----------------------------------------------------------------------------
-- satellite_acquisitions: storico delle osservazioni (una riga per scena).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.satellite_acquisitions (
    id                       serial                 PRIMARY KEY, -- crea satellite_acquisitions_id_seq
    target_id                integer
        CONSTRAINT satellite_acquisitions_target_id_fkey REFERENCES public.target_areas (id),
    acquisition_date         date                   NOT NULL,
    cloud_cover_percentage   numeric(5, 2),                      -- da eo:cloud_cover (media tile)
    calculated_land_area_km2 numeric(10, 2),                     -- output del calcolo NDWI
    raw_stac_metadata        jsonb,                              -- Item STAC originale completo
    snapshot_geom            geometry(Polygon, 4326),            -- ingombro della scena/snapshot
    created_at               timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

-- -----------------------------------------------------------------------------
-- Indici spaziali GiST (R-Tree sui bounding box delle geometrie).
-- -----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS target_areas_geom_idx
    ON public.target_areas USING gist (geom);

CREATE INDEX IF NOT EXISTS satellite_acquisitions_geom_idx
    ON public.satellite_acquisitions USING gist (snapshot_geom);

COMMIT;
