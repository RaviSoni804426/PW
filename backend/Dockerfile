# Central backend for PW OngoingRec.
#
# Build context is this directory, not the repository root:
#   docker build -t ongoingrec-backend backend/
#   docker run -p 8000:8000 -v ongoingrec-data:/data ongoingrec-backend
#
# It runs open: no credentials to set, and anyone with the URL can fetch a
# clip. Set ADMIN_API_KEY to gate the admin surface if that changes.
#
# The recording agent is NOT in this image -- it lives in ../agent and is a
# Windows service that runs on a counsellor's laptop. This container only
# receives what those laptops send it.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    PORT=8000

WORKDIR /app

# Dependencies first, so a code change does not re-run the install layer.
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/

# Recordings and the device database live here. Mount a named volume over it:
# without one, every redeploy starts with no enrolled devices, and agents whose
# tokens are no longer recognised have to re-enrol before clips can flow again.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# python rather than curl: the slim image has no curl, and adding one just for
# a health check is a package and a CVE surface that earns nothing.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz', timeout=4).status==200 else 1)"

CMD ["python", "-m", "backend"]
