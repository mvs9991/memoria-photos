# Memoria — local photo intelligence.
#
# The image carries the code and its dependencies. It deliberately does NOT carry the
# model weights (~1.1 GB, and the face models are non-commercial): those download into
# the data volume on first use, so the image stays small and redistributable.
#
# Your photos are mounted READ-ONLY. That is not a convention here — never altering an
# original is the project's core guarantee, and :ro makes the kernel enforce it.

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PHOTOINTEL_DATA=/data

# opencv-python-headless still links libglib and libxcb even in the headless build —
# importing cv2 fails without them. The other imaging wheels are self-contained.
# curl is here only so the container can report its own health; gosu lets the
# entrypoint drop privileges after fixing /data ownership.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libglib2.0-0 libxcb1 curl gosu \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU PyTorch by default: it is a fraction of the size and works everywhere. For an
# NVIDIA GPU, build with --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu124
# and run with `--gpus all`.
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
RUN pip install torch torchvision --index-url ${TORCH_INDEX}

# Dependencies first, so editing source does not reinstall them.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY photointel/ ./photointel/
COPY web/dist/ ./web/dist/
COPY README.md LICENSE ./
COPY docker-entrypoint.sh /usr/local/bin/
# Strip CRLF as well as chmod: the file may arrive from a Windows checkout or a zip,
# and a shebang ending in CR fails with a confusing "no such file or directory".
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh && chmod +x /usr/local/bin/docker-entrypoint.sh

# A non-root user to run as. It must never be able to write the photos even if the
# mount were read-write by mistake.
#
# USER is deliberately not set: a *bind-mounted* /data arrives owned by root, so the
# entrypoint fixes ownership as root and then drops to this user with gosu. (A named
# volume inherits the image's ownership, which is why this only bites on bind mounts —
# and why it passed locally but failed in CI.)
RUN useradd --create-home --uid 10001 memoria \
 && mkdir -p /data /photos \
 && chown -R memoria:memoria /data /app

VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8765/api/stats || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
# 0.0.0.0 so the port is reachable from outside the container; publish it only to the
# interface you want (compose binds it to localhost by default).
CMD ["python", "-m", "photointel", "serve", "--host", "0.0.0.0", "--port", "8765"]
