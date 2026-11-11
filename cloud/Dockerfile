# Example image. Build from the repository root so the shared spec/ is included:
#   docker build -f cloud/Dockerfile -t neuxmind-cloud .
#   docker run -p 8800:8800 -e DATABASE_URL=postgresql+psycopg://user:pass@db/neuxmind neuxmind-cloud
# Platform operators: register in the app, verify the e-mail, then run the
# management command against the same database (do NOT rely on an e-mail
# allow-list in production, see cloud/README.md "平台运营员"):
#   docker run --rm -e DATABASE_URL=... neuxmind-cloud python -m neuxmind_cloud grant-operator <email>
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    NEUXMIND_ENV=prod NEUXMIND_SPEC_PATH=/app/spec
WORKDIR /app

COPY cloud/requirements.txt /app/cloud/requirements.txt
RUN pip install --no-cache-dir -r /app/cloud/requirements.txt "psycopg[binary]==3.2.10"

COPY spec/neuxmind_spec /app/spec/neuxmind_spec
COPY cloud/neuxmind_cloud /app/cloud/neuxmind_cloud

RUN useradd --system --uid 10001 neuxmind
USER neuxmind
WORKDIR /app/cloud
EXPOSE 8800
# TLS is terminated by the load balancer / reverse proxy in front of this container.
CMD ["python", "-m", "neuxmind_cloud", "serve", "--host", "0.0.0.0", "--port", "8800"]
