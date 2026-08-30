# scale.ps1 — launch a polite, parallel, resumable crawl across the 4 associations.
#
# Runs with --ignore-robots because the associations have authorized this
# collection directly. To stay a good partner to them, the crawler still:
#   * identifies you honestly in the User-Agent (set $contact below), and
#   * rate-limits every request (raise $delay to be gentler).
#
#   powershell -ExecutionPolicy Bypass -File .\scale.ps1
#
# Re-run any time to RESUME. --reset-skipped re-queues seeds that a previous
# robots-respecting run had skipped.

$contact = "eli.mai12932@gmail.com"          # <- your contact, shown to the sites
$delay   = 1.5                                # seconds between requests, per process
$ua      = "cattle-graph-crawler/1.0 (authorized; contact: $contact)"

$seeds = @(
  @{ code = "CHIA";  seed = "MA430053" },   # Chianina
  @{ code = "MAINE"; seed = "402303"   },   # Maine-Anjou
  @{ code = "SHORT"; seed = "3790685"  },   # Shorthorn
  @{ code = "ANGUS"; seed = "13054003" }    # Angus
)

foreach ($a in $seeds) {
  $c = $a.code; $s = $a.seed
  $cmd = "python crawl.py --association $c --seed $s " +
         "--out data_$c.jsonl --db frontier_$c.db " +
         "--ignore-robots --reset-skipped --delay $delay " +
         "--user-agent '$ua' -v"
  Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd
  Write-Host "launched $c  (seed $s)  ->  data_$c.jsonl / frontier_$c.db"
}

Write-Host ""
Write-Host "4 crawlers running. They keep going until each frontier is drained."
Write-Host "Load into Neo4j any time (safe while crawling):  .\load_all.ps1"
