#!/bin/sh
# 允许容器网内 streaming replication（仅本地演练）
set -eu
HBA="${PGDATA}/pg_hba.conf"
if ! grep -q 'replication replicator' "$HBA" 2>/dev/null; then
  printf '\nhost replication replicator 0.0.0.0/0 scram-sha-256\n' >> "$HBA"
fi
