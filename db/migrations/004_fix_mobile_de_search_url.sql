-- Fix mobile.de search URL for BMW Z3 2.8 Vorfacelift.
-- Correct make/model codes: ms=3500;51;; = BMW / Z3
-- (Previous attempts used ms=3500;69;; which maps to BMW M135 — wrong!)
-- c=Cabrio, cc=2700:2900 (2.8L), fr=1996:1998 (Vorfacelift years).

UPDATE search_configs
SET search_url = 'https://suchen.mobile.de/fahrzeuge/search.html?isSearchRequest=true&s=Car&vc=Car&dam=false&fr=1996%3A1998&ms=3500%3B51%3B%3B&c=Cabrio&cc=2700%3A2900'
WHERE platform = 'mobile_de';
