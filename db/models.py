import logging
from statistics import median

logger = logging.getLogger(__name__)


def get_active_search_configs(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT sc.id, sc.platform, sc.search_url, v.name as vehicle_name
            FROM search_configs sc
            JOIN vehicles v ON v.id = sc.vehicle_id
            WHERE sc.active = TRUE
        """)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def create_scrape_run(conn, search_config_id):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO scrape_runs (search_config_id) VALUES (%s) RETURNING id",
            (search_config_id,),
        )
        conn.commit()
        return cur.fetchone()[0]


def finish_scrape_run(conn, run_id, status, listings_found=0, error_message=None):
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE scrape_runs
               SET finished_at = NOW(), status = %s, listings_found = %s, error_message = %s
               WHERE id = %s""",
            (status, listings_found, error_message, run_id),
        )
        conn.commit()


def upsert_listing(conn, search_config_id, platform_id, listing_url):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO listings (search_config_id, platform_id, listing_url)
               VALUES (%s, %s, %s)
               ON CONFLICT (search_config_id, platform_id)
               DO UPDATE SET last_seen = NOW()
               RETURNING id""",
            (search_config_id, platform_id, listing_url),
        )
        conn.commit()
        return cur.fetchone()[0]


def insert_snapshot(conn, listing_id, scrape_run_id, data):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO listing_snapshots
               (listing_id, scrape_run_id, price_cents, mileage_km, year, location, seller_type, title)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                listing_id,
                scrape_run_id,
                data.get("price_cents"),
                data.get("mileage_km"),
                data.get("year"),
                data.get("location"),
                data.get("seller_type"),
                data.get("title"),
            ),
        )
        conn.commit()


def update_run_statistics(conn, run_id):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT price_cents FROM listing_snapshots
               WHERE scrape_run_id = %s AND price_cents IS NOT NULL""",
            (run_id,),
        )
        prices = [row[0] for row in cur.fetchall()]

        if not prices:
            return

        median_price = int(median(prices))
        avg_price = int(sum(prices) / len(prices))

        cur.execute(
            """UPDATE scrape_runs
               SET median_price = %s, avg_price = %s, min_price = %s, max_price = %s
               WHERE id = %s""",
            (median_price, avg_price, min(prices), max(prices), run_id),
        )
        conn.commit()
        logger.info(
            "Run %d stats: median=%d avg=%d min=%d max=%d (n=%d)",
            run_id, median_price, avg_price, min(prices), max(prices), len(prices),
        )
