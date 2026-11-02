# ci-auditor image (WI-3087). Built from the repository root:
#   docker build -f auditor/Dockerfile -t ci-auditor .
# Contents: the auditor source, ci-standard/ (rules and actionlint config at
# this tag), PyYAML (hash-pinned) and actionlint (checksum-verified).
FROM python:3.13-slim-bookworm@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f

ARG ACTIONLINT_VERSION=1.7.7
ARG ACTIONLINT_SHA256=023070a287cd8cccd71515fedc843f1985bf96c436b7effaecce67290e7e0757

COPY --chmod=644 auditor/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes --no-deps -r /tmp/requirements.lock \
 && python3 -c "import urllib.request; urllib.request.urlretrieve('https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}/actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz', '/tmp/al.tgz')" \
 && echo "${ACTIONLINT_SHA256}  /tmp/al.tgz" | sha256sum -c - \
 && tar -xzf /tmp/al.tgz -C /usr/local/bin actionlint \
 && rm -f /tmp/al.tgz /tmp/requirements.lock \
 && apt-get -qq update && apt-get -qq install -y --no-install-recommends git >/dev/null && rm -rf /var/lib/apt/lists/*

COPY --chmod=755 auditor/src /opt/ci-auditor/src
COPY --chmod=755 ci-standard /opt/ci-auditor/ci-standard

ENV PYTHONPATH=/opt/ci-auditor/src \
    CI_STANDARD_DIR=/opt/ci-auditor/ci-standard \
    ACTIONLINT=/usr/local/bin/actionlint \
    PYTHONUNBUFFERED=1

USER 65534:65534
EXPOSE 9803
ENTRYPOINT ["python3", "-m", "ci_auditor"]
CMD ["serve"]
