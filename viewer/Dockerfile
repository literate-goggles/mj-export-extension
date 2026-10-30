# Hosted MJ Export Viewer: python3 -m mjviewer --hosted, configured entirely from the
# environment (README.md, "Hosted mode"). Build from viewer/:
#   docker build -t mj-export-viewer .
FROM python:3.12.14-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Pillow renders the thumbnails; everything else is the standard library.
RUN pip install --no-cache-dir Pillow==12.3.0 \
 && useradd --system --uid 10001 --user-group --create-home --home-dir /var/lib/mjv --shell /usr/sbin/nologin mjv

WORKDIR /opt/mjviewer
COPY mjviewer/ ./mjviewer/

# The home folder is outside /home on purpose: a platform may mount shared storage over
# /home. The cache uses /home/mjv-cache when that mount is writable for this user and
# falls back to ~/.cache/mj-export-viewer (logged) when it is not.
ENV HOME=/var/lib/mjv \
    PORT=8000
USER mjv
EXPOSE 8000
CMD ["python3", "-m", "mjviewer", "--hosted"]
