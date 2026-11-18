# Token validator of the TES gateway (TESP-src issue #26). Built by the
# tesp-api stack from this directory, which playbook.yml copies to
# tesp_validator_dir. Bump the base image deliberately:
#   docker buildx imagetools inspect python:<version>-slim-trixie
FROM python:3.13.15-slim-trixie@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Every package and its hash are pinned (pip-compile --generate-hashes from
# requirements.in); wheels only, so nothing is compiled at build time.
COPY requirements.txt /app/requirements.txt
RUN pip install --require-hashes --only-binary=:all: --no-deps \
        -r /app/requirements.txt

COPY tes_validator.py /app/tes_validator.py

USER 65534:65534
EXPOSE 8081
CMD ["python", "/app/tes_validator.py", "--config", "/etc/tes-validator/config.json"]
