-- Deactivate AutoScout24 search config for BMW Z3 2.8 Vorfacelift.
-- The Z3 is scraped exclusively on mobile.de.

UPDATE search_configs
SET active = FALSE
WHERE platform = 'autoscout24'
  AND vehicle_id = (SELECT id FROM vehicles WHERE name = 'BMW Z3 2.8 Vorfacelift');
