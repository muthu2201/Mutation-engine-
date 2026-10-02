# ADR 0004 — Platforms and isolation: a portable searcher, a Linux judge

**Status:** accepted

## Context

Colloid has to run on developers' machines (Linux, macOS, Windows) and on bench hosts. Its
two halves need different things from the OS:

- The **search side** (core, operators, archives, bandit, attribution, data lake, reports,
  LLM calls) is plain computation and I/O. It needs nothing OS-specific.
- The **judge** (the evaluator) needs two kernel features. Without them it is not
  trustworthy:
  1. a *security boundary* around untrusted, LLM-generated candidate code: syscall
     filtering, namespaces, an unprivileged identity, a read-only view of the filesystem;
  2. *exact whole-process-tree CPU accounting*, so work cannot hide in a child process.

On Linux these are seccomp, namespaces and cgroups. macOS and Windows have no equivalent
that a user-space program can apply to a child process. Pretending otherwise would make
every "verified" result on those hosts meaningless.

## Decision

1. **The search side is natively portable.** POSIX-only imports are lazy, state lives in a
   per-platform directory (`COLLOID_STATE`), and CI runs lint, strict types, architecture
   contracts and the portable test suite on Ubuntu, macOS and Windows.
2. **The judge has full fidelity only on Linux**, on both cgroup v1 and unified cgroup v2
   (`CgroupSet`): `memory.max`/`memory.events`, `cpu.stat`, `pids.max`, `cgroup.kill`, with
   docker-in-docker style controller delegation. A CI job verifies v2 on a real kernel as
   root. Candidates additionally run in a **filesystem jail**: a private mount namespace,
   writable paths bind-mounted, every other mount read-only, and private tmpfs scratch space.
3. **On macOS and Windows the full-fidelity judge runs in the Colloid Linux container**
   (`Dockerfile`, `docker/README.md`, built by `scripts/provision-linux.sh`) under Docker
   Desktop, Podman machine or WSL2.
4. **Portable fallbacks exist, labelled honestly.** `ProcessSandbox` (isolation level C)
   provides a wall clock, a memory/pids watchdog, rlimits, whole-tree kill and psutil-sampled
   whole-tree CPU (`CpuCounterFile`, within 5% of cgroup accounting on Linux). It **refuses
   untrusted candidate code** unless explicitly allowed. CPU pinning falls back to psutil or
   is recorded as unpinned; memory falls back from smaps PSS to psutil PSS or USS.
5. **Measurements carry their capability set.** The environment fingerprint records the
   sandbox backend, memory metric and pinning. Results from different capability sets are
   never compared, and the A/A noise floor is measured on the host and backend in use before
   anything is promoted.
6. **Kernel-level genes (risk class D) need full VMs** and are refused by every sandbox
   here.

## Consequences

- The same engine and data lake work everywhere. Trust in a measurement comes from where the
  judge ran, never from where the searcher ran.
- OS knobs inside the container act on the VM's kernel. Users exclude the `os` region there.
- Running the judge natively on macOS or Windows is a deliberate, visible downgrade
  (isolation C, trusted operators only), never a silent one.
- First CI runs found four platform bugs that tests on one Linux host could not:
  - v1-only cgroup setup on a v2 host;
  - a Windows file-replace race in the CPU counter;
  - an eager OS-account lookup;
  - filesystem confinement that relied on directory permissions (the reason for the jail).
