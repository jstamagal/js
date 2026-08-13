#if 0
: <<'//'
  envctx - dense environment context for an agent prompt, in ~3ms.

  This file is BOTH a POSIX shell script and a C program.  Run it as
      sh envctx.c
  The shell half compiles the C half into a cached binary (once, ~45ms) and
  execs it (~3ms) on every later run.  Intended use in a prompt file:

      ```!sh
      sh ~/js/tools/envctx.c
      ```

  or inline:  !{sh sh ~/js/tools/envctx.c}

  Rebuilds automatically when this file is newer than the cached binary.
  Set CC to pick a compiler.  ENVCTX_HIST=n changes the shell-history count.
//
set -e
_src=$0
_cache=${XDG_CACHE_HOME:-$HOME/.cache}/js
_bin=$_cache/envctx.bin
if [ ! -x "$_bin" ] || [ "$_src" -nt "$_bin" ]; then
  mkdir -p "$_cache"
  ${CC:-cc} -O2 -o "$_bin.$$" -x c "$_src" 2>/dev/null && mv -f "$_bin.$$" "$_bin" || {
    rm -f "$_bin.$$"; echo "envctx: build failed" >&2; exit 1; }
fi
exec "$_bin" "$@"
#endif

#define _GNU_SOURCE
#include <ctype.h>
#include <dirent.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/utsname.h>
#include <time.h>
#include <unistd.h>

#define HIST_MAX 12
#define LINE 4096

static char HOMEDIR[512];
static size_t HOMELEN;

/* ---------- tiny helpers ---------------------------------------------- */

static char *slurp(const char *path, char *buf, size_t cap) {
  int fd = open(path, O_RDONLY);
  if (fd < 0)
    return NULL;
  ssize_t n = read(fd, buf, cap - 1);
  close(fd);
  if (n <= 0)
    return NULL;
  buf[n] = 0;
  return buf;
}

/* value following `key` in a /proc-style file already in `buf` */
static long procval(const char *buf, const char *key) {
  const char *p = strstr(buf, key);
  if (!p)
    return -1;
  p += strlen(key);
  while (*p && !isdigit((unsigned char)*p))
    p++;
  return strtol(p, NULL, 10);
}

static void chomp(char *s) {
  size_t n = strlen(s);
  while (n && (s[n - 1] == '\n' || s[n - 1] == '\r' || s[n - 1] == ' '))
    s[--n] = 0;
}

/* full path of `prog`, searching the usual bindirs then $PATH */
static int which_path(const char *prog, char *out, size_t cap) {
  static const char *dirs[] = {"/usr/bin/", "/bin/", "/usr/local/bin/",
                               "/usr/sbin/", "/sbin/"};
  for (unsigned i = 0; i < sizeof dirs / sizeof *dirs; i++) {
    snprintf(out, cap, "%s%s", dirs[i], prog);
    if (access(out, X_OK) == 0)
      return 1;
  }
  const char *path = getenv("PATH");
  if (path) {
    char pb[4096];
    snprintf(pb, sizeof pb, "%s", path);
    for (char *t = strtok(pb, ":"); t; t = strtok(NULL, ":")) {
      snprintf(out, cap, "%s/%s", t, prog);
      if (access(out, X_OK) == 0)
        return 1;
    }
  }
  out[0] = 0;
  return 0;
}

static int have(const char *prog) {
  char p[512];
  return which_path(prog, p, sizeof p);
}

static int isfile(const char *p) {
  struct stat st;
  return stat(p, &st) == 0 && S_ISREG(st.st_mode);
}
static int isdir(const char *p) {
  struct stat st;
  return stat(p, &st) == 0 && S_ISDIR(st.st_mode);
}

/* collapse $HOME -> ~ */
static const char *tilde(const char *p, char *out, size_t cap) {
  if (HOMELEN && !strncmp(p, HOMEDIR, HOMELEN) &&
      (p[HOMELEN] == '/' || !p[HOMELEN]))
    snprintf(out, cap, "~%s", p + HOMELEN);
  else
    snprintf(out, cap, "%s", p);
  return out;
}

static void human(double bytes, char *out, size_t cap) {
  static const char *u[] = {"B", "K", "M", "G", "T"};
  int i = 0;
  while (bytes >= 1024 && i < 4) {
    bytes /= 1024;
    i++;
  }
  snprintf(out, cap, bytes < 10 && i ? "%.1f%s" : "%.0f%s", bytes, u[i]);
}

/* ---------- secret scrubbing ------------------------------------------
   Hand-rolled because importing scrubadub costs ~1.5s; this costs ~0us.
   Token-wise redaction: emails, IPs, phones, known key prefixes, JWTs,
   URL userinfo, KEY=secret pairs, and long high-entropy blobs.           */

static const char *KEY_PREFIX[] = {
    "sk-",   "sk_live_", "sk_test_", "rk_live_", "pk_live_",    "ghp_",
    "gho_",  "ghu_",     "ghs_",     "ghr_",     "github_pat_", "glpat-",
    "xoxb-", "xoxp-",    "xoxa-",    "xapp-",    "AKIA",        "ASIA",
    "AIza",  "ya29.",    "hf_",      "r8_",      "sbp_",        "dop_v1_",
    "npm_",  "pypi-",    "SG.",      "shpat_",   "anthropic-",  "Bearer",
    NULL};

static const char *SECRET_KEY[] = {
    "password", "passwd",     "secret",      "token", "apikey",
    "api_key",  "access_key", "private_key", "auth",  "credential",
    "session",  "cookie",     "bearer",      NULL};

static int ci_has(const char *hay, const char *needle) {
  size_t hn = strlen(hay), nn = strlen(needle);
  if (nn > hn)
    return 0;
  for (size_t i = 0; i + nn <= hn; i++) {
    size_t j = 0;
    while (j < nn && tolower((unsigned char)hay[i + j]) == needle[j])
      j++;
    if (j == nn)
      return 1;
  }
  return 0;
}

static int looks_ipv4(const char *t) {
  int parts = 0, d;
  const char *p = t;
  while (parts < 4) {
    if (!isdigit((unsigned char)*p))
      return 0;
    d = 0;
    int n = 0;
    while (isdigit((unsigned char)*p) && n < 3) {
      d = d * 10 + (*p++ - '0');
      n++;
    }
    if (d > 255)
      return 0;
    parts++;
    if (parts < 4) {
      if (*p != '.')
        return 0;
      p++;
    }
  }
  return !*p || ispunct((unsigned char)*p) ? parts == 4 : 0;
}

static int looks_phone(const char *t) {
  int digits = 0, seps = 0;
  for (const char *p = t; *p; p++) {
    if (isdigit((unsigned char)*p))
      digits++;
    else if (*p == '-' || *p == '.' || *p == '(' || *p == ')' || *p == '+')
      seps++;
    else
      return 0;
  }
  return digits >= 10 && digits <= 13 && seps >= 1;
}

/* long mixed-case+digit run with no path/word structure => probably a secret */
static int looks_blob(const char *t) {
  size_t n = strlen(t);
  if (n < 28)
    return 0;
  int lo = 0, up = 0, dg = 0, other = 0;
  for (const char *p = t; *p; p++) {
    if (islower((unsigned char)*p))
      lo++;
    else if (isupper((unsigned char)*p))
      up++;
    else if (isdigit((unsigned char)*p))
      dg++;
    else if (*p == '_' || *p == '-' || *p == '+' || *p == '/' || *p == '=')
      other++;
    else
      return 0; /* contains . / : etc -> path or url, leave it */
  }
  if (!dg)
    return 0;
  if (n >= 40 && dg && (lo || up))
    return 1;
  return up && lo && dg && n >= 32;
}

static void scrub_token(const char *t, char *out, size_t cap) {
  /* KEY=VALUE with a sensitive-looking key */
  const char *eq = strchr(t, '=');
  if (eq && eq != t) {
    char k[128];
    size_t kl =
        (size_t)(eq - t) < sizeof k - 1 ? (size_t)(eq - t) : sizeof k - 1;
    memcpy(k, t, kl);
    k[kl] = 0;
    for (int i = 0; SECRET_KEY[i]; i++)
      if (ci_has(k, SECRET_KEY[i])) {
        snprintf(out, cap, "%s=<redacted>", k);
        return;
      }
  }
  for (int i = 0; KEY_PREFIX[i]; i++)
    if (!strncmp(t, KEY_PREFIX[i], strlen(KEY_PREFIX[i])) &&
        strlen(t) > strlen(KEY_PREFIX[i]) + 6) {
      snprintf(out, cap, "<key:%s..>", KEY_PREFIX[i]);
      return;
    }
  if (!strncmp(t, "eyJ", 3) && strlen(t) > 24) {
    snprintf(out, cap, "<jwt>");
    return;
  }
  const char *at = strchr(t, '@');
  if (at && at != t && strchr(at, '.')) {
    const char *scheme = strstr(t, "://");
    if (scheme && scheme < at) { /* url userinfo */
      char host[256];
      snprintf(host, sizeof host, "%s", at + 1);
      size_t sl = (size_t)(scheme + 3 - t);
      char sch[32];
      if (sl > sizeof sch - 1)
        sl = sizeof sch - 1;
      memcpy(sch, t, sl);
      sch[sl] = 0;
      snprintf(out, cap, "%s<user:pw>@%s", sch, host);
      return;
    }
    snprintf(out, cap, "<email>");
    return;
  }
  if (looks_ipv4(t) && strncmp(t, "127.", 4) && strncmp(t, "0.0.0.0", 7) &&
      strncmp(t, "192.168.", 8) && strncmp(t, "10.", 3)) {
    snprintf(out, cap, "<ip>");
    return;
  }
  if (looks_phone(t)) {
    snprintf(out, cap, "<phone>");
    return;
  }
  if (looks_blob(t)) {
    snprintf(out, cap, "<secret:%zub>", strlen(t));
    return;
  }
  snprintf(out, cap, "%s", t);
}

static void scrub(const char *in, char *out, size_t cap) {
  char home_folded[LINE * 2];
  /* fold $HOME to ~ first so paths do not leak the account name */
  if (HOMELEN) {
    size_t o = 0;
    for (const char *p = in; *p && o < sizeof home_folded - 2;) {
      if (!strncmp(p, HOMEDIR, HOMELEN)) {
        home_folded[o++] = '~';
        p += HOMELEN;
      } else
        home_folded[o++] = *p++;
    }
    home_folded[o] = 0;
    in = home_folded;
  }
  size_t o = 0;
  const char *p = in;
  char tok[LINE], red[LINE];
  while (*p && o < cap - 1) {
    if (isspace((unsigned char)*p) || *p == '"' || *p == '\'' || *p == '`') {
      out[o++] = *p++;
      continue;
    }
    size_t n = 0;
    while (*p && !isspace((unsigned char)*p) && *p != '"' && *p != '\'' &&
           *p != '`' && n < sizeof tok - 1)
      tok[n++] = *p++;
    tok[n] = 0;
    scrub_token(tok, red, sizeof red);
    size_t rl = strlen(red);
    if (o + rl >= cap - 1)
      break;
    memcpy(out + o, red, rl);
    o += rl;
  }
  out[o] = 0;
}

/* ---------- shell history --------------------------------------------- */

struct hist {
  char cmd[512];
  long ts;
};
static char SKIP[10][64];
static int NSKIP;

static void add_skip(const char *s) {
  if (!s || !*s || NSKIP >= 10)
    return;
  const char *b = strrchr(s, '/');
  b = b ? b + 1 : s;
  for (int i = 0; i < NSKIP; i++)
    if (!strcmp(SKIP[i], b))
      return;
  snprintf(SKIP[NSKIP++], sizeof SKIP[0], "%s", b);
}

static void build_skiplist(void) {
  add_skip("envctx");
  const char *sh = getenv("SHELL");
  const char *shb = sh ? (strrchr(sh, '/') ? strrchr(sh, '/') + 1 : sh) : "";
  const char *e = getenv("ENVCTX_SKIP");
  if (e) {
    char t[256];
    snprintf(t, sizeof t, "%s", e);
    for (char *x = strtok(t, ","); x; x = strtok(NULL, ","))
      add_skip(x);
  }
  int pid = (int)getppid();
  char buf[512], path[64];
  for (int d = 0; d < 8 && pid > 1; d++) {
    snprintf(path, sizeof path, "/proc/%d/cmdline", pid);
    if (slurp(path, buf, sizeof buf) && buf[0]) {
      const char *b = strrchr(buf, '/');
      b = b ? b + 1 : buf;
      if (strcmp(b, shb))
        add_skip(b); /* never skip the shell itself */
    }
    snprintf(path, sizeof path, "/proc/%d/stat", pid);
    if (!slurp(path, buf, sizeof buf))
      break;
    char *rp = strrchr(buf, ')');
    if (!rp || sscanf(rp + 2, "%*c %d", &pid) != 1)
      break;
  }
}

static int skip_cmd(const char *cmd) {
  while (*cmd == ' ' || *cmd == '\t')
    cmd++;
  size_t n = 0;
  while (cmd[n] && cmd[n] != ' ')
    n++;
  char first[128];
  if (n > sizeof first - 1)
    n = sizeof first - 1;
  memcpy(first, cmd, n);
  first[n] = 0;
  const char *b = strrchr(first, '/');
  b = b ? b + 1 : first;
  for (int i = 0; i < NSKIP; i++)
    if (!strcmp(b, SKIP[i]))
      return 1;
  return strstr(cmd, "envctx") ? 1 : 0;
}
static int read_history(struct hist *h, int want) {
  const char *shell = getenv("SHELL");
  const char *base = shell ? strrchr(shell, '/') : NULL;
  base = base ? base + 1 : (shell ? shell : "sh");
  char path[1024];
  const char *hf = getenv("HISTFILE");
  if (hf && isfile(hf))
    snprintf(path, sizeof path, "%s", hf);
  else if (!strcmp(base, "zsh"))
    snprintf(path, sizeof path, "%s/.zsh_history", HOMEDIR);
  else if (!strcmp(base, "bash"))
    snprintf(path, sizeof path, "%s/.bash_history", HOMEDIR);
  else if (!strcmp(base, "fish"))
    snprintf(path, sizeof path, "%s/.local/share/fish/fish_history", HOMEDIR);
  else
    snprintf(path, sizeof path, "%s/.%s_history", HOMEDIR, base);
  if (!isfile(path)) {
    snprintf(path, sizeof path, "%s/.zsh_history", HOMEDIR);
    if (!isfile(path))
      return 0;
  }

  /* only the tail matters; history files get huge */
  struct stat st;
  if (stat(path, &st))
    return 0;
  off_t off = st.st_size > 65536 ? st.st_size - 65536 : 0;
  FILE *f = fopen(path, "rb");
  if (!f)
    return 0;
  if (off) {
    fseeko(f, off, SEEK_SET);
    char junk[LINE];
    (void)!fgets(junk, sizeof junk, f);
  }

  /* ring buffer of the last `want` entries */
  int n = 0;
  char raw[LINE];
  while (fgets(raw, sizeof raw, f)) {
    chomp(raw);
    char *cmd = raw;
    long ts = 0;
    if (*cmd == ':') { /* zsh extended: ": <ts>:<dur>;cmd" */
      char *semi = strchr(cmd, ';');
      if (semi) {
        ts = strtol(cmd + 1, NULL, 10);
        cmd = semi + 1;
      }
    } else if (!strncmp(cmd, "- cmd: ", 7))
      cmd += 7; /* fish */
    else if (*cmd == '#' && isdigit((unsigned char)cmd[1]))
      continue; /* bash ts line */
    while (*cmd == ' ')
      cmd++;
    if (!*cmd)
      continue;
    size_t cl = strlen(cmd);
    if (cl && cmd[cl - 1] == '\\')
      continue; /* skip continuation fragments */
    /* drop self-invocations and consecutive dupes */
    if (skip_cmd(cmd))
      continue;
    int slot = n % want;
    if (n && !strcmp(h[(n - 1) % want].cmd, cmd))
      continue;
    snprintf(h[slot].cmd, sizeof h[slot].cmd, "%s", cmd);
    h[slot].ts = ts;
    n++;
  }
  fclose(f);
  if (!n)
    return 0;
  /* unroll ring into chronological order */
  struct hist tmp[HIST_MAX];
  int have_n = n < want ? n : want;
  for (int i = 0; i < have_n; i++)
    tmp[i] = h[(n - have_n + i) % want];
  memcpy(h, tmp, sizeof(struct hist) * have_n);
  return have_n;
}

/* ---------- package manager / project detection ----------------------- */

static const char *pkgmgr(void) {
  struct {
    const char *bin, *label;
  } m[] = {{"xbps-install", "xbps"}, {"pacman", "pacman"}, {"apt-get", "apt"},
           {"dnf", "dnf"},           {"zypper", "zypper"}, {"apk", "apk"},
           {"emerge", "portage"},    {"nix-env", "nix"},   {"brew", "brew"},
           {"pkg", "pkg"},           {NULL, NULL}};
  static char out[128];
  out[0] = 0;
  for (int i = 0; m[i].bin; i++)
    if (have(m[i].bin)) {
      size_t l = strlen(out);
      snprintf(out + l, sizeof out - l, "%s%s", l ? "," : "", m[i].label);
    }
  return out[0] ? out : "?";
}

int main(void) {
  setvbuf(stdout, malloc(1 << 16), _IOFBF, 1 << 16);
  char buf[1 << 16], b2[LINE], b3[LINE];

  snprintf(HOMEDIR, sizeof HOMEDIR, "%s", getenv("HOME") ? getenv("HOME") : "");
  HOMELEN = strlen(HOMEDIR);

  char cwd[1024];
  if (!getcwd(cwd, sizeof cwd))
    snprintf(cwd, sizeof cwd, "?");

  /* ---- line 1: identity + time ---- */
  struct utsname un;
  uname(&un);
  time_t now = time(NULL);
  struct tm lt;
  localtime_r(&now, &lt);
  char ts[64];
  strftime(ts, sizeof ts, "%Y-%m-%d %H:%M %Z", &lt);
  double up = 0;
  if (slurp("/proc/uptime", buf, sizeof buf))
    up = strtod(buf, NULL);
  printf("### ENV %s | host=%s up=%dd%dh\n", ts, un.nodename, (int)(up / 86400),
         (int)(up / 3600) % 24);

  /* ---- os ---- */
  char osname[128] = "", osver[64] = "";
  if (slurp("/etc/os-release", buf, sizeof buf)) {
    char *p = strstr(buf, "\nPRETTY_NAME=");
    if (!p && !strncmp(buf, "PRETTY_NAME=", 12))
      p = buf - 1;
    if (p) {
      p = strchr(p + 1, '=') + 1;
      if (*p == '"')
        p++;
      size_t i = 0;
      while (*p && *p != '"' && *p != '\n' && i < sizeof osname - 1)
        osname[i++] = *p++;
      osname[i] = 0;
    }
    char *v = strstr(buf, "VERSION_ID=");
    if (v)
      sscanf(v + 11, "\"%63[^\"]\"", osver),
          sscanf(v + 11, "%63[^\n\"]", osver);
  }
  int wsl =
      strstr(un.release, "microsoft") || strstr(un.release, "WSL") ? 1 : 0;
  const char *initsys = isdir("/run/systemd/system") ? "systemd"
                        : isdir("/run/runit")        ? "runit"
                        : isfile("/sbin/openrc")     ? "openrc"
                        : isdir("/etc/svc/volatile") ? "smf"
                        : isfile("/etc/inittab")     ? "sysvinit"
                                                     : "?";
  printf("os=%s%s%s arch=%s kern=%s init=%s\n", osname[0] ? osname : un.sysname,
         osver[0] ? " " : "", osver, un.machine, un.release, initsys);

  /* ---- hardware ---- */
  int cpus = (int)sysconf(_SC_NPROCESSORS_ONLN);
  char cpumodel[128] = "?";
  if (slurp("/proc/cpuinfo", buf, sizeof buf)) {
    char *p = strstr(buf, "model name");
    if (!p)
      p = strstr(buf, "Model");
    if (p && (p = strchr(p, ':'))) {
      p += 2;
      size_t i = 0;
      while (*p && *p != '\n' && i < sizeof cpumodel - 1)
        cpumodel[i++] = *p++;
      cpumodel[i] = 0;
      /* trim marketing noise */
      char *cut = strstr(cpumodel, " with ");
      if (cut)
        *cut = 0;
    }
  }
  long mt = -1, ma = -1;
  if (slurp("/proc/meminfo", buf, sizeof buf)) {
    mt = procval(buf, "MemTotal:");
    ma = procval(buf, "MemAvailable:");
  }
  char load[64] = "?";
  if (slurp("/proc/loadavg", buf, sizeof buf)) {
    sscanf(buf, "%63[^\n]", load);
    char *third = load;
    int sp = 0;
    for (char *p = load; *p; p++)
      if (*p == ' ' && ++sp == 3) {
        *p = 0;
        break;
      }
    (void)third;
  }
  human((double)mt * 1024, b2, sizeof b2);
  human((double)ma * 1024, b3, sizeof b3);
  struct statvfs vf;
  char dfree[32] = "?", dtot[32] = "?";
  if (!statvfs(cwd, &vf)) {
    human((double)vf.f_bavail * vf.f_frsize, dfree, sizeof dfree);
    human((double)vf.f_blocks * vf.f_frsize, dtot, sizeof dtot);
  }
  printf("cpu=%dx %s load=%s mem=%s/%s avail disk=%s/%s free\n", cpus, cpumodel,
         load, b3, b2, dfree, dtot);

  /* ---- session ---- */
  const char *sh = getenv("SHELL"), *term = getenv("TERM"),
             *venv = getenv("VIRTUAL_ENV");
  int pathdirs = 0, envn = 0;
  const char *P = getenv("PATH");
  if (P)
    for (const char *p = P; *p; p++)
      if (*p == ':')
        pathdirs++;
  extern char **environ;
  for (char **e = environ; *e; e++)
    envn++;
  printf("user=%s shell=%s term=%s pkg=%s path=%d env=%d%s%s%s%s%s\n",
         getenv("USER") ? getenv("USER") : "?", sh ? sh : "?",
         term ? term : "-", pkgmgr(), pathdirs + 1, envn, venv ? " venv=" : "",
         venv ? tilde(venv, b2, sizeof b2) : "",
         isfile("/.dockerenv") ? " container=docker" : "", wsl ? " wsl=1" : "",
         getenv("SSH_CONNECTION") ? " ssh=1" : "");

  /* ---- cwd + git (single popen for every git fact) ---- */
  printf("cwd=%s", tilde(cwd, b2, sizeof b2));
  char gitdir[1400];
  int in_repo = 0;
  {
    char probe[1024];
    snprintf(probe, sizeof probe, "%s", cwd);
    for (;;) {
      snprintf(gitdir, sizeof gitdir, "%s/.git", probe);
      if (isdir(gitdir) || isfile(gitdir)) {
        in_repo = 1;
        break;
      }
      char *slash = strrchr(probe, '/');
      if (!slash || slash == probe)
        break;
      *slash = 0;
    }
    if (in_repo && strcmp(probe, cwd))
      printf(" repo_root=%s", tilde(probe, b3, sizeof b3));
    /* a linked worktree or submodule has .git as a FILE holding
       "gitdir: <path>"; follow it or the STATE probes below all miss */
    if (in_repo && isfile(gitdir)) {
      char link[1400];
      if (slurp(gitdir, link, sizeof link)) {
        char *g = strstr(link, "gitdir:");
        if (g) {
          g += 7;
          while (*g == ' ')
            g++;
          chomp(g);
          if (*g == '/')
            snprintf(gitdir, sizeof gitdir, "%s", g);
          else if (*g)
            snprintf(gitdir, sizeof gitdir, "%s/%s", probe, g);
        }
      }
    }
  }
  /* what kind of project is this */
  struct {
    const char *f, *tag;
  } marks[] = {{"pyproject.toml", "python:pyproject"},
               {"requirements.txt", "python:reqs"},
               {"uv.lock", "uv"},
               {"poetry.lock", "poetry"},
               {"package.json", "node"},
               {"pnpm-lock.yaml", "pnpm"},
               {"yarn.lock", "yarn"},
               {"bun.lockb", "bun"},
               {"package-lock.json", "npm"},
               {"Cargo.toml", "rust"},
               {"go.mod", "go"},
               {"CMakeLists.txt", "cmake"},
               {"Makefile", "make"},
               {"justfile", "just"},
               {"Dockerfile", "docker"},
               {"compose.yaml", "compose"},
               {"docker-compose.yml", "compose"},
               {".venv", "venv"},
               {"flake.nix", "nix"},
               {NULL, NULL}};
  char proj[512] = "";
  for (int i = 0; marks[i].f; i++) {
    char p[1300];
    snprintf(p, sizeof p, "%s/%s", cwd, marks[i].f);
    struct stat st;
    if (!stat(p, &st)) {
      size_t l = strlen(proj);
      snprintf(proj + l, sizeof proj - l, "%s%s", l ? "," : "", marks[i].tag);
    }
  }
  int nfiles = 0, ndirs = 0;
  DIR *d = opendir(cwd);
  if (d) {
    struct dirent *e;
    while ((e = readdir(d)) && nfiles + ndirs < 5000) {
      if (e->d_name[0] == '.')
        continue;
      if (e->d_type == DT_DIR)
        ndirs++;
      else
        nfiles++;
    }
    closedir(d);
  }
  printf(" entries=%df/%dd%s%s\n", nfiles, ndirs, proj[0] ? " proj=" : "",
         proj);

  if (in_repo) {
    FILE *g = popen(
        "git status --porcelain=v2 --branch 2>/dev/null; echo @@L; "
        "git log -1 --format='%h%x09%s%x09%cr%x09%an' 2>/dev/null; echo @@S; "
        "git stash list 2>/dev/null | wc -l; echo @@R; "
        "git config --get remote.origin.url 2>/dev/null",
        "r");
    if (g) {
      char branch[128] = "?", upstream[128] = "", ab[64] = "", head[64] = "";
      int mod = 0, add = 0, del = 0, unt = 0, stg = 0, cfl = 0;
      char logline[LINE] = "", stash[32] = "0", remote[512] = "";
      int sect = 0;
      while (fgets(buf, sizeof buf, g)) {
        chomp(buf);
        if (!strcmp(buf, "@@L")) {
          sect = 1;
          continue;
        }
        if (!strcmp(buf, "@@S")) {
          sect = 2;
          continue;
        }
        if (!strcmp(buf, "@@R")) {
          sect = 3;
          continue;
        }
        if (sect == 0) {
          if (!strncmp(buf, "# branch.head ", 14))
            snprintf(branch, sizeof branch, "%.127s", buf + 14);
          else if (!strncmp(buf, "# branch.upstream ", 18))
            snprintf(upstream, sizeof upstream, "%.127s", buf + 18);
          else if (!strncmp(buf, "# branch.ab ", 12))
            snprintf(ab, sizeof ab, "%.63s", buf + 12);
          else if (!strncmp(buf, "# branch.oid ", 13))
            snprintf(head, sizeof head, "%.9s", buf + 13);
          else if (buf[0] == '?')
            unt++;
          else if (buf[0] == 'u')
            cfl++;
          else if (buf[0] == '1' || buf[0] == '2') {
            /* porcelain v2: buf[2] is the index column, buf[3] the worktree
               one. A only ever appears staged, so read both and bucket each
               file once, else staged adds and deletes report as zero. */
            char x = buf[2], y = buf[3];
            if (x != '.')
              stg++;
            if (x == 'A')
              add++;
            else if (x == 'D' || y == 'D')
              del++;
            else if (x == 'M' || y == 'M' || x == 'R' || y == 'R')
              mod++;
          }
        } else if (sect == 1) {
          if (!logline[0])
            snprintf(logline, sizeof logline, "%.*s", LINE - 1, buf);
        } else if (sect == 2) {
          if (isdigit((unsigned char)buf[0]))
            snprintf(stash, sizeof stash, "%.16s", buf);
        } else if (buf[0])
          snprintf(remote, sizeof remote, "%.500s", buf);
      }
      pclose(g);
      printf("git branch=%s", branch);
      if (upstream[0])
        printf(" <-%s", upstream);
      if (ab[0])
        printf(" %s", ab);
      printf(" head=%s dirty=%dM/%dA/%dD/%d?", head, mod, add, del, unt);
      if (cfl)
        printf(" CONFLICTS=%d", cfl);
      if (stg)
        printf(" staged=%d", stg);
      if (strcmp(stash, "0"))
        printf(" stash=%s", stash);
      if (remote[0]) {
        scrub(remote, b2, sizeof b2);
        printf(" remote=%s", b2);
      }
      /* in-progress operations are high-signal for an agent */
      char p[1536];
      snprintf(p, sizeof p, "%s/MERGE_HEAD", gitdir);
      if (isfile(p))
        printf(" STATE=merging");
      snprintf(p, sizeof p, "%s/rebase-merge", gitdir);
      if (isdir(p))
        printf(" STATE=rebasing");
      snprintf(p, sizeof p, "%s/BISECT_LOG", gitdir);
      if (isfile(p))
        printf(" STATE=bisecting");
      putchar('\n');
      if (logline[0]) {
        char *t1 = strchr(logline, '\t');
        if (t1) {
          *t1++ = 0;
          char *t2 = strchr(t1, '\t');
          if (t2)
            *t2++ = 0;
          char *t3 = t2 ? strchr(t2, '\t') : NULL;
          if (t3)
            *t3++ = 0;
          scrub(t1, b2, sizeof b2);
          printf("last_commit=%s \"%.90s\" (%s%s%s)\n", logline, b2,
                 t2 ? t2 : "", t3 ? ", " : "", t3 ? t3 : "");
        }
      }
    }
  } else {
    printf("git=not-a-repo\n");
  }

  /* ---- toolchain ------------------------------------------------------
     Spawning `foo --version` for a dozen tools costs ~18ms — more than
     everything else here combined.  Versions change only when the binaries
     do, so cache the rendered line and invalidate on a stat() fingerprint
     of each tool's path+mtime+size.  Steady state: a few stat calls.      */
  {
    static const char *TOOLS[] = {"python3", "node", "cc",     "git",
                                  "rustc",   "go",   "uv",     "just",
                                  "rg",      "jq",   "docker", NULL};
    char fp[2048];
    size_t fl = 0;
    for (int i = 0; TOOLS[i]; i++) {
      /* search $PATH, not just the system bindirs: the version line below is
         produced by `command -v`, so anything it can find must be fingerprinted
         here or a version bump never invalidates the cache */
      char full[512];
      struct stat st;
      if (!which_path(TOOLS[i], full, sizeof full) || stat(full, &st))
        continue;
      fl += (size_t)snprintf(fp + fl, sizeof fp - fl, "%s:%s:%ld:%lld;",
                             TOOLS[i], full, (long)st.st_mtime,
                             (long long)st.st_size);
      if (fl >= sizeof fp - 64)
        break;
    }
    unsigned long hash = 5381;
    for (char *p = fp; *p; p++)
      hash = hash * 33u + (unsigned char)*p;

    const char *xdg = getenv("XDG_CACHE_HOME");
    char cachedir[900], cachefile[1024];
    if (xdg && *xdg)
      snprintf(cachedir, sizeof cachedir, "%s/js", xdg);
    else
      snprintf(cachedir, sizeof cachedir, "%s/.cache/js", HOMEDIR);
    snprintf(cachefile, sizeof cachefile, "%s/envctx.tools.%lx", cachedir,
             hash);

    char tools[2048];
    if (!slurp(cachefile, tools, sizeof tools)) {
      FILE *t = popen(
          "v(){ command -v $1 >/dev/null 2>&1 || return; "
          "printf '%s=%s\\n' \"$1\" \"$($1 $2 2>&1 | head -1 | "
          "tr -cd '[:alnum:]._+-' | cut -c1-22)\"; }; "
          "{ v python3 -V & v node -v & v cc -dumpversion & v git --version & "
          "v rustc -V & v go version & v uv -V & v just --version & "
          "v rg --version & v jq --version & v docker -v & wait; } "
          "2>/dev/null | sort",
          "r");
      size_t o = 0;
      tools[0] = 0;
      if (t) {
        char line[256];
        int col = 0;
        while (fgets(line, sizeof line, t)) {
          chomp(line);
          if (!line[0])
            continue;
          char *eq = strchr(line, '=');
          if (eq) {
            size_t nl = (size_t)(eq - line);
            char *v = eq + 1;
            /* "Python3.14.6", "versiongo1.26.5linuxamd64", "v24.14.1" all carry
               a name/word prefix and sometimes a platform suffix; keep the
               number. */
            while (isalpha((unsigned char)*v))
              v++;
            while (*v && !isdigit((unsigned char)*v))
              v++;
            char num[32];
            size_t j = 0;
            for (char *p = v; *p && j < sizeof num - 1; p++) {
              if (isdigit((unsigned char)*p) || *p == '.')
                num[j++] = *p;
              else
                break; /* first letter/dash after the digits = build metadata */
            }
            num[j] = 0;
            while (j && num[j - 1] == '.')
              num[--j] = 0;
            o += (size_t)snprintf(tools + o, sizeof tools - o, " %.*s=%s",
                                  (int)nl, line, j ? num : eq + 1);
          } else
            o += (size_t)snprintf(tools + o, sizeof tools - o, " %s", line);
          if (++col % 6 == 0)
            o += (size_t)snprintf(tools + o, sizeof tools - o, "\n     ");
          if (o >= sizeof tools - 80)
            break;
        }
        pclose(t);
      }
      /* best-effort cache write; a stale/unwritable cache is not an error */
      char tmp[1100];
      snprintf(tmp, sizeof tmp, "%s.tmp%d", cachefile, (int)getpid());
      FILE *w = fopen(tmp, "w");
      if (!w) {
        char mk[1024];
        snprintf(mk, sizeof mk, "%s", cachedir);
        mkdir(mk, 0700);
        w = fopen(tmp, "w");
      }
      if (w) {
        fputs(tools, w);
        fclose(w);
        if (rename(tmp, cachefile))
          unlink(tmp);
      }
    }
    printf("tools:%s\n", tools);
  }

  /* ---- recent shell commands, scrubbed ---- */
  {
    int want = 5;
    const char *hn = getenv("ENVCTX_HIST");
    if (hn) {
      want = atoi(hn);
      if (want < 1)
        want = 1;
      if (want > HIST_MAX)
        want = HIST_MAX;
    }
    build_skiplist();
    struct hist h[HIST_MAX];
    int n = read_history(h, want);
    printf("recent_shell(scrubbed, oldest first):\n");
    if (!n)
      printf("  (unavailable)\n");
    for (int i = 0; i < n; i++) {
      scrub(h[i].cmd, b2, sizeof b2);
      char age[32] = "";
      if (h[i].ts) {
        long dt = (long)now - h[i].ts;
        if (dt < 0)
          dt = 0;
        if (dt < 3600)
          snprintf(age, sizeof age, "%ldm", dt / 60);
        else if (dt < 86400)
          snprintf(age, sizeof age, "%ldh", dt / 3600);
        else
          snprintf(age, sizeof age, "%ldd", dt / 86400);
      }
      printf("  %d%s%s%s %.180s%s\n", i + 1, age[0] ? " (-" : "", age,
             age[0] ? ")" : "", b2, strlen(b2) > 180 ? ".." : "");
    }
  }
  return 0;
}
