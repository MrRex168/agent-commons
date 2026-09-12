FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md alembic.ini ./
COPY migrations ./migrations
COPY src ./src
COPY scripts ./scripts

RUN python -m pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn agent_commons.main:app --host 0.0.0.0 --port 8000"]
