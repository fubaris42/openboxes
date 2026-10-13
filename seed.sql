-- OpenBoxes seed data: 3 warehouses, 8 categories, 39 products (seragam is one product per size).
-- Safe to re-run: every row has a fixed id and is inserted with INSERT IGNORE.
-- Apply with:  uv run obx.py seed

SET NAMES utf8mb3;
SET @now := NOW();
SET @admin := (SELECT id FROM `user` WHERE username = 'admin' LIMIT 1);
SET @org := (SELECT id FROM party WHERE code = 'MO' LIMIT 1);
SET @depot := (SELECT id FROM location_type WHERE location_type_code = 'DEPOT' ORDER BY id LIMIT 1);

-- Warehouses -----------------------------------------------------------------
INSERT IGNORE INTO inventory (id, version, date_created, last_updated) VALUES
  ('seed-inv-bandung', 0, @now, @now),
  ('seed-inv-cibubur', 0, @now, @now),
  ('seed-inv-bintaro', 0, @now, @now);

INSERT IGNORE INTO location
  (id, version, date_created, last_updated, name, location_number, inventory_id, manager_id,
   active, location_type_id, organization_id, bg_color, fg_color, sort_order)
VALUES
  ('seed-wh-bandung', 0, @now, @now, 'Bandung', 'BDG', 'seed-inv-bandung', @admin, 1, @depot, @org, 'FFFFFF', '000000', 1),
  ('seed-wh-cibubur', 0, @now, @now, 'Cibubur', 'CBB', 'seed-inv-cibubur', @admin, 1, @depot, @org, 'FFFFFF', '000000', 2),
  ('seed-wh-bintaro', 0, @now, @now, 'Bintaro', 'BTR', 'seed-inv-bintaro', @admin, 1, @depot, @org, 'FFFFFF', '000000', 3);

-- Categories -----------------------------------------------------------------
-- reuse the app's root category if it has one (flagged, or simply the parentless one)
SET @root := (SELECT id FROM category WHERE is_root = 1 OR parent_category_id IS NULL
              ORDER BY is_root DESC, date_created LIMIT 1);
INSERT INTO category (id, version, date_created, last_updated, name, sort_order, is_root)
SELECT 'seed-cat-root', 0, @now, @now, 'ROOT', 0, 1 FROM DUAL WHERE @root IS NULL;
SET @root := COALESCE(@root, 'seed-cat-root');

INSERT IGNORE INTO category (id, version, date_created, last_updated, name, parent_category_id, sort_order, is_root) VALUES
  ('seed-cat-koper',   0, @now, @now, 'Koper',           @root, 1, 0),
  ('seed-cat-tas',     0, @now, @now, 'Tas',             @root, 2, 0),
  ('seed-cat-buku',    0, @now, @now, 'Buku',            @root, 3, 0),
  ('seed-cat-syal',    0, @now, @now, 'Syal',            @root, 4, 0),
  ('seed-cat-batik',   0, @now, @now, 'Batik',           @root, 5, 0),
  ('seed-cat-seragam', 0, @now, @now, 'Seragam',         @root, 6, 0),
  ('seed-cat-busana',  0, @now, @now, 'Busana Muslimah', @root, 7, 0),
  ('seed-cat-ihrom',   0, @now, @now, 'Ihrom',           @root, 8, 0);

-- Products -------------------------------------------------------------------
-- Staged in a temp table so the shared columns are written once.
-- OpenBoxes has no size variants, so every size is its own product. A "product family"
-- ties the sizes of one garment together.
INSERT IGNORE INTO product_group (id, version, date_created, last_updated, name, category_id) VALUES
  ('seed-fam-seragam-p', 0, @now, @now, 'Seragam okt 2026 P', 'seed-cat-seragam'),
  ('seed-fam-seragam-l', 0, @now, @now, 'Seragam okt 2026 L', 'seed-cat-seragam');

CREATE TEMPORARY TABLE seed_product (
  id char(38) PRIMARY KEY, product_code varchar(255), name varchar(255), category_id char(38),
  family_id char(38)
) DEFAULT CHARSET = utf8mb3;

-- Seragam: one product per cut (P, L) and size, e.g. SRG-2610-P-XS "Seragam okt 2026 P (XS)"
INSERT INTO seed_product (id, product_code, name, category_id, family_id)
SELECT CONCAT('seed-prd-srg-', LOWER(c.cut), '-', LOWER(s.size)),
       CONCAT('SRG-2610-', c.cut, '-', s.size),
       CONCAT('Seragam okt 2026 ', c.cut, ' (', s.size, ')'),
       'seed-cat-seragam', c.family_id
FROM (SELECT 'P' AS cut, 'seed-fam-seragam-p' AS family_id
      UNION ALL SELECT 'L', 'seed-fam-seragam-l') c
CROSS JOIN (SELECT 'XS' AS size UNION ALL SELECT 'S' UNION ALL SELECT 'M' UNION ALL SELECT 'L'
            UNION ALL SELECT 'XL' UNION ALL SELECT '2XL' UNION ALL SELECT '3XL'
            UNION ALL SELECT '4XL' UNION ALL SELECT '5XL') s;

INSERT INTO seed_product (id, product_code, name, category_id) VALUES
  ('seed-prd-01', 'KPR-B28-2503',   'Koper bagasi 28" mar 2025',        'seed-cat-koper'),
  ('seed-prd-02', 'KPR-C16-2503',   'Koper cabin 16" mar 2025',         'seed-cat-koper'),
  ('seed-prd-03', 'KPR-B26-2511',   'Koper bagasi 26" nov 2025',        'seed-cat-koper'),
  ('seed-prd-04', 'KPR-C16-2511',   'Koper cabin 16" nov 2025',         'seed-cat-koper'),
  ('seed-prd-05', 'TAS-PSP-C-2503', 'Tas paspor type C mar 2025',       'seed-cat-tas'),
  ('seed-prd-06', 'TAS-PSP-B-2511', 'Tas paspor type B nov 2025',       'seed-cat-tas'),
  ('seed-prd-07', 'TAS-SDL-PRS',    'Tas sendal parasut',               'seed-cat-tas'),
  ('seed-prd-08', 'TAS-SDL-TRN',    'Tas sendal torin',                 'seed-cat-tas'),
  ('seed-prd-09', 'BKU-DOA',        'Buku doa',                         'seed-cat-buku'),
  ('seed-prd-10', 'SYL-2C',         'Syal 2 color',                     'seed-cat-syal'),
  ('seed-prd-11', 'SYL-3C-TF',      'Syal 3 color typeface',            'seed-cat-syal'),
  ('seed-prd-12', 'SYL-2C-DOT',     'Syal 2 color dotted',              'seed-cat-syal'),
  ('seed-prd-13', 'SYL-3C-TFL',     'Syal 3 color typeface + logo',     'seed-cat-syal'),
  ('seed-prd-14', 'BTK-KTN',        'Batik katun',                      'seed-cat-batik'),
  ('seed-prd-15', 'BTK-PRT-SPX',    'Batik printing 1 side spandex',    'seed-cat-batik'),
  ('seed-prd-18', 'MKN-BLK',        'Mukena BLACK FULL',                'seed-cat-busana'),
  ('seed-prd-19', 'KRD-BLK',        'Kerudung BLACK FULL',              'seed-cat-busana'),
  ('seed-prd-20', 'IHR-REG',        'Ihrom ALL SIZE REGULAR',           'seed-cat-ihrom'),
  ('seed-prd-21', 'IHR-PRM',        'Ihrom ALL SIZE PREMIUM',           'seed-cat-ihrom'),
  ('seed-prd-22', 'IHR-PRM-DTF',    'Ihrom ALL SIZE PREMIUM DTF',       'seed-cat-ihrom'),
  ('seed-prd-23', 'IHR-SBK',        'Sabuk ihrom',                      'seed-cat-ihrom');

-- flag values mirror the defaults of the Product domain class
INSERT IGNORE INTO product
  (id, version, date_created, last_updated, name, product_code, category_id, product_family_id,
   product_type_id, unit_of_measure, package_size, created_by_id, updated_by_id,
   active, essential, lot_control, lot_and_expiry_control,
   cold_chain, controlled_substance, hazardous_material, serialized, reconditioned)
SELECT id, 0, @now, @now, name, product_code, category_id, family_id, 'DEFAULT',
       'pcs', 1, @admin, @admin,
       1, 1, 1, 0,
       0, 0, 0, 0, 0
FROM seed_product;

DROP TEMPORARY TABLE seed_product;

SELECT (SELECT COUNT(*) FROM location WHERE id LIKE 'seed-wh-%')  AS warehouses,
       (SELECT COUNT(*) FROM category WHERE id LIKE 'seed-cat-%' AND is_root = 0) AS categories,
       (SELECT COUNT(*) FROM product  WHERE id LIKE 'seed-prd-%') AS products;
