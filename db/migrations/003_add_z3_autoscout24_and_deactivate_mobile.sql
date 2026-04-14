-- Add BMW Z3 search on AutoScout24 (active)
INSERT INTO search_configs (vehicle_id, platform, search_url, active) VALUES
    (
        (SELECT id FROM vehicles WHERE name = 'BMW Z3 2.8 Vorfacelift'),
        'autoscout24',
        'https://www.autoscout24.de/lst/bmw/z3/ve_2.8?fregfrom=1996&fregto=1998&sort=standard&desc=0',
        TRUE
    )
ON CONFLICT DO NOTHING;

-- Deactivate mobile.de until IP ban expires and scraper is improved
UPDATE search_configs
SET active = FALSE
WHERE platform = 'mobile_de';
