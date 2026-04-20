-- Fix mobile.de search URL for BMW Z3 2.8 Vorfacelift.
-- Remove cc= (cubic capacity) filter — it was too restrictive and caused
-- mobile.de to return 0 exact results, showing only "ähnliche Fahrzeuge".
-- ms=3500;69;; = BMW / Z3 (make/model codes).
-- fr=1996:1998 = Erstzulassung 1996-1998 (Vorfacelift years).
-- "Andere Suchkriterien" cards are filtered out in the scraper itself.

UPDATE search_configs
SET search_url = 'https://suchen.mobile.de/fahrzeuge/search.html?dam=0&fr=1996%3A1998&isSearchRequest=true&ms=3500%3B69%3B%3B&s=Car&vc=Car'
WHERE platform = 'mobile_de';
