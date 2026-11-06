-- =============================================================================
-- 02_seed_targets.sql — Ground Truth / Baseline OSINT dei target (KAN-3)
-- =============================================================================
-- Inserisce o aggiorna (UPSERT) i target validati. Idempotente: rieseguirlo
-- riallinea il DB a questo file senza creare duplicati.
-- Richiede 01_schema.sql (in particolare il vincolo UNIQUE su target_areas.name).
--
-- Esecuzione:
--   PGPASSWORD=... psql -h localhost -p 5433 -U geoint_admin -d geoint_core \
--        -v ON_ERROR_STOP=1 -f database/02_seed_targets.sql
-- =============================================================================

BEGIN;

-- Mischief Reef (Isole Spratly), tile MGRS 50PLS + 50PLR.
--   admiralty_rating  : B2   (fonte solitamente affidabile, informazione probabilmente vera)
--   baseline_area_km2 : 5.58 (terre emerse artificiali, fonte AMTI/CSIS)
--   geom              : AOI WGS84 ~84.9 km², bordo sud corretto a 9.87 il 2026-10-31
--                       (il valore precedente 9.88 tagliava la barriera meridionale)
INSERT INTO public.target_areas (name, admiralty_rating, baseline_area_km2, geom)
VALUES (
    'Mischief Reef',
    'B2',
    5.58,
    ST_GeomFromText(
        'POLYGON((115.48 9.87, 115.58 9.87, 115.58 9.94, 115.48 9.94, 115.48 9.87))',
        4326
    )
)
ON CONFLICT (name) DO UPDATE
SET admiralty_rating  = EXCLUDED.admiralty_rating,
    baseline_area_km2 = EXCLUDED.baseline_area_km2,
    geom              = EXCLUDED.geom;
-- created_at non viene toccato in caso di aggiornamento: resta la data di
-- prima creazione del target.

COMMIT;
