FROM python:3.13-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
# psycopg2-binary ships its own libpq, so no build toolchain or libpq-dev is
# needed — dropping the apt layer removes ~90MB and a network round trip.
RUN pip install --no-cache-dir -r requirements.txt

# .dockerignore keeps .env, __pycache__ and docs out of the image.
COPY . .

RUN chmod +x /app/entrypoint.sh

# Drop privileges: nothing here needs root at runtime.
RUN useradd --system --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8001

# Run `alembic upgrade head` before the server starts (see entrypoint.sh).
ENTRYPOINT ["/app/entrypoint.sh"]
# --proxy-headers makes uvicorn read X-Forwarded-For from the nginx front end,
# so request.client.host is the real caller. Without it every request proxied by
# nginx looks like it came from the nginx container, and the per-IP rate limiter
# lumps all admin-UI users into a single 60-req/min bucket.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
