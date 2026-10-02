# Colloid engine + evaluator in a Linux container.
#
# This is the full-fidelity judge for macOS and Windows hosts (Docker Desktop, Podman machine,
# WSL2): candidates run under the real Linux sandbox (seccomp + namespaces + cgroups v1/v2) and
# CPU is accounted exactly from cgroups. On those hosts nothing else gives a security boundary
# around untrusted, LLM-generated code. It is also a reproducible Linux bench host.
# See docker/README.md for how to run it (it needs --privileged: it creates namespaces and
# cgroups for every candidate).
FROM ubuntu:24.04

# Optional: extra CA certificates (*.crt) for build hosts behind a TLS-inspecting proxy.
COPY docker/extra-ca/ /usr/local/share/ca-certificates/colloid-extra/
RUN apt-get update -q && apt-get install -y -q --no-install-recommends ca-certificates && update-ca-certificates

COPY . /src/colloid
RUN /src/colloid/scripts/provision-linux.sh && rm -rf /var/lib/apt/lists/* /root/.cache

ENV PATH=/opt/colloid/venv/bin:$PATH \
    COLLOID_STATE=/opt/colloid/state \
    PYTHONUNBUFFERED=1
WORKDIR /src/colloid
ENTRYPOINT ["colloid"]
CMD ["--help"]
