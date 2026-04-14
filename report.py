#!/usr/bin/env python3
"""Generate price trend charts and HTML report from scraped data."""

import argparse
import base64
import io
import logging
import os
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from config.logging_config import setup_logging
from db.connection import get_connection

logger = logging.getLogger(__name__)

REPORT_DIR = os.path.join(os.path.dirname(__file__), "report")


def get_vehicle_stats(conn):
    """Get aggregated price stats per scrape run for each vehicle."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                v.name AS vehicle_name,
                sr.started_at,
                sr.median_price,
                sr.avg_price,
                sr.min_price,
                sr.max_price,
                sr.listings_found,
                sc.platform
            FROM scrape_runs sr
            JOIN search_configs sc ON sc.id = sr.search_config_id
            JOIN vehicles v ON v.id = sc.vehicle_id
            WHERE sr.status = 'success' AND sr.median_price IS NOT NULL
            ORDER BY v.name, sr.started_at
        """)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def get_listing_prices(conn, vehicle_name):
    """Get individual listing prices over time for a vehicle."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                ls.scraped_at,
                ls.price_cents,
                ls.title,
                l.platform_id
            FROM listing_snapshots ls
            JOIN listings l ON l.id = ls.listing_id
            JOIN search_configs sc ON sc.id = l.search_config_id
            JOIN vehicles v ON v.id = sc.vehicle_id
            WHERE v.name = %s AND ls.price_cents IS NOT NULL
            ORDER BY ls.scraped_at
        """, (vehicle_name,))
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def get_current_listings(conn, vehicle_name):
    """Get the most recent snapshot for each active listing."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (l.platform_id)
                ls.price_cents,
                ls.mileage_km,
                ls.year,
                ls.location,
                ls.seller_type,
                ls.title,
                l.listing_url,
                ls.scraped_at
            FROM listing_snapshots ls
            JOIN listings l ON l.id = ls.listing_id
            JOIN search_configs sc ON sc.id = l.search_config_id
            JOIN vehicles v ON v.id = sc.vehicle_id
            WHERE v.name = %s AND ls.price_cents IS NOT NULL
            ORDER BY l.platform_id, ls.scraped_at DESC
        """, (vehicle_name,))
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def cents_to_eur(cents):
    return cents / 100 if cents else 0


def create_price_trend_chart(stats, vehicle_name):
    """Create a price trend chart (median, avg, min, max over time)."""
    vehicle_stats = [s for s in stats if s["vehicle_name"] == vehicle_name]
    if not vehicle_stats:
        return None

    dates = [s["started_at"] for s in vehicle_stats]
    median_prices = [cents_to_eur(s["median_price"]) for s in vehicle_stats]
    avg_prices = [cents_to_eur(s["avg_price"]) for s in vehicle_stats]
    min_prices = [cents_to_eur(s["min_price"]) for s in vehicle_stats]
    max_prices = [cents_to_eur(s["max_price"]) for s in vehicle_stats]

    fig, ax = plt.subplots(figsize=(10, 5))

    ax.fill_between(dates, min_prices, max_prices, alpha=0.15, color="#2196F3", label="Min–Max")
    ax.plot(dates, median_prices, "o-", color="#1565C0", linewidth=2, markersize=6, label="Median")
    ax.plot(dates, avg_prices, "s--", color="#FF9800", linewidth=1.5, markersize=5, label="Durchschnitt")

    ax.set_title(f"Preisentwicklung: {vehicle_name}", fontsize=14, fontweight="bold")
    ax.set_xlabel("Datum")
    ax.set_ylabel("Preis (€)")
    ax.legend()
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m.%Y"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate()

    # Format y-axis with € and thousands separator
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f"{x:,.0f} €".replace(",", ".")))

    plt.tight_layout()
    return fig_to_base64(fig)


def create_price_distribution_chart(conn, vehicle_name):
    """Create a histogram of current listing prices."""
    listings = get_current_listings(conn, vehicle_name)
    if not listings:
        return None

    prices = [cents_to_eur(l["price_cents"]) for l in listings]

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.hist(prices, bins=min(20, len(prices)), color="#2196F3", edgecolor="white", alpha=0.8)
    ax.axvline(sorted(prices)[len(prices) // 2], color="#F44336", linestyle="--", linewidth=2,
               label=f"Median: {sorted(prices)[len(prices) // 2]:,.0f} €".replace(",", "."))
    ax.set_title(f"Preisverteilung: {vehicle_name} (n={len(prices)})", fontsize=14, fontweight="bold")
    ax.set_xlabel("Preis (€)")
    ax.set_ylabel("Anzahl Inserate")
    ax.legend()
    ax.grid(True, alpha=0.3, axis="y")
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f"{x:,.0f} €".replace(",", ".")))
    plt.tight_layout()
    return fig_to_base64(fig)


def fig_to_base64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")


def build_listings_table(conn, vehicle_name):
    """Build HTML table of current listings."""
    listings = get_current_listings(conn, vehicle_name)
    if not listings:
        return "<p>Keine Inserate gefunden.</p>"

    # Sort by price
    listings.sort(key=lambda l: l["price_cents"] or 0)

    rows = []
    for l in listings:
        price = f'{cents_to_eur(l["price_cents"]):,.0f} €'.replace(",", ".")
        km = f'{l["mileage_km"]:,} km'.replace(",", ".") if l["mileage_km"] else "–"
        year = l["year"] or "–"
        location = l["location"] or "–"
        seller = l["seller_type"] or "–"
        title = (l["title"] or "–")[:80]
        url = l["listing_url"] or "#"
        rows.append(
            f"<tr><td>{price}</td><td>{year}</td><td>{km}</td>"
            f"<td>{location}</td><td>{seller}</td>"
            f'<td><a href="{url}" target="_blank">{title}</a></td></tr>'
        )

    return f"""
    <table>
        <thead><tr>
            <th>Preis</th><th>Baujahr</th><th>km</th>
            <th>Ort</th><th>Verkäufer</th><th>Inserat</th>
        </tr></thead>
        <tbody>{"".join(rows)}</tbody>
    </table>
    """


def generate_html_report(conn, stats):
    """Generate complete HTML report."""
    # Get unique vehicle names
    vehicles = list(dict.fromkeys(s["vehicle_name"] for s in stats))

    # If no stats yet, get vehicles from DB
    if not vehicles:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM vehicles ORDER BY name")
            vehicles = [row[0] for row in cur.fetchall()]

    sections = []
    for vehicle in vehicles:
        trend_chart = create_price_trend_chart(stats, vehicle)
        dist_chart = create_price_distribution_chart(conn, vehicle)
        table = build_listings_table(conn, vehicle)

        section = f'<div class="vehicle-section"><h2>{vehicle}</h2>'

        if trend_chart:
            section += f'<h3>Preisentwicklung</h3><img src="data:image/png;base64,{trend_chart}" alt="Preistrend">'
        else:
            section += "<p><em>Noch keine historischen Daten für Trendanalyse.</em></p>"

        if dist_chart:
            section += f'<h3>Preisverteilung (aktuell)</h3><img src="data:image/png;base64,{dist_chart}" alt="Preisverteilung">'

        section += f"<h3>Aktuelle Inserate</h3>{table}</div>"
        sections.append(section)

    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="3600">
    <title>Fahrzeug-Preistracker</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               background: #f5f5f5; color: #333; padding: 20px; max-width: 1100px; margin: 0 auto; }}
        h1 {{ margin-bottom: 5px; color: #1a1a1a; }}
        .subtitle {{ color: #666; margin-bottom: 30px; font-size: 0.9em; }}
        .vehicle-section {{ background: #fff; border-radius: 8px; padding: 25px;
                           margin-bottom: 25px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        h2 {{ color: #1565C0; margin-bottom: 20px; padding-bottom: 10px; border-bottom: 2px solid #e0e0e0; }}
        h3 {{ color: #555; margin: 20px 0 10px; font-size: 1em; }}
        img {{ max-width: 100%; height: auto; border-radius: 4px; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 0.85em; margin-top: 10px; }}
        th {{ background: #f0f0f0; padding: 8px 10px; text-align: left; font-weight: 600;
             border-bottom: 2px solid #ddd; }}
        td {{ padding: 7px 10px; border-bottom: 1px solid #eee; }}
        tr:hover {{ background: #f8f8f8; }}
        a {{ color: #1565C0; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    <h1>Fahrzeug-Preistracker</h1>
    <p class="subtitle">Letzte Aktualisierung: {now}</p>
    {"".join(sections)}
</body>
</html>"""
    return html


def main():
    parser = argparse.ArgumentParser(description="Generate price report")
    parser.add_argument("--output", type=str, default=REPORT_DIR, help="Output directory")
    args = parser.parse_args()

    setup_logging()

    os.makedirs(args.output, exist_ok=True)
    output_file = os.path.join(args.output, "index.html")

    conn = get_connection()
    try:
        stats = get_vehicle_stats(conn)
        html = generate_html_report(conn, stats)

        with open(output_file, "w") as f:
            f.write(html)

        logger.info("Report generated: %s", output_file)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
