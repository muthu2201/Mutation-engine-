/*
 * sbx-exec: the privilege-dropping launcher behind Colloid's Linux sandbox adapter.
 *
 * Candidate programs are untrusted: they were written by an LLM or produced by a mutation
 * operator, and the blueprint (D2, D4) treats them as adversaries. This helper is started
 * as root by the evaluator and, before exec'ing the candidate, applies in order:
 *
 *   1. cgroup membership    - joins the given cgroup-v1 directories (cpuacct for exact CPU
 *                             accounting, memory for the RSS cap, pids against fork bombs,
 *                             freezer so the whole tree can be frozen and killed atomically)
 *   2. CPU affinity / nice / scheduling policy (these are OS-layer *knob genes*)
 *   3. a fresh network namespace (CLONE_NEWNET): no interfaces except a downed loopback, so
 *                             there is no network at all; Unix sockets on the filesystem
 *                             still work, which is how the candidate reaches Postgres and how
 *                             the load generator reaches the candidate
 *   3b. a filesystem jail (--fs-jail, candidates only): a private mount namespace in which
 *                             the declared writable paths (--rw) are bind-mounted onto
 *                             themselves and every other mount is remounted read-only, and
 *                             /tmp, /var/tmp, /dev/shm are fresh private tmpfs. Nothing a
 *                             candidate writes outside its workspace outlives it or is seen
 *                             by another candidate (no cross-evaluation cache through /tmp),
 *                             whatever the host's directory permissions. Connecting to Unix
 *                             sockets needs no writable mount, so Postgres stays reachable.
 *   4. resource limits      - address space, processes, file size, open files, CPU seconds
 *   5. credential drop      - setgroups(0), setgid, setuid to an unprivileged sandbox user,
 *                             then verify root cannot be regained
 *   6. PR_SET_PDEATHSIG     - the candidate dies if the evaluator dies
 *   7. PR_SET_NO_NEW_PRIVS  - no setuid binaries can re-elevate
 *   8. a seccomp-BPF filter - denies (EPERM) syscalls a web service never needs and that
 *                             are escape or tampering primitives: ptrace, process_vm_*, mount,
 *                             namespaces (unshare/setns, clone with CLONE_NEW* flags, clone3),
 *                             module loading, bpf, perf_event_open, keyrings, clock setting,
 *                             reboot...; and refuses AF_INET/AF_INET6/AF_PACKET sockets
 *                             (defence in depth on top of the empty network namespace)
 *
 * Usage: sbx-exec [options] -- program args...
 *   --uid N --gid N        drop to this user/group (required unless --no-drop)
 *   --no-drop              keep root (only used for trusted tooling, never for candidates)
 *   --netns                new, empty network namespace
 *   --fs-jail              read-only filesystem except --rw paths; private /tmp /var/tmp /dev/shm
 *   --rw DIR               writable path inside the jail (repeatable, absolute, resolved)
 *   --cgroup DIR           join cgroup DIR (repeatable)
 *   --cpus LIST            CPU affinity, e.g. "1-2" or "1,3"
 *   --nice N               nice value (-20..19)
 *   --sched other|batch|idle
 *   --as-mb N --nproc N --fsize-mb N --nofile N --cpu-s N
 *   --seccomp              install the seccomp filter
 *   --chdir DIR
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/sched.h>
#include <linux/seccomp.h>
#include <sched.h>
#include <signal.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/mount.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/statvfs.h>
#include <sys/syscall.h>
#include <unistd.h>

#define MAX_CGROUPS 8
#define MAX_RW 16
#define MAX_FILTER 256

static void die(const char *what) {
    fprintf(stderr, "sbx-exec: %s: %s\n", what, strerror(errno));
    _exit(126);
}

static void die_msg(const char *msg) {
    fprintf(stderr, "sbx-exec: %s\n", msg);
    _exit(126);
}

static void join_cgroup(const char *dir) {
    char path[4096];
    snprintf(path, sizeof path, "%s/cgroup.procs", dir);
    int fd = open(path, O_WRONLY | O_CLOEXEC);
    if (fd < 0) die(path);
    char buf[32];
    int n = snprintf(buf, sizeof buf, "%d\n", (int)getpid());
    if (write(fd, buf, (size_t)n) != n) die("write cgroup.procs");
    close(fd);
}

static void set_cpus(const char *list) {
    cpu_set_t set;
    CPU_ZERO(&set);
    char *copy = strdup(list), *save = NULL;
    for (char *tok = strtok_r(copy, ",", &save); tok; tok = strtok_r(NULL, ",", &save)) {
        int a, b;
        if (sscanf(tok, "%d-%d", &a, &b) == 2) {
            for (int c = a; c <= b; c++) CPU_SET(c, &set);
        } else if (sscanf(tok, "%d", &a) == 1) {
            CPU_SET(a, &set);
        } else {
            die_msg("bad --cpus list");
        }
    }
    free(copy);
    if (sched_setaffinity(0, sizeof set, &set) != 0) die("sched_setaffinity");
}

static void limit(int resource, rlim_t value, const char *name) {
    struct rlimit rl = {value, value};
    if (setrlimit(resource, &rl) != 0) die(name);
}

/* ---------------------------------------------------------------- filesystem jail */

/* path == dir, or path is inside dir */
static int is_under(const char *path, const char *dir) {
    size_t n = strlen(dir);
    if (n == 1 && dir[0] == '/') return 1;
    return strncmp(path, dir, n) == 0 && (path[n] == '\0' || path[n] == '/');
}

/* /proc/self/mountinfo escapes space, tab, newline and backslash as \ooo (octal) */
static void unescape_octal(char *s) {
    char *r = s, *w = s;
    while (*r) {
        if (r[0] == '\\' && r[1] >= '0' && r[1] <= '7' && r[2] >= '0' && r[2] <= '7' && r[3] >= '0' && r[3] <= '7') {
            *w++ = (char)(((r[1] - '0') << 6) | ((r[2] - '0') << 3) | (r[3] - '0'));
            r += 4;
        } else {
            *w++ = *r++;
        }
    }
    *w = '\0';
}

/* the per-mount flags a read-only bind remount must keep (it may not silently drop nosuid etc.) */
static unsigned long kept_flags(const char *mp) {
    struct statvfs sv;
    unsigned long f = 0;
    if (statvfs(mp, &sv) != 0) return 0;
    if (sv.f_flag & ST_NOSUID) f |= MS_NOSUID;
    if (sv.f_flag & ST_NODEV) f |= MS_NODEV;
    if (sv.f_flag & ST_NOEXEC) f |= MS_NOEXEC;
    if (sv.f_flag & ST_NOATIME) f |= MS_NOATIME;
    if (sv.f_flag & ST_NODIRATIME) f |= MS_NODIRATIME;
    if (sv.f_flag & ST_RELATIME) f |= MS_RELATIME;
    return f;
}

static void fs_jail(const char **rw, int nrw) {
    if (unshare(CLONE_NEWNS) != 0) die("unshare(CLONE_NEWNS)");
    /* nothing we do below may propagate back to the host */
    if (mount(NULL, "/", NULL, MS_REC | MS_PRIVATE, NULL) != 0) die("make mounts private");
    /* 1. each writable path becomes its own mount, so step 2 leaves it writable */
    for (int k = 0; k < nrw; k++)
        if (mount(rw[k], rw[k], NULL, MS_BIND | MS_REC, NULL) != 0) die(rw[k]);
    /* 2. every other mount read-only; /proc, /sys and /dev keep their own semantics */
    FILE *mi = fopen("/proc/self/mountinfo", "re");
    if (!mi) die("/proc/self/mountinfo");
    char line[8192];
    while (fgets(line, sizeof line, mi)) {
        char *save = NULL, *mp = NULL;
        int field = 0;
        for (char *tok = strtok_r(line, " ", &save); tok; tok = strtok_r(NULL, " ", &save), field++) {
            if (field == 4) { mp = tok; break; }  /* id parent maj:min root MOUNTPOINT ... */
        }
        if (!mp) continue;
        unescape_octal(mp);
        if (is_under(mp, "/proc") || is_under(mp, "/sys") || is_under(mp, "/dev")) continue;
        int writable = 0;
        for (int k = 0; k < nrw; k++) if (is_under(mp, rw[k])) writable = 1;
        if (writable) continue;
        if (mount(NULL, mp, NULL, MS_REMOUNT | MS_BIND | MS_RDONLY | kept_flags(mp), NULL) != 0) {
            if (errno == ENOENT) continue;  /* shadowed by a later mount: that one is handled itself */
            die(mp);                        /* fail closed: never run a candidate half-jailed */
        }
    }
    fclose(mi);
    /* 3. private scratch space, unless a writable path lives there (it would be hidden) */
    static const char *const scratch[] = {"/tmp", "/var/tmp", "/dev/shm"};
    for (size_t t = 0; t < sizeof scratch / sizeof scratch[0]; t++) {
        if (access(scratch[t], F_OK) != 0) continue;
        int holds_rw = 0;
        for (int k = 0; k < nrw; k++) if (is_under(rw[k], scratch[t])) holds_rw = 1;
        if (holds_rw) continue;
        if (mount("tmpfs", scratch[t], "tmpfs", MS_NOSUID | MS_NODEV, "size=256m,mode=1777") != 0) die(scratch[t]);
    }
}

/* ---------------------------------------------------------------- seccomp */

static struct sock_filter prog[MAX_FILTER];
static int plen = 0;

static void emit(struct sock_filter f) {
    if (plen >= MAX_FILTER) die_msg("seccomp filter too long");
    prog[plen++] = f;
}

static const int denied[] = {
    __NR_ptrace, __NR_process_vm_readv, __NR_process_vm_writev, __NR_mount, __NR_umount2,
    __NR_pivot_root, __NR_chroot, __NR_unshare, __NR_setns, __NR_kexec_load,
#ifdef __NR_kexec_file_load
    __NR_kexec_file_load,
#endif
    __NR_init_module, __NR_finit_module, __NR_delete_module, __NR_bpf, __NR_perf_event_open,
    __NR_keyctl, __NR_add_key, __NR_request_key, __NR_reboot, __NR_swapon, __NR_swapoff,
    __NR_acct, __NR_settimeofday, __NR_clock_settime, __NR_clock_adjtime, __NR_adjtimex,
    __NR_userfaultfd, __NR_open_by_handle_at, __NR_name_to_handle_at, __NR_iopl, __NR_ioperm,
    __NR_quotactl, __NR_syslog, __NR_vhangup, __NR_fanotify_init, __NR_lookup_dcookie,
#ifdef __NR_move_mount
    __NR_move_mount, __NR_open_tree, __NR_fsopen, __NR_fsconfig, __NR_fsmount, __NR_fspick,
#endif
};

static void install_seccomp(void) {
    const unsigned int ERR_EPERM = SECCOMP_RET_ERRNO | (EPERM & SECCOMP_RET_DATA);
    const unsigned int ERR_EACCES = SECCOMP_RET_ERRNO | (EACCES & SECCOMP_RET_DATA);
    const unsigned int ERR_ENOSYS = SECCOMP_RET_ERRNO | (ENOSYS & SECCOMP_RET_DATA);
    const unsigned long ns_flags = CLONE_NEWNS | CLONE_NEWUTS | CLONE_NEWIPC | CLONE_NEWUSER |
                                   CLONE_NEWPID | CLONE_NEWNET | CLONE_NEWCGROUP;

    /* Only the native x86-64 ABI is allowed; x32 and i386 entry points are killed. */
    emit((struct sock_filter)BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch)));
    emit((struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH_X86_64, 1, 0));
    emit((struct sock_filter)BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS));
    emit((struct sock_filter)BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)));
    emit((struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JGE | BPF_K, 0x40000000, 0, 1));
    emit((struct sock_filter)BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS));

    for (size_t i = 0; i < sizeof denied / sizeof denied[0]; i++) {
        emit((struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, (unsigned)denied[i], 0, 1));
        emit((struct sock_filter)BPF_STMT(BPF_RET | BPF_K, ERR_EPERM));
    }
    /* clone3 passes flags in memory we cannot inspect: report ENOSYS so libc falls back to clone. */
    emit((struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_clone3, 0, 1));
    emit((struct sock_filter)BPF_STMT(BPF_RET | BPF_K, ERR_ENOSYS));
    /* clone(flags, ...): refuse namespace creation. */
    emit((struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_clone, 0, 4));
    emit((struct sock_filter)BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[0])));
    emit((struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, (unsigned)ns_flags, 0, 1));
    emit((struct sock_filter)BPF_STMT(BPF_RET | BPF_K, ERR_EPERM));
    emit((struct sock_filter)BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW));
    /* socket(domain, ...): no IP or raw packet sockets. */
    emit((struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_socket, 0, 6));
    emit((struct sock_filter)BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[0])));
    emit((struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AF_INET, 2, 0));
    emit((struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AF_INET6, 1, 0));
    emit((struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AF_PACKET, 0, 1));
    emit((struct sock_filter)BPF_STMT(BPF_RET | BPF_K, ERR_EACCES));
    emit((struct sock_filter)BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW));
    emit((struct sock_filter)BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW));

    struct sock_fprog fprog = {.len = (unsigned short)plen, .filter = prog};
    if (syscall(__NR_seccomp, SECCOMP_SET_MODE_FILTER, 0, &fprog) != 0) die("seccomp");
}

int main(int argc, char **argv) {
    long uid = -1, gid = -1, nice_v = 0, as_mb = 0, nproc = 0, fsize_mb = 0, nofile = 0, cpu_s = 0;
    int netns = 0, seccomp = 0, no_drop = 0, have_nice = 0, jail = 0;
    const char *cgroups[MAX_CGROUPS];
    const char *rw[MAX_RW];
    int ncg = 0, nrw = 0;
    const char *cpus = NULL, *sched = NULL, *dir = NULL;
    int i = 1;
    for (; i < argc; i++) {
        const char *a = argv[i];
#define NEED_ARG() do { if (i + 1 >= argc) die_msg("missing argument value"); } while (0)
        if (strcmp(a, "--") == 0) { i++; break; }
        else if (strcmp(a, "--uid") == 0) { NEED_ARG(); uid = atol(argv[++i]); }
        else if (strcmp(a, "--gid") == 0) { NEED_ARG(); gid = atol(argv[++i]); }
        else if (strcmp(a, "--no-drop") == 0) { no_drop = 1; }
        else if (strcmp(a, "--netns") == 0) { netns = 1; }
        else if (strcmp(a, "--fs-jail") == 0) { jail = 1; }
        else if (strcmp(a, "--rw") == 0) { NEED_ARG(); if (nrw >= MAX_RW) die_msg("too many --rw paths"); rw[nrw++] = argv[++i];
                                           if (rw[nrw - 1][0] != '/') die_msg("--rw paths must be absolute"); }
        else if (strcmp(a, "--seccomp") == 0) { seccomp = 1; }
        else if (strcmp(a, "--cgroup") == 0) { NEED_ARG(); if (ncg >= MAX_CGROUPS) die_msg("too many cgroups"); cgroups[ncg++] = argv[++i]; }
        else if (strcmp(a, "--cpus") == 0) { NEED_ARG(); cpus = argv[++i]; }
        else if (strcmp(a, "--nice") == 0) { NEED_ARG(); nice_v = atol(argv[++i]); have_nice = 1; }
        else if (strcmp(a, "--sched") == 0) { NEED_ARG(); sched = argv[++i]; }
        else if (strcmp(a, "--as-mb") == 0) { NEED_ARG(); as_mb = atol(argv[++i]); }
        else if (strcmp(a, "--nproc") == 0) { NEED_ARG(); nproc = atol(argv[++i]); }
        else if (strcmp(a, "--fsize-mb") == 0) { NEED_ARG(); fsize_mb = atol(argv[++i]); }
        else if (strcmp(a, "--nofile") == 0) { NEED_ARG(); nofile = atol(argv[++i]); }
        else if (strcmp(a, "--cpu-s") == 0) { NEED_ARG(); cpu_s = atol(argv[++i]); }
        else if (strcmp(a, "--chdir") == 0) { NEED_ARG(); dir = argv[++i]; }
        else { fprintf(stderr, "sbx-exec: unknown option %s\n", a); return 126; }
    }
    if (i >= argc) die_msg("no program given");
    if (!no_drop && (uid <= 0 || gid <= 0)) die_msg("--uid/--gid required (non-root)");

    for (int c = 0; c < ncg; c++) join_cgroup(cgroups[c]);
    if (cpus) set_cpus(cpus);
    if (have_nice && setpriority(PRIO_PROCESS, 0, (int)nice_v) != 0) die("setpriority");
    if (sched) {
        struct sched_param sp = {0};
        int pol = SCHED_OTHER;
        if (strcmp(sched, "batch") == 0) pol = SCHED_BATCH;
        else if (strcmp(sched, "idle") == 0) pol = SCHED_IDLE;
        else if (strcmp(sched, "other") != 0) die_msg("bad --sched");
        if (sched_setscheduler(0, pol, &sp) != 0) die("sched_setscheduler");
    }
    if (netns && unshare(CLONE_NEWNET) != 0) die("unshare(CLONE_NEWNET)");
    if (jail) fs_jail(rw, nrw);
    if (as_mb > 0) limit(RLIMIT_AS, (rlim_t)as_mb << 20, "RLIMIT_AS");
    if (fsize_mb > 0) limit(RLIMIT_FSIZE, (rlim_t)fsize_mb << 20, "RLIMIT_FSIZE");
    if (nofile > 0) limit(RLIMIT_NOFILE, (rlim_t)nofile, "RLIMIT_NOFILE");
    if (cpu_s > 0) limit(RLIMIT_CPU, (rlim_t)cpu_s, "RLIMIT_CPU");
    limit(RLIMIT_CORE, 0, "RLIMIT_CORE");
    if (dir && chdir(dir) != 0) die("chdir");

    if (!no_drop) {
        if (setgroups(0, NULL) != 0) die("setgroups");
        if (setresgid((gid_t)gid, (gid_t)gid, (gid_t)gid) != 0) die("setresgid");
        if (nproc > 0) limit(RLIMIT_NPROC, (rlim_t)nproc, "RLIMIT_NPROC");
        if (setresuid((uid_t)uid, (uid_t)uid, (uid_t)uid) != 0) die("setresuid");
        if (setuid(0) == 0 || seteuid(0) == 0) die_msg("privilege drop failed: root regained");
    }
    if (prctl(PR_SET_PDEATHSIG, SIGKILL) != 0) die("PR_SET_PDEATHSIG");
    if (getppid() == 1) die_msg("parent already gone");
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) die("PR_SET_NO_NEW_PRIVS");
    if (seccomp) install_seccomp();
    execvp(argv[i], &argv[i]);
    die("execvp");
    return 126;
}
