FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first (better layer cache)
COPY requirements.txt .
RUN pip install -r requirements.txt

# App source
COPY cell_lookup.py bot.py ./

# Cache directory (mounted as volume in compose)
RUN mkdir -p /app/cache

# Run as non-root
RUN useradd --system --uid 1000 --home /app appuser \
 && chown -R appuser:appuser /app
USER appuser

CMD ["python", "-u", "bot.py"]
