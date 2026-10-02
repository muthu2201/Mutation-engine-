# Running Colloid in a Linux container

Colloid's search side (core, operators, data lake, reports, LLM calls) runs natively on
Linux, macOS and Windows. Its **judge** (the evaluator) needs two Linux kernel features to be
trustworthy:

1. a **security boundary** around untrusted candidate code (seccomp, namespaces, an
   unprivileged uid), and
2. **exact whole-process-tree CPU accounting** (cgroups).

macOS and Windows have neither, so on those hosts the full-fidelity way to run Colloid is this
container. Docker Desktop and Podman machine run it in a Linux VM, and WSL2 runs it natively.
The native fallback (`ProcessSandbox`, isolation level C) refuses untrusted code by design.

## Build

```bash
docker build -t colloid .
# behind a TLS-inspecting proxy: drop its CA certificate(s) into docker/extra-ca/*.crt first
# and pass the proxy through: --build-arg http_proxy=... --build-arg https_proxy=...
```

## Run

```bash
# the evaluator creates a namespace + cgroup per candidate, so it needs --privileged
docker run --rm -it --privileged -v "$PWD/runs:/src/colloid/runs" colloid canaries
docker run --rm -it --privileged -v "$PWD/runs:/src/colloid/runs" colloid run experiments/stackzero.yaml
docker run --rm -it --privileged -v "$PWD/runs:/src/colloid/runs" colloid report runs/stackzero

# integration test suite inside the container
docker run --rm -it --privileged --entrypoint bash colloid -c "COLLOID_INTEGRATION=1 pytest -q tests"
```

On cgroup v2 hosts (Docker Desktop, current Linux distributions) the sandbox moves the
container's own processes into a leaf `init` cgroup on first use, so that it can delegate
`cpu`, `memory` and `pids` to per-candidate cgroups. This is the standard docker-in-docker
technique.

## What the container cannot change

- **OS knobs** (`os.*`: sysctl, transparent huge pages) act on the kernel of the VM or host
  that runs the container. On Docker Desktop that is a VM kernel you may not want tuned.
  Leave the `os` region out of `regions:` there.
- **Measurements are host-specific**, as everywhere. The environment fingerprint records the
  capability set and the VM kernel, and the A/A noise floor is measured inside the
  container before anything is promoted.
