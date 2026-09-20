#!/bin/sh
set -eu
own="${HOSTNAME}.${HEADLESS}"
if [ "$ROLE" = sentinel ]; then
  if [ ! -s /data/sentinel.conf ]; then
    cat > /data/sentinel.conf <<CONF
port 26379
bind 0.0.0.0
protected-mode no
dir /data
sentinel resolve-hostnames yes
sentinel announce-hostnames yes
sentinel announce-ip ${own}
sentinel monitor ${MASTER} ${DATA_PRIMARY} 6379 2
sentinel down-after-milliseconds ${MASTER} 10000
sentinel failover-timeout ${MASTER} 60000
sentinel parallel-syncs ${MASTER} 1
CONF
  fi
  exec valkey-server /data/sentinel.conf --sentinel
fi

# A restart must join the primary agreed by a majority, never blindly restart
# ordinal zero as primary. Persisted Sentinel epochs survive pod replacement.
discover_primary() {
  : > /tmp/votes
  for voter in 0 1 2; do
    result=$(timeout 3 valkey-cli --raw -h "${SENTINEL_PREFIX}-${voter}.${SENTINEL_HEADLESS}" -p 26379 SENTINEL get-master-addr-by-name "$MASTER" 2>/dev/null || true)
    host=$(printf '%s\n' "$result" | sed -n '1p')
    port=$(printf '%s\n' "$result" | sed -n '2p')
    case "$host" in
      "${DATA_PREFIX}-0.${DATA_HEADLESS}"|"${DATA_PREFIX}-1.${DATA_HEADLESS}")
        [ "$port" != 6379 ] || printf '%s\n' "$host" >> /tmp/votes ;;
    esac
  done
  sort /tmp/votes | uniq -c | awk '$1 >= 2 {print $2; exit}'
}
while :; do
  selected=$(discover_primary)
  [ -z "$selected" ] || break
  echo 'Waiting for two Sentinel voters to agree on the session primary.'
  sleep 2
done

if [ "$selected" = "$own" ]; then
  # Losing local durable state is not evidence that every replica is empty.
  # An explicit recovery bootstrap is needed to prevent wiping a surviving copy.
  if [ ! -s /data/appendonlydir/appendonly.aof.manifest ] && [ "$ALLOW_EMPTY_BOOTSTRAP" != true ]; then
    echo 'Refusing empty primary bootstrap; recover a replica or explicitly reset the session set.' >&2
    exit 1
  fi
  set --
else
  set -- --replicaof "$selected" 6379
fi
valkey-server --bind 0.0.0.0 --protected-mode no --dir /data \
  --appendonly yes --appendfsync always --save '' \
  --maxmemory "$MAXMEMORY" --maxmemory-policy noeviction \
  --replica-announce-ip "$own" --replica-announce-port 6379 "$@" &
server=$!
trap 'kill -TERM "$server" 2>/dev/null || true; wait "$server" || true; exit 0' TERM INT
# Bound how long an isolated old primary can continue serving sessions. Allow
# one transient missed poll, then stop; the next start repeats quorum discovery.
misses=0
while kill -0 "$server" 2>/dev/null; do
  sleep 3
  role=$(valkey-cli --raw ROLE 2>/dev/null | sed -n '1p' || true)
  if [ "$role" = master ]; then
    agreed=$(discover_primary)
    if [ "$agreed" != "$own" ]; then
      misses=$((misses + 1))
      if [ "$misses" -ge 2 ]; then
        echo 'Stopping primary without majority Sentinel authority.' >&2
        kill -TERM "$server" 2>/dev/null || true
        wait "$server" || true
        exit 1
      fi
    else
      misses=0
    fi
  else
    misses=0
  fi
done
wait "$server"
