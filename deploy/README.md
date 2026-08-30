# Deploying to Lightsail

Ubuntu 24.04 LTS, 4 GB / 2 vCPU / 80 GB. The Python needs no changes for
Linux -- every Windows-specific call in the tree is already behind an
`os.name == "nt"` guard. What changes is the process supervisor: systemd
replaces the four console windows `scale.ps1` used to open.

## 1. Create the instance

Paste `lightsail-launch.sh` into the **Launch script** box. It installs
Python, Node 22 (muster needs >= 20.6; Ubuntu ships 18), swap, a journal
cap and unattended security upgrades, then stops. It fetches nothing and
contains no secrets -- the launch script stays visible in the Lightsail
console forever.

Then, before anything else:

- **Firewall**: restrict SSH (22) to your own IP. Leave 8787 closed.
- **Static IP**: attach one, or the address changes on every stop/start.
- **Automatic snapshots**: turn them on. Seven rolling dailies is the only
  thing standing between you and re-crawling three months of pedigrees.

## 2. Upload code and crawl state

The crawlers must be **stopped** while you copy. `frontier_*.db` is live
SQLite; a hot copy is a torn database. The JSONL files are safe either way
(`JsonlWriter.write` flushes per record), and the frontier is transactional,
so stopping is cheap -- you lose at most the animal in flight.

```bash
# from the Windows box, crawlers stopped
tar -czf cattle-state.tgz -C /c/cattle-graph \
    data_CHIA.jsonl data_MAINE.jsonl data_SHORT.jsonl \
    frontier_CHIA.db frontier_MAINE.db frontier_SHORT.db \
    status_*.json load_offsets.json

scp -i key.pem cattle-state.tgz ubuntu@<static-ip>:/opt/cattle-graph/
git clone <this repo> /tmp/cg && cp -r /tmp/cg/. /opt/cattle-graph/
tar -xzf /opt/cattle-graph/cattle-state.tgz -C /opt/cattle-graph/
```

Verify the frontiers arrived intact before deleting anything locally:

```bash
for db in /opt/cattle-graph/frontier_*.db; do
  sqlite3 "$db" "PRAGMA integrity_check; SELECT status, COUNT(*) FROM queue GROUP BY status;"
done
```

The `done` counts must match what the Windows box last reported:
**CHIA 103,876 · MAINE 101,778 · SHORT 94,849**.

## 3. Bootstrap and start

```bash
sudo cattle-bootstrap                 # venv + systemd units
sudo nano /etc/cattle-graph.env       # rotate the Aura password first
bash /opt/cattle-graph/deploy/scale.sh
```

## Running it

```bash
bash deploy/status.sh                 # the old four-windows view
journalctl -u crawl@CHIA -f           # one crawler's output
sudo systemctl restart crawl@MAINE    # safe any time
bash deploy/load_all.sh               # push into Neo4j, idempotent
```

## Deliberate choices

- **No `-v`** in the unit. That flag produced ~16 MB of log per crawler per
  day (284 MB over 18 days). journald keeps the rest.
- **No `--reset-skipped`** in the unit. It was a one-off to re-queue entries
  an earlier robots-respecting run had passed over; under `Restart=always`
  it would fire on every restart. Run it by hand if you need it.
- **No `--seed`.** The frontier holds the queue and `crawl.py` drains it.
  Seeds only matter when starting an association from nothing.
- **ANGUS is not enabled.** angus.org serves a JS challenge a plain HTTP
  client cannot clear, so its frontier is empty and the service would
  restart-loop. See the note in `angus.py`.
- **Dashboard on loopback.** It exposes unauthenticated POST endpoints that
  trigger loads and schema changes. Reach it with
  `ssh -L 8787:localhost:8787` rather than opening the port.
- **Credentials via `EnvironmentFile`**, never as command-line arguments --
  arguments are visible in `ps` to every user on the box.

## The published status board

`dashboard_web.py` cannot go on Netlify: it globs local status files and
shells out to `crawl.py` and `loader.py`. Functions are serverless JS with no
access to this box's disk and no way to start a subprocess. So the board is
split in two, which is the arrangement muster already uses:

| | where | what |
|---|---|---|
| read-only board | Netlify function | counts, rates, ETA, staleness |
| controls | crawl box, SSH tunnel | `/api/load`, `/api/schema`, `/api/reconcile` |

The box pushes; nothing polls it. `publish_status.py` runs every 60s from
`cattle-publish.timer` and POSTs to `/api/publish`.

### Status only

`publish_status.py` projects each status file down to an allowlist —
`association, state, done, pending, failed, skipped, rate_per_min, pct,
updated_at`. The `current` block (an animal's name, registration, DOB, sex and
genetic defects) and `pid` never leave the box.

`publish.mjs` independently rejects any board carrying a field outside that
list. The projection is the mechanism; the rejection is there so the guarantee
does not depend on the client behaving.

```bash
./deploy/publish_status.py --dry-run   # see exactly what would be sent
```

### Two secrets, set in Netlify

| variable | who uses it | what it protects |
|---|---|---|
| `CATTLE_INGEST_TOKEN` | the box, `Bearer` | writing to the board |
| `CATTLE_VIEW_PASSWORD` | you, HTTP Basic | reading the board |

Set them in **Site configuration → Environment variables** so they are read at
request time inside the function. A secret in a *build* variable that the
bundler inlines into client JS is public. `CATTLE_INGEST_TOKEN` also goes in
`/etc/cattle-graph.env` on the box, along with `CATTLE_ENDPOINT`.

Both endpoints return **503 when their variable is unset** rather than falling
open. A typo'd variable name breaks the site loudly instead of quietly
publishing it. `netlify/functions/auth.test.mjs` asserts that, plus the
constant-time compare and the field allowlist:

```bash
npm test
```

Any username works at the Basic auth prompt — the password is the secret.
