#!/bin/bash
# Deployment script for Ubuntu/Debian VM
# Run as root: sudo bash deploy/setup.sh

set -e

echo "=== Vehicle Scraper Setup ==="

# 1. System packages — use python3 (works on Debian Trixie, Ubuntu 22+)
apt update
apt install -y python3 python3-venv python3-pip postgresql postgresql-client

# 2. Create scraper user
if ! id -u scraper &>/dev/null; then
    useradd --system --create-home --shell /bin/bash scraper
    echo "Created user 'scraper'"
fi

# 3. Setup PostgreSQL database
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='scraper'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE ROLE scraper WITH LOGIN PASSWORD 'CHANGE_ME';"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='vehicle_scraper'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE vehicle_scraper OWNER scraper;"

# 4. Deploy code
mkdir -p /opt/mobile-scraper
cp -r . /opt/mobile-scraper/
chown -R scraper:scraper /opt/mobile-scraper

# 5. Python venv
sudo -u scraper bash -c "
    cd /opt/mobile-scraper
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    patchright install chromium
"

# 6. Create .env if not exists
if [ ! -f /opt/mobile-scraper/.env ]; then
    cp /opt/mobile-scraper/.env.example /opt/mobile-scraper/.env
    echo "IMPORTANT: Edit /opt/mobile-scraper/.env with your DB password!"
fi

# 7. Run database migration
sudo -u scraper bash -c "
    cd /opt/mobile-scraper
    source .env
    psql \$DATABASE_URL -f db/migrations/001_initial.sql
"

# 8. Install systemd units
cp deploy/vehicle-scraper.service /etc/systemd/system/
cp deploy/vehicle-scraper.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable vehicle-scraper.timer
systemctl start vehicle-scraper.timer

echo ""
echo "=== Setup complete ==="
echo "1. Edit /opt/mobile-scraper/.env with your DB password"
echo "2. Test manually: sudo -u scraper /opt/mobile-scraper/venv/bin/python /opt/mobile-scraper/main.py --dry-run"
echo "3. Check timer: systemctl list-timers vehicle-scraper.timer"
