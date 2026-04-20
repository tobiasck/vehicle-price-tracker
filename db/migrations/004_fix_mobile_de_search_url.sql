-- Fix mobile.de search URL for BMW Z3 2.8 Vorfacelift.
-- The old URL had minCubicCapacity/maxCubicCapacity params that are not
-- standard mobile.de query params — they were silently ignored, causing
-- mobile.de to return 0 results and showing "ähnliche Fahrzeuge" instead.
-- Use the cc= param (cubic capacity range in cc) that mobile.de actually
-- supports, and keep fr= (Erstzulassung) and ms= (make/model: BMW/Z3).

UPDATE search_configs
SET search_url = 'https://suchen.mobile.de/fahrzeuge/search.html?dam=0&fr=1996%3A1998&isSearchRequest=true&ms=3500%3B69%3B%3B&s=Car&vc=Car'
WHERE platform = 'mobile_de';
