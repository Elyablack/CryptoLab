#!/bin/bash
set -euo pipefail
read -r sha
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || exit 2
exec 9>/run/lock/cryptolab-backup.lock
flock -w 60 9
cd /srv/cryptolab
release=/srv/cryptolab/releases/$sha
mkdir -p "$release"
curl --fail --location --proto '=https' --tlsv1.2 "https://api.github.com/repos/Elyablack/CryptoLab/tarball/$sha" | tar -xz --strip-components=1 -C "$release"
docker build --label org.opencontainers.image.revision="$sha" -t "cryptolab-paper:$sha" "$release"
docker run --rm --network none "cryptolab-paper:$sha" paper --help </dev/null >/dev/null
stamp=$(date -u +%Y%m%dT%H%M%SZ)
record=/srv/cryptolab/deployments/$stamp-$sha
mkdir -p "$record"
cp .env "$record/previous.env"
cp compose.yaml "$record/compose.yaml"
cp CURRENT_COMMIT "$record/previous-commit"
docker compose stop --timeout 180 paper-bot
# Always resume the old release if preparation fails before switching.
trap 'docker compose start paper-bot >/dev/null 2>&1 || true' EXIT
for db in paper_state market_data; do
 docker compose run -T --rm --no-deps paper-bot paper backup --source-database "/app/data/$db.db" --output "/app/backups/$db-pre-$stamp.db" </dev/null
done
# Refuse schema-changing releases; such upgrades need a separate migration plan.
old=$(sed -n 's/^CRYPTOLAB_IMAGE=//p' .env)
for image in "$old" "cryptolab-paper:$sha"; do
 docker run --rm --network none --entrypoint python "$image" -c 'import hashlib; from cryptolab.storage.paper_state_repository import SCHEMA_SQL; from cryptolab.storage.sqlite_repository import CREATE_CANDLES_TABLE_SQL; print(hashlib.sha256((SCHEMA_SQL+CREATE_CANDLES_TABLE_SQL).encode()).hexdigest())' </dev/null
done > "$record/schema-hashes"
[[ $(sort -u "$record/schema-hashes" | wc -l) -eq 1 ]] || { echo 'Schema differs; manual migration required'; exit 1; }
image=$(docker image inspect "cryptolab-paper:$sha" --format '{{.Id}}')
sed "s/^CRYPTOLAB_IMAGE=.*/CRYPTOLAB_IMAGE=$image/" .env > .env.next
chmod 600 .env.next
mv .env.next .env
rollback() {
 docker compose stop --timeout 180 paper-bot || true
 cp "$record/previous.env" .env
 docker compose up -d --no-build
 echo 'Deployment failed; previous image restored. Databases retained.'
}
trap 'rollback' ERR
trap - EXIT
docker compose up -d --no-build
healthy=false
for attempt in $(seq 1 24); do
 if docker compose exec -T paper-bot cryptolab paper health --mode multi --session-id btc-eth-rotation --state-database /app/data/paper_state.db </dev/null; then healthy=true; break; fi
 sleep 5
done
[[ "$healthy" == true ]]
printf '%s\n' "$sha" > CURRENT_COMMIT
printf '%s\n' "$image" > "$record/image-id"
trap - ERR
echo "Deployed $sha"
