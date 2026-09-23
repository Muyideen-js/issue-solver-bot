# The solver shells out to the real git binary (SolverWorkspace._git raises
# "Git is not installed" without it), and language buildpacks do not reliably
# ship git in the runtime image — so build from this Dockerfile, not a buildpack.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Clones land in a TemporaryDirectory under /tmp, so the app itself needs no
# writable project directory; run unprivileged anyway.
RUN useradd --create-home --uid 10001 solver && chown -R solver:solver /app
USER solver

# The host injects PORT; 8080 is the fallback for a plain `docker run`.
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
