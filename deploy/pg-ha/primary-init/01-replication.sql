-- 本地主从演练用复制角色（仅 deploy/docker-compose.pg-ha.yml；凭据仅 dev）
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'replicator') THEN
    CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD 'aip-dev-only';
  END IF;
END
$$;
