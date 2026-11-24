# syntax=docker/dockerfile:1.4
FROM python:3.14-slim AS builder

# apt runs as root throughout (APT::Sandbox::User), and the base image's _apt-owned partial directories become root's,
# so no build step needs CAP_DAC_OVERRIDE: root never has to write files another user owns. Hosts that withhold that
# capability from containers (for example SELinux with virt_sandbox_use_all_caps off) can then build this image.
RUN echo 'APT::Sandbox::User "root";' > /etc/apt/apt.conf.d/99-sandbox-root \
    && chown root:root /var/lib/apt/lists/partial /var/cache/apt/archives/partial \
    && apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    gcc \
    libc6-dev \
    libpq-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /venv
ENV PATH="/venv/bin:$PATH"

# COPY, not RUN --mount=source: podman (which builds the production image, .woodpecker.yaml) leaves a mounted file
# out of the layer's cache key, so an edited requirements file reused the old install. A COPY keys the install on
# the file's contents. The same holds for requirements-test.txt in `test` below.
COPY requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip,id=pip-py314-slim \
    pip install -r /tmp/requirements.txt gunicorn

# Test dependencies go here and NOWHERE ELSE. This stage is what
# compose.test.yaml builds; `runtime` below copies /venv from `builder`, which
# never sees requirements-test.txt, so nothing INSTALLED BY THIS STAGE reaches a
# production image. (That is narrower than "no test package in production":
# c2pa-python declares pytest as a runtime dependency, so pytest is in the
# production venv either way. Pre-existing, pure Python, no ARM64 risk.)
#
# That separation is the point: atheris (the fuzzing engine) publishes no
# aarch64 wheel, so an entry for it in requirements.txt makes
# `pip install -r requirements.txt` build it from source -- needing clang with
# libFuzzer -- and fail outright on any ARM64 host.
#
# This stage must stay ABOVE `runtime`: a Dockerfile with no --target builds the
# LAST stage, and that has to remain the production image.
FROM builder AS test

# git: FediTest installs from a git commit, and some tests ask git which files are tracked.
COPY requirements-test.txt /tmp/requirements-test.txt
RUN --mount=type=cache,target=/root/.cache/pip,id=pip-py314-slim \
    apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install -r /tmp/requirements-test.txt

FROM python:3.14-slim AS runtime

ARG TARGETARCH
ARG SUPERCRONIC_VERSION=v0.2.51


# apt as root, as in builder above, and the python user made without copying skeleton files into a home it already
# owns (that copy, too, needs CAP_DAC_OVERRIDE): same uid/gid 1000, same /home/python, mode 700.
# apt-get upgrade: the base image's Debian packages as patched since it was built (a zlib fix was waiting in one).
RUN adduser --disabled-password --gecos "" --no-create-home python && \
    install -d -m 700 -o python -g python /home/python && \
    echo 'APT::Sandbox::User "root";' > /etc/apt/apt.conf.d/99-sandbox-root \
    && chown root:root /var/lib/apt/lists/partial /var/cache/apt/archives/partial \
    && apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
    tini \
    ffmpeg \
    curl \
    ca-certificates \
    procps \
    postgresql-client \
    tesseract-ocr \
    tesseract-ocr-eng && \
    curl -fsSLO "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH}" && \
    case "${TARGETARCH}" in \
      amd64) echo "1692066e350a88ff4d7d6474fe3c7a6c41ec5e89  supercronic-linux-${TARGETARCH}" ;; \
      arm64) echo "9d4c2000493215a1898502b2fc2dada3e8ea8c7a  supercronic-linux-${TARGETARCH}" ;; \
      *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac | sha1sum -c - && \
    chmod +x "supercronic-linux-${TARGETARCH}" && \
    mv "supercronic-linux-${TARGETARCH}" /usr/local/bin/supercronic && \
    rm -rf /var/lib/apt/lists/* /tmp/*

COPY --from=builder /venv /venv
ENV PATH="/venv/bin:$PATH"

COPY --chown=python:python . /app

WORKDIR /app

# As the owner of /app: root writing into files python owns would need CAP_DAC_OVERRIDE (see builder above).
USER python
RUN (pybabel compile -d app/translations || true) && \
    chmod u+x ./entrypoint.sh ./entrypoint_celery.sh ./entrypoint_async.sh ./entrypoint_jetstream.sh

EXPOSE 5000
ENV CRON="false"

LABEL org.opencontainers.image.authors="rimu"
LABEL org.opencontainers.image.source="https://codeberg.org/rimu/pyfedi"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"
LABEL org.opencontainers.image.description="A Lemmy/Mbin alternative written in Python with Flask."

HEALTHCHECK --interval=60s --retries=2 --timeout=10s CMD curl -ILfSs http://localhost:5000/health >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
