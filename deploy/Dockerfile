# 控制面与数据面共用一个镜像（architecture.md 12.1、13）；启动命令决定角色。
# 运行时需要的契约文件（docs/specs 的 JSON Schema、policies 的 rego）一并打包，路径与仓库一致。
# 控制台静态文件（console/dist），由控制面在 /console 下同源提供
FROM node:22-slim AS console
WORKDIR /console
COPY console/package.json console/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY console ./
RUN npm run build

FROM python:3.12-slim AS build
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
RUN pip install --no-cache-dir uv==0.11.14
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
RUN useradd --uid 10001 --create-home aip
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY src ./src
COPY docs/specs ./docs/specs
COPY policies ./policies
COPY migrations ./migrations
COPY alembic.ini ./
COPY --from=console /console/dist ./console/dist
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/src PYTHONUNBUFFERED=1 AIP_DATA_DIR=/var/lib/aip \
    AIP_CONSOLE_DIST=/app/console/dist
RUN mkdir -p /var/lib/aip && chown aip /var/lib/aip
USER 10001
EXPOSE 8080
# 默认数据面；控制面：uvicorn aip.control_plane.app:app；迁移：alembic upgrade head
CMD ["uvicorn", "aip.data_plane.app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
