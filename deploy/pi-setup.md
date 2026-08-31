# Running the crawl on a Raspberry Pi

The DigitalBeef breed hosts — **chianina** especially — sit behind Cloudflare,
which scores datacenter networks (AWS / Lightsail) far more harshly than an
ordinary home ISP address. Running the crawl from a Pi on your home connection
puts it behind a **residential IP**, which does more to avoid blocking than any
IP-rotation scheme you could build in the cloud.

The trade-off is that it's **one** IP — your home address — and it does not
rotate. So the strategy is *keep that one IP clean by behaving well*, which is
exactly what this crawler is built for (per-request delay, resumable frontier,
page archive so nothing is fetched twice). Don't crank the delay down; getting
your home IP blocked also affects the browsers in your house.

Everything below runs over your own SSH session to the Pi. Replace
`pi@raspberrypi.local` with your Pi's user and host.

---

## 0. Prerequisites

- Raspberry Pi OS (or any Debian/Ubuntu-based ARM Linux), 64-bit recommended.
- The Pi reachable over SSH, and outbound internet from it.
- Python 3.9+ and git. (Install in step 2.)

Nothing here needs Neo4j on the Pi — the Pi only *crawls*, writing
`data_CHIA.jsonl`. Loading into the graph happens elsewhere (step 6).

## 1. SSH in

```bash
ssh pi@raspberrypi.local
```

## 2. Install system packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git sqlite3 rsync
```

## 3. Get the code into /opt/cattle-graph

Same layout as the Lightsail box, so the systemd unit paths match.

```bash
sudo mkdir -p /opt/cattle-graph
sudo chown "$(id -un)":"$(id -gn)" /opt/cattle-graph
```

This is a **private repo**, so an unauthenticated `https://` clone fails with
`Password authentication is not supported`. Pick one:

**a) SSH key (recommended — doesn't expire, best for a long-running box):**

```bash
ssh-keygen -t ed25519 -C "pi-cattle-graph" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub     # add this to GitHub: repo Settings ->
                              # Deploy keys (read-only), or account SSH keys
ssh -T git@github.com         # accept the host key the first time
git clone git@github.com:elliotmai/cattle-graph.git /tmp/cg
```

**b) Fine-grained token over HTTPS (quick, but expires):** create a token with
Contents: Read on `elliotmai/cattle-graph`, then

```bash
git clone https://<YOUR_TOKEN>@github.com/elliotmai/cattle-graph.git /tmp/cg
```

**c) No GitHub auth on the Pi** — copy the tree from a machine that already has
it and skip straight to step 4:

```bash
# from the other machine:
scp -r /path/to/cattle-graph/. pi@raspberrypi.local:/opt/cattle-graph/
```

For (a) or (b), finish by checking out this branch and copying it in:

```bash
git -C /tmp/cg checkout claude/lightsail-dynamic-ip-switching-bqlv4f
cp -r /tmp/cg/. /opt/cattle-graph/
```

## 4. Python virtualenv

The crawler needs only `requests` and `beautifulsoup4` (both have ARM wheels);
`neo4j` in requirements.txt installs fine too but is unused on the Pi.

```bash
cd /opt/cattle-graph
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# Smoke-test the parser with no network:
.venv/bin/python test_parsers.py
```

## 5. Crawl state: resume the existing run, or start fresh

**Recommended — move the running CHIA crawl onto the Pi.** Continue exactly
where your current crawl is, now from the residential IP. `frontier_*.db` is
live SQLite, so **stop the crawler on the old box first** — a hot copy is a torn
database.

```bash
# On the box currently crawling CHIA:
sudo systemctl stop crawl@CHIA          # (or however it runs there)

# Copy frontier + output to the Pi (run from the old box, or pull from the Pi):
scp frontier_CHIA.db data_CHIA.jsonl pi@raspberrypi.local:/opt/cattle-graph/

# On the Pi, confirm the frontier arrived intact:
sqlite3 /opt/cattle-graph/frontier_CHIA.db \
  "PRAGMA integrity_check; SELECT status, COUNT(*) FROM queue GROUP BY status;"
```

The `done` count should match what the old box last reported. Do **not** restart
CHIA on the old box — you want exactly one crawler per frontier.

**Or — start CHIA from scratch** (empty frontier). Seed it once; the unit has no
`--seed` because from then on `crawl.py` drains the frontier itself.

```bash
cd /opt/cattle-graph
.venv/bin/python crawl.py --association CHIA --seed MA430053 \
    --out data_CHIA.jsonl --db frontier_CHIA.db \
    --add-seeds-only
```

## 6. Environment file (delay + contact UA)

The unit reads `CRAWL_DELAY` and `CRAWL_UA` from `/etc/cattle-graph.env`. On a
home line, keep the delay polite and put a **real contact address** in the UA —
it's the courteous thing and it's what associations ask for.

```bash
sudo tee /etc/cattle-graph.env >/dev/null <<'EOF'
CRAWL_DELAY=3.0
CRAWL_UA=cattle-graph crawler (you@example.com)
EOF
sudo chmod 600 /etc/cattle-graph.env
```

## 7. Install and start the service

The unit template ships with a `__CRAWL_USER__` placeholder; substitute your
actual Pi login so systemd runs the crawl as you (not root).

```bash
cd /opt/cattle-graph
sed "s/__CRAWL_USER__/$(id -un)/g" deploy/crawl-pi@.service \
  | sudo tee /etc/systemd/system/crawl-pi@.service >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now crawl-pi@CHIA
```

Watch it work:

```bash
journalctl -u crawl-pi@CHIA -f
```

You should see animals being fetched at your `CRAWL_DELAY` pace. If Cloudflare
still challenges (403/429), the crawler backs off and, after 5 in a row, exits
75 and stays stopped rather than hammering your home IP — see Troubleshooting.

## 8. Get the crawled data into Neo4j

The Pi writes `data_CHIA.jsonl` and archives pages under `html/`. Two ways to
load it:

- **Ship the JSONL to wherever your loader/dashboard runs** (keeps Neo4j
  credentials off the Pi — matches the split the rest of `deploy/` uses):

  ```bash
  rsync -avz /opt/cattle-graph/data_CHIA.jsonl \
      user@loader-box:/opt/cattle-graph/data_CHIA.jsonl
  ```

  The dashboard's auto-loader tails the file into Neo4j from there. A cron on
  the Pi can rsync every few minutes.

- **Or load directly from the Pi to Neo4j Aura** (internet reaches Aura fine):

  ```bash
  cd /opt/cattle-graph
  .venv/bin/python loader.py data_CHIA.jsonl --neo4j \
      --uri "neo4j+s://<your-aura-host>" --user neo4j --password '<pw>'
  ```

  This puts the Aura password on the Pi; prefer the rsync path if you'd rather
  it live in one place.

## 9. Add the other DigitalBeef breeds (optional)

The unit is a template, so more breeds are just more instances — each gets its
own frontier and output:

```bash
sudo systemctl enable --now crawl-pi@MAINE crawl-pi@SHORT
```

Leave **ANGUS** off: angus.org serves a JS challenge a plain HTTP client can't
clear (noted in `angus.py` and the Lightsail README), so its frontier stays
empty and the service would restart-loop.

---

## Pi-specific notes

- **No `--pin-ip` needed.** That flag was a workaround for cloud environments
  that couldn't resolve the DigitalBeef subdomains. Your home DNS resolves them
  normally, so connect straight through. Keep it only as a fallback if a name
  briefly stops resolving while the site is up.
- **A residential IP is the big win, not a cure-all.** Cloudflare can still
  challenge on request pattern. If it does, the next levers are a realistic
  `CRAWL_UA` and a *higher* `CRAWL_DELAY` — not more IPs.
- **Your home IP may change** when the ISP re-leases it. That's fine here: the
  crawl doesn't care what its source IP is, and a new residential IP is just as
  trusted as the old one. The frontier is on disk, so a reconnect loses nothing.
- **SD-card wear.** `--html-store` and the SQLite frontier write steadily. On a
  long full-breed crawl that's real write volume for an SD card. If you have a
  USB SSD, put `/opt/cattle-graph` on it. Otherwise back up `frontier_CHIA.db`
  periodically (step 5's scp, in reverse) so a dead card doesn't cost the crawl.
- **Power/uptime.** The frontier makes the crawl fully resumable, and the unit
  is `Restart=always` with `WantedBy=multi-user.target`, so it comes back after
  a reboot or power blip on its own.

## Troubleshooting

- **Service won't start / wrong user:** `systemctl status crawl-pi@CHIA`. If it
  complains about the user, re-run the `sed` in step 7 — the placeholder wasn't
  substituted.
- **Exited status=75 and stays down:** the host refused us
  (`--max-blocked` 403/429s in a row). This is the deliberate stop, not a bug.
  Wait for the block to clear (raise `CRAWL_DELAY` first), then
  `sudo systemctl start crawl-pi@CHIA`.
- **Nothing being fetched but no errors:** the frontier may be drained (all
  `done`). Check with the `sqlite3 ... GROUP BY status` query in step 5; add
  seeds or an `--enumerate` sweep to fill gaps (see `CRAWLER.md`).
- **Live status:** `status_CHIA.json` holds a heartbeat, and `deploy/state.py`
  summarizes crawl progress if you want the same view the Lightsail box uses.
