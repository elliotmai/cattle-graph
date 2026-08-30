#!/usr/bin/env bash
#
# Lightsail launch script -- paste into the "Launch script" box on the
# create-instance page. Blueprint: Ubuntu 24.04 LTS. Plan: 4 GB / 2 vCPU.
#
# Runs once as root on first boot. Output lands in
#   /var/log/cloud-init-output.log  and  /var/log/cattle-launch.log
#
# IT CONTAINS NO SECRETS ON PURPOSE. The launch script stays visible in the
# Lightsail console for the life of the instance, so it only writes a
# placeholder env file; you fill in the Aura credentials over SSH afterwards.
#
# It also does not fetch the application. Nothing is cloned or downloaded --
# the machine is prepared, and you upload code and crawl state yourself, then
# run `cattle-bootstrap`.

set -euxo pipefail
exec > >(tee -a /var/log/cattle-launch.log) 2>&1
echo "=== cattle-graph launch script starting: $(date -u) ==="

export DEBIAN_FRONTEND=noninteractive
# cloud-init may still be holding the dpkg lock this early in boot; wait for it
# rather than racing and failing.
APT="apt-get -o DPkg::Lock::Timeout=600 -y"

timedatectl set-timezone UTC

$APT update
$APT install --no-install-recommends \
    python3-venv python3-pip \
    git rsync curl ca-certificates gnupg sqlite3 \
    unattended-upgrades

# --- Node for muster -------------------------------------------------------
# muster needs Node >= 20.6 and Ubuntu 24.04 ships 18.19 in the archive, which
# fails in ways that never mention Node. Add NodeSource with an explicitly
# fetched keyring rather than piping their setup script into a root shell.
curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
    | gpg --dearmor -o /usr/share/keyrings/nodesource.gpg
echo "deb [signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
    > /etc/apt/sources.list.d/nodesource.list
$APT update
$APT install nodejs
node -v

# --- swap ------------------------------------------------------------------
# 4 GB is ample for three crawlers, but Lightsail images ship with no swap at
# all, so a bad day becomes an OOM kill instead of a slow minute.
if ! swapon --show | grep -q .; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# --- journal size cap ------------------------------------------------------
# Crawler output goes to journald. Default cap is 10% of disk; 1 GB is plenty
# and leaves the 80 GB for actual data.
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/99-cattle.conf <<'EOF'
[Journal]
SystemMaxUse=1G
EOF
systemctl restart systemd-journald

# --- unattended security upgrades -----------------------------------------
# The crawlers are systemd services with Restart=always and WantedBy=
# multi-user.target, so an unattended reboot costs one in-flight animal. That
# is a better trade than an unpatched kernel facing the internet for three
# months. Set Automatic-Reboot to "false" if you disagree.
cat > /etc/apt/apt.conf.d/52unattended-cattle <<'EOF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:00";
EOF
systemctl enable --now unattended-upgrades

# NOTE: deliberately no ufw. Lightsail has its own firewall in front of the
# instance -- configure it there. A second firewall inside the box mostly
# creates opportunities to lock yourself out of SSH.

# --- application directories ----------------------------------------------
install -d -o ubuntu -g ubuntu -m 0755 /opt/cattle-graph
install -d -o ubuntu -g ubuntu -m 0755 /opt/muster

# --- credentials placeholder ----------------------------------------------
# 0640 root:ubuntu -- readable by the services, not world-readable, and passed
# to the loader through the environment so it never appears in `ps` output.
if [ ! -f /etc/cattle-graph.env ]; then
cat > /etc/cattle-graph.env <<'EOF'
# Fill these in over SSH. Rotate the Aura password first -- the old one was
# exposed in a process command line on the previous machine.
NEO4J_URI=neo4j+s://CHANGEME.databases.neo4j.io
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=CHANGEME
NEO4J_DATABASE=neo4j

# Where the status board lives, and the token that lets this box write to it.
# The view password is a Netlify env var, not here -- this box only publishes.
CATTLE_ENDPOINT=https://CHANGEME.netlify.app/api/publish
CATTLE_INGEST_TOKEN=CHANGEME

# Crawl politeness. Do not lower the delay: one request per ~1.5s per host is
# the agreed ceiling, and a datacenter IP is far more visible than a home one.
CRAWL_DELAY=1.5
CRAWL_UA=cattle-graph-crawler/1.0 (authorized; contact: eli.mai12932@gmail.com)
EOF
chown root:ubuntu /etc/cattle-graph.env
chmod 0640 /etc/cattle-graph.env
fi

# --- bootstrap helper, run after you upload the code ----------------------
cat > /usr/local/bin/cattle-bootstrap <<'EOF'
#!/usr/bin/env bash
# Run once, after uploading /opt/cattle-graph. Builds the venv and installs
# the systemd units from the repo's deploy/ directory, so the units stay
# version-controlled in one place instead of being duplicated here.
set -euo pipefail
APP=/opt/cattle-graph

[ -f "$APP/crawl.py" ] || { echo "no code at $APP -- upload it first"; exit 1; }

sudo -u ubuntu python3 -m venv "$APP/.venv"
sudo -u ubuntu "$APP/.venv/bin/pip" install --quiet --upgrade pip
sudo -u ubuntu "$APP/.venv/bin/pip" install --quiet -r "$APP/requirements.txt"

install -m 0644 "$APP/deploy/crawl@.service"           /etc/systemd/system/
install -m 0644 "$APP/deploy/cattle-dashboard.service" /etc/systemd/system/
install -m 0644 "$APP/deploy/cattle-publish.service"   /etc/systemd/system/
install -m 0644 "$APP/deploy/cattle-publish.timer"     /etc/systemd/system/
systemctl daemon-reload

echo
echo "venv built, units installed."
grep -q CHANGEME /etc/cattle-graph.env \
  && echo "NEXT: edit /etc/cattle-graph.env (still has CHANGEME), then run:" \
  || echo "NEXT: run:"
echo "  bash $APP/deploy/scale.sh"
EOF
chmod 0755 /usr/local/bin/cattle-bootstrap

echo "=== launch script finished: $(date -u) ==="
echo "Next: upload code + crawl state to /opt/cattle-graph, then run cattle-bootstrap"
