# Everything is Python stdlib — no pip install, no build step, no compiler.
FROM python:3.11-slim

# IST so the nightly sync fires at 22:00 local, and so shift times in logs
# match what deployers see in MDS
ENV TZ=Asia/Kolkata PYTHONUNBUFFERED=1
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
 && apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY *.py ./
COPY static/ ./static/
COPY sql/ ./sql/

# history.db lives here. Mount a volume at /app/data or the 30-day backfill
# repeats (~7 min) on every restart.
RUN mkdir -p /app/data
VOLUME /app/data

EXPOSE 8770
HEALTHCHECK --interval=60s --timeout=10s --start-period=600s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${PORT:-8770}/health || exit 1

CMD ["python3", "run.py"]
