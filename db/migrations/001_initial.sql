-- Vehicle Market Price Tracker — Initial Schema

CREATE TABLE IF NOT EXISTS vehicles (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(200) NOT NULL UNIQUE,
    description     TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS search_configs (
    id              SERIAL PRIMARY KEY,
    vehicle_id      INTEGER NOT NULL REFERENCES vehicles(id),
    platform        VARCHAR(50) NOT NULL,
    search_url      TEXT NOT NULL,
    active          BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id              SERIAL PRIMARY KEY,
    search_config_id INTEGER NOT NULL REFERENCES search_configs(id),
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    status          VARCHAR(20) DEFAULT 'running',
    listings_found  INTEGER DEFAULT 0,
    error_message   TEXT,
    median_price    INTEGER,
    avg_price       INTEGER,
    min_price       INTEGER,
    max_price       INTEGER
);

CREATE TABLE IF NOT EXISTS listings (
    id              SERIAL PRIMARY KEY,
    search_config_id INTEGER NOT NULL REFERENCES search_configs(id),
    platform_id     VARCHAR(100) NOT NULL,
    listing_url     TEXT NOT NULL,
    first_seen      TIMESTAMPTZ DEFAULT NOW(),
    last_seen       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(search_config_id, platform_id)
);

CREATE TABLE IF NOT EXISTS listing_snapshots (
    id              SERIAL PRIMARY KEY,
    listing_id      INTEGER NOT NULL REFERENCES listings(id),
    scrape_run_id   INTEGER NOT NULL REFERENCES scrape_runs(id),
    price_cents     INTEGER,
    mileage_km      INTEGER,
    year            SMALLINT,
    location        VARCHAR(200),
    seller_type     VARCHAR(20),
    title           TEXT,
    scraped_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_listings_platform ON listings(search_config_id, platform_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_listing ON listing_snapshots(listing_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_run ON listing_snapshots(scrape_run_id);
CREATE INDEX IF NOT EXISTS idx_runs_config ON scrape_runs(search_config_id, started_at);

-- Initial data: vehicles and search configs
INSERT INTO vehicles (name, description) VALUES
    ('BMW Z3 2.8 Vorfacelift', 'BMW Z3 E36/7 2.8L, Baujahr 1996-1998 (vor Facelift)'),
    ('Honda CB 750 Four', 'Honda CB 750 Four, Baujahr 1969-1978')
ON CONFLICT (name) DO NOTHING;

INSERT INTO search_configs (vehicle_id, platform, search_url, active) VALUES
    (
        (SELECT id FROM vehicles WHERE name = 'BMW Z3 2.8 Vorfacelift'),
        'mobile_de',
        'https://suchen.mobile.de/fahrzeuge/search.html?dam=0&isSearchRequest=true&ms=3500%3B69%3B%3B&minFirstRegistrationDate=1996-01-01&maxFirstRegistrationDate=1998-12-31&minCubicCapacity=2700&maxCubicCapacity=2900',
        TRUE
    ),
    (
        (SELECT id FROM vehicles WHERE name = 'Honda CB 750 Four'),
        'autoscout24',
        'https://www.autoscout24.de/motorrad/honda/cb-750-four?fregfrom=1969&fregto=1978&sort=standard&desc=0',
        TRUE
    );
