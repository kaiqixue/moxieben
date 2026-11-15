FROM python:3.11-slim-bookworm@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates fonts-arphic-uming \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 moxieben \
    && useradd --uid 10001 --gid 10001 --no-create-home moxieben
WORKDIR /app
COPY requirements-server.txt .
RUN python -m pip install --no-cache-dir -r requirements-server.txt
COPY *.py ./
COPY templates/ ./templates/
COPY static/ ./static/
RUN rm -f test_*.py && mkdir -p /var/lib/moxieben && chown 10001:10001 /var/lib/moxieben
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).read()"
CMD ["gunicorn", "--config", "gunicorn.conf.py", "wsgi:app"]
