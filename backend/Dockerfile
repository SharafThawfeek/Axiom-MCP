FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway injects PORT at runtime; fall back to 8000 for local `docker build`/`docker run`.
ENV PORT=8000
CMD uvicorn axiom_debug.main:app --host 0.0.0.0 --port ${PORT}
