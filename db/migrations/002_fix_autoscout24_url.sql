-- Fix AutoScout24 search URL: /motorrad/ -> /lst-moto/
UPDATE search_configs
SET search_url = 'https://www.autoscout24.de/lst-moto/honda/cb-750-four?fregfrom=1969&fregto=1978&sort=standard&desc=0'
WHERE platform = 'autoscout24'
  AND search_url LIKE '%/motorrad/%';
