-- Fix mobile.de search URL for BMW Z3 2.8 Vorfacelift.
-- The original URL used minCubicCapacity/maxCubicCapacity which are not
-- valid mobile.de params. The correct param is cc=2700:2900 (cubic capacity
-- range). Confirmed working: earlier debug session found 19 Z3 listings with
-- cc=2700:2900 in the search URL.

UPDATE search_configs
SET search_url = 'https://suchen.mobile.de/fahrzeuge/search.html?cc=2700%3A2900&dam=0&fr=1996%3A1998&isSearchRequest=true&ms=3500%3B69%3B%3B&s=Car&vc=Car'
WHERE platform = 'mobile_de';
