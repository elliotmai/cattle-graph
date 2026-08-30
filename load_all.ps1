# load_all.ps1 — load every data_*.jsonl into Neo4j, deduped on International ID.
# Safe to run repeatedly and while the crawlers are still going (it's idempotent).
# Assumes your Aura creds are already in $env:NEO4J_URI / _USERNAME / _PASSWORD.

if (-not $env:NEO4J_URI) { throw "Load your Neo4j creds first (see RUN.md step 0)." }

Get-ChildItem -Filter "data_*.jsonl" | ForEach-Object {
  Write-Host "=== loading $($_.Name) ==="
  python loader.py $_.FullName --neo4j `
    --uri $env:NEO4J_URI --user $env:NEO4J_USERNAME --password $env:NEO4J_PASSWORD --insecure
}
Write-Host "done. Check counts in the Aura Browser:  MATCH (a:Animal) RETURN count(a);"
