#!/usr/bin/env python3
"""Generate interactive price report with Plotly charts."""

import argparse
import json
import logging
import os
from datetime import datetime

from config.logging_config import setup_logging
from db.connection import get_connection

logger = logging.getLogger(__name__)

REPORT_DIR = os.path.join(os.path.dirname(__file__), "report")


def slugify(name):
    """Convert vehicle name to a safe HTML id slug."""
    import re
    s = name.lower().strip()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    return s.strip('-')


def get_vehicles(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT v.id, v.name, v.description,
                   COUNT(DISTINCT sc.id) AS config_count,
                   COALESCE(SUM(sr.listings_found), 0) AS total_listings
            FROM vehicles v
            LEFT JOIN search_configs sc ON sc.vehicle_id = v.id AND sc.active = TRUE
            LEFT JOIN scrape_runs sr ON sr.search_config_id = sc.id AND sr.status = 'success'
            GROUP BY v.id, v.name, v.description
            ORDER BY v.name
        """)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def get_vehicle_stats(conn):
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


def get_current_listings(conn, vehicle_name):
    """Return only listings seen in the most recent successful scrape run.
    This ensures sold/removed vehicles disappear from the table after the
    next scrape."""
    with conn.cursor() as cur:
        # Find the latest successful run for this vehicle (any platform)
        cur.execute("""
            SELECT sr.id
            FROM scrape_runs sr
            JOIN search_configs sc ON sc.id = sr.search_config_id
            JOIN vehicles v ON v.id = sc.vehicle_id
            WHERE v.name = %s AND sr.status = 'success'
            ORDER BY sr.started_at DESC
            LIMIT 1
        """, (vehicle_name,))
        row = cur.fetchone()
        if not row:
            return []
        latest_run_id = row[0]

        cur.execute("""
            SELECT
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
            WHERE v.name = %s
              AND ls.scrape_run_id = %s
              AND ls.price_cents IS NOT NULL
            ORDER BY ls.price_cents ASC
        """, (vehicle_name, latest_run_id))
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def serialize_stats(stats):
    """Convert stats to JSON-serializable format."""
    result = {}
    for s in stats:
        name = s["vehicle_name"]
        if name not in result:
            result[name] = []
        result[name].append({
            "date": s["started_at"].isoformat(),
            "median": s["median_price"] / 100 if s["median_price"] else None,
            "avg": s["avg_price"] / 100 if s["avg_price"] else None,
            "min": s["min_price"] / 100 if s["min_price"] else None,
            "max": s["max_price"] / 100 if s["max_price"] else None,
            "count": s["listings_found"],
            "platform": s["platform"],
        })
    return result


def serialize_listings(conn, vehicles):
    """Convert all listings to JSON-serializable format."""
    result = {}
    for v in vehicles:
        listings = get_current_listings(conn, v["name"])
        result[v["name"]] = [{
            "price": l["price_cents"] / 100 if l["price_cents"] else 0,
            "km": l["mileage_km"],
            "year": l["year"],
            "location": l["location"],
            "seller": l["seller_type"],
            "title": (l["title"] or "")[:100],
            "url": l["listing_url"] or "",
            "scraped": l["scraped_at"].isoformat() if l["scraped_at"] else "",
        } for l in listings]
    return result


def generate_html(conn, stats, vehicles):
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    stats_json = json.dumps(serialize_stats(stats))
    listings_json = json.dumps(serialize_listings(conn, vehicles))
    vehicles_json = json.dumps([{"name": v["name"], "description": v["description"],
                                  "total_listings": int(v["total_listings"]),
                                  "slug": slugify(v["name"])} for v in vehicles])

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Fahrzeug-Preistracker</title>
<script src="https://cdn.plot.ly/plotly-2.35.0.min.js"></script>
<style>
:root {{
    --bg: #121212; --bg2: #1e1e1e; --card: #252525; --border: #383838;
    --text: #e0e0e0; --text2: #888; --accent: #90caf9; --accent2: #ffb74d;
    --danger: #ef5350; --success: #66bb6a;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
       background:var(--bg); color:var(--text); }}

.landing {{ min-height:100vh; display:flex; flex-direction:column; align-items:center;
           justify-content:center; padding:40px 20px; }}
.landing h1 {{ font-size:2.2em; margin-bottom:8px; color:#fff; }}
.landing .subtitle {{ color:var(--text2); margin-bottom:40px; }}
.vehicle-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr));
                gap:20px; max-width:800px; width:100%; }}
.vehicle-card {{ background:var(--card); border:1px solid var(--border); border-radius:12px;
                padding:28px; cursor:pointer; transition:all 0.2s; }}
.vehicle-card:hover {{ border-color:var(--accent); transform:translateY(-3px);
                      box-shadow:0 8px 25px rgba(0,0,0,0.4); }}
.vehicle-card h2 {{ font-size:1.3em; margin-bottom:8px; color:#fff; }}
.vehicle-card p {{ color:var(--text2); font-size:0.9em; }}
.vehicle-card .stat {{ display:inline-block; background:var(--bg2); padding:4px 10px;
                      border-radius:6px; font-size:0.8em; margin-top:12px; color:var(--accent); }}

/* Admin buttons */
.btn {{ display:inline-flex; align-items:center; gap:6px; padding:9px 18px; border-radius:7px;
       font-size:0.88em; font-weight:500; cursor:pointer; border:none; transition:all 0.18s; }}
.btn-primary {{ background:#2979ff; color:#fff; }}
.btn-primary:hover {{ background:#448aff; }}
.btn-success {{ background:#2e7d32; color:#fff; }}
.btn-success:hover {{ background:#388e3c; }}
.btn-outline {{ background:none; border:1px solid var(--border); color:var(--text); }}
.btn-outline:hover {{ border-color:var(--accent); color:var(--accent); }}
.landing-actions {{ display:flex; gap:12px; margin-bottom:32px; }}

/* Modal */
.modal-overlay {{ display:none; position:fixed; inset:0; background:rgba(0,0,0,0.7);
                 z-index:100; align-items:center; justify-content:center; }}
.modal-overlay.open {{ display:flex; }}
.modal {{ background:var(--card); border:1px solid var(--border); border-radius:12px;
         padding:28px; width:100%; max-width:500px; }}
.modal h2 {{ font-size:1.2em; margin-bottom:20px; color:#fff; }}
.form-group {{ margin-bottom:14px; }}
.form-group label {{ display:block; font-size:0.82em; color:var(--text2);
                    margin-bottom:5px; font-weight:500; }}
.form-group input, .form-group select, .form-group textarea {{
    width:100%; background:var(--bg2); border:1px solid var(--border);
    color:var(--text); padding:9px 12px; border-radius:6px; font-size:0.88em;
    outline:none; font-family:inherit; }}
.form-group input:focus, .form-group select:focus, .form-group textarea:focus {{
    border-color:var(--accent); }}
.form-group textarea {{ resize:vertical; min-height:60px; }}
.modal-actions {{ display:flex; gap:10px; justify-content:flex-end; margin-top:20px; }}
.modal-msg {{ font-size:0.85em; padding:8px 12px; border-radius:6px; margin-top:10px;
             display:none; }}
.modal-msg.success {{ background:#1b5e20; color:#a5d6a7; display:block; }}
.modal-msg.error {{ background:#b71c1c; color:#ffcdd2; display:block; }}

/* Run status bar */
.run-status {{ display:none; position:fixed; bottom:20px; right:20px; z-index:200;
              background:var(--card); border:1px solid var(--border); border-radius:10px;
              padding:14px 18px; min-width:260px; box-shadow:0 4px 20px rgba(0,0,0,0.5); }}
.run-status.visible {{ display:block; }}
.run-status-title {{ font-weight:600; margin-bottom:6px; color:#fff; font-size:0.9em; }}
.run-status-log {{ font-size:0.75em; color:var(--text2); max-height:120px; overflow-y:auto;
                  font-family:monospace; }}
.spinner {{ display:inline-block; width:10px; height:10px; border:2px solid var(--border);
           border-top-color:var(--accent); border-radius:50%;
           animation:spin 0.7s linear infinite; margin-right:6px; }}
@keyframes spin {{ to {{ transform:rotate(360deg); }} }}

.detail {{ display:none; max-width:1100px; margin:0 auto; padding:20px; }}
.detail.active {{ display:block; }}
.back-btn {{ background:none; border:1px solid var(--border); color:var(--accent);
            padding:8px 16px; border-radius:6px; cursor:pointer; font-size:0.9em;
            margin-bottom:20px; transition:all 0.2s; }}
.back-btn:hover {{ background:var(--card); border-color:var(--accent); }}
.detail h1 {{ font-size:1.6em; margin-bottom:5px; color:#fff; }}
.detail .meta {{ color:var(--text2); margin-bottom:25px; font-size:0.9em; }}

.chart-card {{ background:var(--card); border-radius:10px; padding:20px;
              margin-bottom:20px; border:1px solid var(--border); }}
.chart-card h3 {{ color:var(--text2); font-size:0.95em; margin-bottom:12px; font-weight:500; }}

.table-section {{ background:var(--card); border-radius:10px; padding:20px;
                 border:1px solid var(--border); }}
.table-section h3 {{ color:var(--text2); font-size:0.95em; margin-bottom:12px; font-weight:500; }}

.filters {{ display:flex; gap:10px; margin-bottom:12px; flex-wrap:wrap; }}
.filters input, .filters select {{
    background:var(--bg2); border:1px solid var(--border); color:var(--text);
    padding:7px 12px; border-radius:6px; font-size:0.85em; outline:none; }}
.filters input:focus, .filters select:focus {{ border-color:var(--accent); }}
.filters input::placeholder {{ color:var(--text2); }}

.table-wrap {{ max-height:400px; overflow-y:auto; border-radius:6px; }}
.table-wrap::-webkit-scrollbar {{ width:6px; }}
.table-wrap::-webkit-scrollbar-track {{ background:var(--bg2); border-radius:3px; }}
.table-wrap::-webkit-scrollbar-thumb {{ background:var(--border); border-radius:3px; }}
.table-wrap::-webkit-scrollbar-thumb:hover {{ background:#555; }}

table {{ width:100%; border-collapse:collapse; font-size:0.85em; }}
thead {{ position:sticky; top:0; z-index:1; }}
th {{ background:var(--bg2); padding:10px 12px; text-align:left; font-weight:600;
     color:var(--text2); border-bottom:2px solid var(--border); cursor:pointer;
     user-select:none; white-space:nowrap; }}
th:hover {{ color:var(--accent); }}
th .sort-icon {{ margin-left:4px; font-size:0.7em; }}
td {{ padding:8px 12px; border-bottom:1px solid var(--border); white-space:nowrap; }}
tr:hover {{ background:rgba(255,255,255,0.03); }}
a {{ color:var(--accent); text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
.no-data {{ text-align:center; padding:40px; color:var(--text2); }}
</style>
</head>
<body>

<div class="landing" id="landing">
    <h1>Fahrzeug-Preistracker</h1>
    <p class="subtitle">Letzte Aktualisierung: {now}</p>
    <div class="landing-actions">
        <button class="btn btn-success" onclick="startRun()">
            &#9654; Jetzt scrapen
        </button>
        <button class="btn btn-primary" onclick="openAddModal()">
            &#43; Fahrzeug hinzufügen
        </button>
        <button class="btn btn-outline" onclick="openScheduleModal()">
            &#9881; Zeitplan
        </button>
    </div>
    <div id="nextRunInfo" style="font-size:0.8em;color:var(--text2);margin-top:-20px;margin-bottom:20px;"></div>
    <div class="vehicle-grid" id="vehicleGrid"></div>
</div>

<div id="detailContainer"></div>

<!-- Add vehicle modal -->
<div class="modal-overlay" id="addModal">
    <div class="modal">
        <h2>&#43; Fahrzeug hinzufügen</h2>
        <div class="form-group">
            <label>Name *</label>
            <input id="add-name" type="text" placeholder="z.B. BMW Z3 2.8 Vorfacelift">
        </div>
        <div class="form-group">
            <label>Beschreibung</label>
            <input id="add-desc" type="text" placeholder="z.B. E36/7, Baujahr 1996-1998">
        </div>
        <div class="form-group">
            <label>Plattform *</label>
            <select id="add-platform">
                <option value="mobile_de">mobile.de</option>
                <option value="autoscout24">AutoScout24</option>
                <option value="kleinanzeigen">Kleinanzeigen</option>
            </select>
        </div>
        <div class="form-group">
            <label>Such-URL *</label>
            <textarea id="add-url" placeholder="https://suchen.mobile.de/fahrzeuge/search.html?..."></textarea>
        </div>
        <div id="add-msg" class="modal-msg"></div>
        <div class="modal-actions">
            <button class="btn btn-outline" onclick="closeAddModal()">Abbrechen</button>
            <button class="btn btn-primary" onclick="submitAddVehicle()">Hinzufügen</button>
        </div>
    </div>
</div>

<!-- Schedule modal -->
<div class="modal-overlay" id="scheduleModal">
    <div class="modal">
        <h2>&#9881; Automatischer Zeitplan</h2>
        <div class="form-group" style="display:flex;align-items:center;gap:10px;">
            <label style="margin:0;flex:1;">Automatisch scrapen</label>
            <input type="checkbox" id="sch-enabled" style="width:auto;transform:scale(1.4);">
        </div>
        <div id="sch-options" style="margin-top:16px;">
            <div class="form-group">
                <label>Häufigkeit</label>
                <select id="sch-frequency" onchange="updateScheduleUI()">
                    <option value="daily">Täglich</option>
                    <option value="weekly">Wöchentlich</option>
                    <option value="interval">Alle X Stunden</option>
                </select>
            </div>
            <div class="form-group" id="sch-weekday-row">
                <label>Wochentag</label>
                <select id="sch-weekday">
                    <option value="0">Montag</option>
                    <option value="1">Dienstag</option>
                    <option value="2">Mittwoch</option>
                    <option value="3">Donnerstag</option>
                    <option value="4">Freitag</option>
                    <option value="5">Samstag</option>
                    <option value="6">Sonntag</option>
                </select>
            </div>
            <div class="form-group" id="sch-time-row">
                <label>Uhrzeit</label>
                <input type="time" id="sch-time" value="06:00">
            </div>
            <div class="form-group" id="sch-interval-row" style="display:none;">
                <label>Intervall (Stunden)</label>
                <input type="number" id="sch-interval" min="1" max="168" value="24">
            </div>
        </div>
        <div id="sch-msg" class="modal-msg"></div>
        <div class="modal-actions">
            <button class="btn btn-outline" onclick="closeScheduleModal()">Schließen</button>
            <button class="btn btn-primary" onclick="saveSchedule()">Speichern</button>
        </div>
    </div>
</div>

<!-- Run status bar -->
<div class="run-status" id="runStatus">
    <div class="run-status-title" id="runStatusTitle">
        <span class="spinner" id="runSpinner"></span>Scrape läuft...
    </div>
    <div class="run-status-log" id="runStatusLog"></div>
</div>

<script>
const STATS = {stats_json};
const LISTINGS = {listings_json};
const VEHICLES = {vehicles_json};

const fmt = n => n != null ? n.toLocaleString('de-DE', {{maximumFractionDigits:0}}) + ' \\u20ac' : '\\u2013';
const fmtKm = n => n != null ? n.toLocaleString('de-DE') + ' km' : '\\u2013';

// Landing page
const grid = document.getElementById('vehicleGrid');
VEHICLES.forEach((v, idx) => {{
    const listings = LISTINGS[v.name] || [];
    const stats = STATS[v.name] || [];
    const lastStat = stats[stats.length - 1];
    const medianStr = lastStat ? fmt(lastStat.median) : 'Keine Daten';

    const card = document.createElement('div');
    card.className = 'vehicle-card';
    card.innerHTML = `
        <h2>${{v.name}}</h2>
        <p>${{v.description || ''}}</p>
        <span class="stat">${{listings.length}} Inserate</span>
        <span class="stat">Median: ${{medianStr}}</span>
    `;
    card.onclick = () => showDetail(v.slug, v.name);
    grid.appendChild(card);
}});

function showDetail(slug, name) {{
    document.getElementById('landing').style.display = 'none';
    document.querySelectorAll('.detail').forEach(d => d.classList.remove('active'));
    const el = document.getElementById('detail-' + slug);
    if (el) {{ el.classList.add('active'); }}
    renderCharts(slug, name);
}}

function showLanding() {{
    document.querySelectorAll('.detail').forEach(d => d.classList.remove('active'));
    document.getElementById('landing').style.display = 'flex';
}}

// Detail pages
const container = document.getElementById('detailContainer');
VEHICLES.forEach(v => {{
    const slug = v.slug;
    const div = document.createElement('div');
    div.className = 'detail';
    div.id = 'detail-' + slug;

    const listings = LISTINGS[v.name] || [];

    div.innerHTML = `
        <button class="back-btn" onclick="showLanding()">\\u2190 Alle Fahrzeuge</button>
        <h1>${{v.name}}</h1>
        <p class="meta">${{v.description || ''}} &middot; ${{listings.length}} aktuelle Inserate</p>

        <div class="chart-card">
            <h3>Preisentwicklung (Zoom: Bereich markieren, Doppelklick = Reset)</h3>
            <div id="trend-${{slug}}" style="height:350px"></div>
        </div>

        <div class="chart-card">
            <h3>Preisverteilung</h3>
            <div id="dist-${{slug}}" style="height:280px"></div>
        </div>

        <div class="table-section">
            <h3>Aktuelle Inserate</h3>
            <div class="filters">
                <input type="text" placeholder="Suche..." oninput="filterTable('${{slug}}', this.closest('.filters'))">
                <input type="number" placeholder="Preis min" oninput="filterTable('${{slug}}', this.closest('.filters'))">
                <input type="number" placeholder="Preis max" oninput="filterTable('${{slug}}', this.closest('.filters'))">
                <select onchange="filterTable('${{slug}}', this.closest('.filters'))">
                    <option value="">Alle Verkäufer</option>
                    <option value="private">Privat</option>
                    <option value="dealer">Händler</option>
                </select>
            </div>
            <div class="table-wrap">
                <table id="table-${{slug}}">
                    <thead><tr>
                        <th onclick="sortTable('${{slug}}',0)">Preis <span class="sort-icon">\\u25B2\\u25BC</span></th>
                        <th onclick="sortTable('${{slug}}',1)">Baujahr <span class="sort-icon">\\u25B2\\u25BC</span></th>
                        <th onclick="sortTable('${{slug}}',2)">km <span class="sort-icon">\\u25B2\\u25BC</span></th>
                        <th>Ort</th>
                        <th>Verkäufer</th>
                        <th>Inserat</th>
                    </tr></thead>
                    <tbody></tbody>
                </table>
            </div>
        </div>
    `;
    container.appendChild(div);

    // Fill table
    const tbody = div.querySelector('tbody');
    listings.sort((a,b) => a.price - b.price);
    listings.forEach(l => {{
        const tr = document.createElement('tr');
        tr.dataset.search = (l.title + ' ' + l.location + ' ' + l.seller).toLowerCase();
        tr.dataset.price = l.price;
        tr.dataset.seller = l.seller || '';
        tr.innerHTML = `
            <td>${{fmt(l.price)}}</td>
            <td>${{l.year || '\\u2013'}}</td>
            <td>${{fmtKm(l.km)}}</td>
            <td>${{l.location || '\\u2013'}}</td>
            <td>${{l.seller || '\\u2013'}}</td>
            <td><a href="${{l.url}}" target="_blank">${{l.title || '\\u2013'}}</a></td>
        `;
        tbody.appendChild(tr);
    }});
}});

const plotLayout = {{
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: 'rgba(0,0,0,0)',
    font: {{ color: '#e0e0e0', size: 12 }},
    margin: {{ l:60, r:20, t:10, b:40 }},
    xaxis: {{ gridcolor:'#333', linecolor:'#333' }},
    yaxis: {{ gridcolor:'#333', linecolor:'#333', ticksuffix: ' \\u20ac' }},
    legend: {{ bgcolor:'rgba(0,0,0,0)', x:0, y:1.15, orientation:'h' }},
    dragmode: 'zoom',
}};
const plotConfig = {{ responsive:true, displayModeBar:true,
    modeBarButtonsToRemove:['lasso2d','select2d','autoScale2d'],
    displaylogo:false }};

function renderCharts(slug, name) {{
    const stats = STATS[name] || [];
    const listings = LISTINGS[name] || [];

    const trendDiv = document.getElementById('trend-' + slug);
    if (stats.length > 0) {{
        const dates = stats.map(s => s.date);
        const traces = [
            {{ x:dates, y:stats.map(s=>s.min), fill:'none', mode:'lines', line:{{width:0}},
              showlegend:false, hoverinfo:'skip' }},
            {{ x:dates, y:stats.map(s=>s.max), fill:'tonexty', fillcolor:'rgba(144,202,249,0.08)',
              mode:'lines', line:{{width:0}}, name:'Min\\u2013Max', hoverinfo:'skip' }},
            {{ x:dates, y:stats.map(s=>s.median), mode:'lines+markers', name:'Median',
              line:{{color:'#90caf9',width:2}}, marker:{{size:7}} }},
            {{ x:dates, y:stats.map(s=>s.avg), mode:'lines+markers', name:'Durchschnitt',
              line:{{color:'#ffb74d',width:2,dash:'dash'}}, marker:{{size:5,symbol:'square'}} }},
        ];
        Plotly.newPlot(trendDiv, traces, plotLayout, plotConfig);
    }} else {{
        trendDiv.innerHTML = '<p class="no-data">Noch keine historischen Daten.</p>';
    }}

    const distDiv = document.getElementById('dist-' + slug);
    if (listings.length > 0) {{
        const prices = listings.map(l => l.price).filter(p => p > 0);
        const median = [...prices].sort((a,b)=>a-b)[Math.floor(prices.length/2)];
        Plotly.newPlot(distDiv, [
            {{ x:prices, type:'histogram', marker:{{color:'rgba(144,202,249,0.5)',
              line:{{color:'rgba(144,202,249,0.8)',width:1}}}} }},
        ], {{
            ...plotLayout,
            xaxis: {{ ...plotLayout.xaxis, ticksuffix:' \\u20ac' }},
            yaxis: {{ ...plotLayout.yaxis, ticksuffix:'', title:'Anzahl' }},
            shapes: [{{ type:'line', x0:median, x1:median, y0:0, y1:1, yref:'paper',
                       line:{{color:'#ef5350',width:2,dash:'dash'}} }}],
            annotations: [{{ x:median, y:1, yref:'paper', text:'Median: '+fmt(median),
                           showarrow:false, font:{{color:'#ef5350'}}, yshift:10 }}],
        }}, plotConfig);
    }} else {{
        distDiv.innerHTML = '<p class="no-data">Keine Inserate.</p>';
    }}
}}

function filterTable(slug, filtersDiv) {{
    const inputs = filtersDiv.querySelectorAll('input, select');
    const search = inputs[0].value.toLowerCase();
    const minPrice = parseFloat(inputs[1].value) || 0;
    const maxPrice = parseFloat(inputs[2].value) || Infinity;
    const seller = inputs[3].value;

    const rows = document.querySelectorAll('#table-' + slug + ' tbody tr');
    rows.forEach(tr => {{
        const matchSearch = !search || tr.dataset.search.includes(search);
        const price = parseFloat(tr.dataset.price) || 0;
        const matchPrice = price >= minPrice && price <= maxPrice;
        const matchSeller = !seller || tr.dataset.seller === seller;
        tr.style.display = (matchSearch && matchPrice && matchSeller) ? '' : 'none';
    }});
}}

const sortState = {{}};
function sortTable(slug, colIdx) {{
    const key = slug + colIdx;
    sortState[key] = !(sortState[key] || false);
    const asc = sortState[key];

    const tbody = document.querySelector('#table-' + slug + ' tbody');
    const rows = Array.from(tbody.rows);
    rows.sort((a,b) => {{
        let va = a.cells[colIdx].textContent.replace(/[^0-9,.\\-]/g,'').replace(/\\./g,'').replace(',','.');
        let vb = b.cells[colIdx].textContent.replace(/[^0-9,.\\-]/g,'').replace(/\\./g,'').replace(',','.');
        va = parseFloat(va) || 0; vb = parseFloat(vb) || 0;
        return asc ? va - vb : vb - va;
    }});
    rows.forEach(r => tbody.appendChild(r));
}}

// ── Admin: scrape run ────────────────────────────────────────────────────────
let _pollInterval = null;

async function startRun(target) {{
    const res = await fetch('/api/run', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(target ? {{target}} : {{}})
    }});
    const data = await res.json();
    if (res.status === 409) {{
        alert('Scrape läuft bereits — bitte warten.');
        return;
    }}
    if (res.ok) {{
        showRunStatus();
        startPolling();
    }}
}}

function showRunStatus() {{
    document.getElementById('runStatus').classList.add('visible');
    document.getElementById('runStatusTitle').innerHTML =
        '<span class="spinner" id="runSpinner"></span>Scrape läuft...';
    document.getElementById('runStatusLog').textContent = '';
}}

function startPolling() {{
    if (_pollInterval) clearInterval(_pollInterval);
    _pollInterval = setInterval(pollStatus, 1500);
}}

async function pollStatus() {{
    try {{
        const res = await fetch('/api/status');
        const data = await res.json();

        const logEl = document.getElementById('runStatusLog');
        logEl.textContent = (data.log || []).slice(-15).join('\\n');
        logEl.scrollTop = logEl.scrollHeight;

        if (!data.running) {{
            clearInterval(_pollInterval);
            _pollInterval = null;
            const ok = data.exit_code === 0;
            document.getElementById('runStatusTitle').innerHTML =
                ok ? '&#10003; Scrape abgeschlossen' : '&#10007; Scrape fehlgeschlagen';
            // Reload page after 3s to show fresh data
            setTimeout(() => location.reload(), 3000);
        }}
    }} catch(e) {{}}
}}

// ── Schedule modal ───────────────────────────────────────────────────────────
const WEEKDAYS = ['Montag','Dienstag','Mittwoch','Donnerstag','Freitag','Samstag','Sonntag'];

async function openScheduleModal() {{
    const res = await fetch('/api/schedule');
    const cfg = await res.json();

    document.getElementById('sch-enabled').checked = cfg.enabled !== false;
    document.getElementById('sch-frequency').value = cfg.frequency || 'weekly';
    document.getElementById('sch-weekday').value = cfg.weekday ?? 6;
    const h = String(cfg.hour ?? 6).padStart(2,'0');
    const m = String(cfg.minute ?? 0).padStart(2,'0');
    document.getElementById('sch-time').value = `${{h}}:${{m}}`;
    document.getElementById('sch-interval').value = cfg.interval_hours ?? 24;

    document.getElementById('sch-msg').className = 'modal-msg';
    updateScheduleUI();
    document.getElementById('scheduleModal').classList.add('open');
}}

function closeScheduleModal() {{
    document.getElementById('scheduleModal').classList.remove('open');
}}

document.getElementById('scheduleModal').addEventListener('click', function(e) {{
    if (e.target === this) closeScheduleModal();
}});

function updateScheduleUI() {{
    const freq = document.getElementById('sch-frequency').value;
    document.getElementById('sch-weekday-row').style.display = freq === 'weekly' ? '' : 'none';
    document.getElementById('sch-time-row').style.display = freq === 'interval' ? 'none' : '';
    document.getElementById('sch-interval-row').style.display = freq === 'interval' ? '' : 'none';
}}

async function saveSchedule() {{
    const timeParts = document.getElementById('sch-time').value.split(':');
    const payload = {{
        enabled: document.getElementById('sch-enabled').checked,
        frequency: document.getElementById('sch-frequency').value,
        weekday: parseInt(document.getElementById('sch-weekday').value),
        hour: parseInt(timeParts[0]),
        minute: parseInt(timeParts[1]),
        interval_hours: parseInt(document.getElementById('sch-interval').value),
    }};
    const res = await fetch('/api/schedule', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(payload),
    }});
    const data = await res.json();
    const msg = document.getElementById('sch-msg');
    if (res.ok) {{
        msg.className = 'modal-msg success';
        msg.textContent = 'Gespeichert! ' + formatNextRun(data.next_run);
        updateNextRunDisplay(data.next_run);
    }} else {{
        msg.className = 'modal-msg error';
        msg.textContent = data.error || 'Fehler beim Speichern.';
    }}
}}

function formatNextRun(ts) {{
    if (!ts) return '';
    const d = new Date(ts * 1000);
    return 'Nächster Run: ' + d.toLocaleString('de-DE', {{
        weekday:'short', day:'2-digit', month:'2-digit',
        year:'numeric', hour:'2-digit', minute:'2-digit'
    }});
}}

function updateNextRunDisplay(ts) {{
    const el = document.getElementById('nextRunInfo');
    if (el) el.textContent = formatNextRun(ts);
}}

// Load next run info on page load
fetch('/api/schedule').then(r => r.json()).then(cfg => {{
    if (cfg.enabled && cfg.next_run) updateNextRunDisplay(cfg.next_run);
}}).catch(() => {{}});

// ── Admin: add vehicle modal ─────────────────────────────────────────────────
function openAddModal() {{
    document.getElementById('addModal').classList.add('open');
    document.getElementById('add-msg').className = 'modal-msg';
    document.getElementById('add-msg').textContent = '';
}}

function closeAddModal() {{
    document.getElementById('addModal').classList.remove('open');
}}

document.getElementById('addModal').addEventListener('click', function(e) {{
    if (e.target === this) closeAddModal();
}});

async function submitAddVehicle() {{
    const name = document.getElementById('add-name').value.trim();
    const desc = document.getElementById('add-desc').value.trim();
    const platform = document.getElementById('add-platform').value;
    const url = document.getElementById('add-url').value.trim();
    const msg = document.getElementById('add-msg');

    if (!name || !url) {{
        msg.className = 'modal-msg error';
        msg.textContent = 'Name und Such-URL sind Pflichtfelder.';
        return;
    }}

    const res = await fetch('/api/vehicle/add', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{name, description: desc, platform, search_url: url}})
    }});
    const data = await res.json();

    if (res.ok) {{
        msg.className = 'modal-msg success';
        msg.textContent = 'Fahrzeug hinzugefügt! Seite wird neu geladen...';
        setTimeout(() => location.reload(), 1500);
    }} else {{
        msg.className = 'modal-msg error';
        msg.textContent = data.error || 'Fehler beim Hinzufügen.';
    }}
}}
</script>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="Generate price report")
    parser.add_argument("--output", type=str, default=REPORT_DIR, help="Output directory")
    args = parser.parse_args()

    setup_logging()

    os.makedirs(args.output, exist_ok=True)
    output_file = os.path.join(args.output, "index.html")

    conn = get_connection()
    try:
        vehicles = get_vehicles(conn)
        stats = get_vehicle_stats(conn)
        html = generate_html(conn, stats, vehicles)

        with open(output_file, "w") as f:
            f.write(html)

        logger.info("Report generated: %s", output_file)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
