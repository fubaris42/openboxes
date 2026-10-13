#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.8"
# dependencies = []
# ///
"""OpenBoxes helper: one-command setup plus day-to-day maintenance (podman; Windows and Fedora).

  uv run obx.py init              first-time setup: .env, containers, seed data, start on boot
  uv run obx.py status            what is running, where, and when it was last backed up
  uv run obx.py doctor            check prerequisites and common problems
  uv run obx.py start|stop|restart
  uv run obx.py logs [-f]         application logs (add "db" for the database)
  uv run obx.py backup            dump database + uploads into ./backups
  uv run obx.py restore FILE      replace the database with a backup
  uv run obx.py update [VERSION]  back up, then switch to a newer OpenBoxes release
  uv run obx.py seed              (re)apply seed.sql
  uv run obx.py lan on HOST|off   expose the app to the office network, or localhost only
  uv run obx.py autostart install|uninstall
  uv run obx.py config            show .env with passwords hidden
  uv run obx.py db                open a SQL prompt on the database

Keep this file next to compose.yaml. Only the standard library is used, so plain
"python obx.py ..." works too when uv is not installed.
"""
import argparse
import base64
import json
import os
import re
import secrets
import shutil
import socket
import string
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

if sys.version_info < (3, 8):
    sys.exit("obx.py needs Python 3.8 or newer")

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
SEED_FILE = ROOT / "seed.sql"
BACKUP_DIR = ROOT / "backups"
APP, DB = "openboxes-app", "openboxes-db"
IMAGE = "ghcr.io/openboxes/openboxes"
DEFAULT_VERSION = "v0.9.8-hotfix1"
DEFAULT_PORT = "8081"
TASK_NAME = "OpenBoxes"
UNIT_NAME = "openboxes.service"
WINDOWS = os.name == "nt"
NEEDS_MACHINE = WINDOWS or sys.platform == "darwin"

UV = shutil.which("uv")
ME = "uv run obx.py" if UV else "python obx.py"  # how hints tell the user to call this script
COMPOSE_HINT = "uv tool install podman-compose" if UV else "pip install podman-compose"

LOG = None  # set by go_headless()
RUN_ENV = dict(os.environ, PODMAN_COMPOSE_WARNING_LOGS="false")


class Fail(Exception):
    pass


def go_headless():
    """No console (autostart at logon/boot): send our output and every command's to obx.log."""
    global LOG
    LOG = open(ROOT / "obx.log", "a", buffering=1, encoding="utf-8", errors="replace")
    sys.stdout = sys.stderr = LOG


def say(message=""):
    message = message.replace("python obx.py", ME)
    if LOG:
        message = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    print(message, flush=True)


def run(*cmd, check=True, capture=False, quiet=False, interactive=False):
    kw = dict(cwd=str(ROOT), env=RUN_ENV)
    if capture:
        kw.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace")
    elif quiet:
        kw.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif LOG:
        kw.update(stdout=LOG, stderr=LOG)
    if not interactive:
        kw["stdin"] = subprocess.DEVNULL
    if WINDOWS and LOG:
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.run([str(c) for c in cmd], **kw)
    except FileNotFoundError:
        raise Fail("%s is not installed or not on PATH" % cmd[0])
    if check and proc.returncode:
        detail = (proc.stderr or "").strip() if capture else ""
        raise Fail("command failed (exit %d): %s%s" % (
            proc.returncode, " ".join(str(c) for c in cmd), "\n" + detail if detail else ""))
    return proc


def podman(*args, **kw):
    return run("podman", *args, **kw)


def compose(*args, **kw):
    return podman("compose", *args, **kw)


def in_container(name, script, **kw):
    """Run a bash script inside a container; $VARS are expanded there, not on the host."""
    return podman("exec", name, "bash", "-o", "pipefail", "-c", script, **kw)


# --- .env -------------------------------------------------------------------

def read_env():
    values = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def set_env(key, value):
    """Set KEY=value in .env, or remove the key when value is None."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    lines = [l for l in lines if not re.match(r"\s*%s\s*=" % re.escape(key), l)]
    if value is not None:
        lines.append("%s=%s" % (key, value))
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def new_secret(length=28):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def create_env(port=None, lan_host=None):
    if ENV_FILE.exists():
        say(".env already exists, keeping it")
        return
    lines = [
        "# Written by obx.py. Change with 'python obx.py lan ...' / 'update ...' or edit by hand.",
        "OB_VERSION=%s" % DEFAULT_VERSION,
        "OB_PORT=%s" % (port or DEFAULT_PORT),
        "DB_PASSWORD=%s" % new_secret(),
        "DB_ROOT_PASSWORD=%s" % new_secret(),
    ]
    if lan_host:
        lines += ["OB_BIND=0.0.0.0", "OB_HOST=%s" % lan_host]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    if not WINDOWS:
        ENV_FILE.chmod(0o600)
    say("Created .env with new database passwords")


def app_url(env=None):
    env = read_env() if env is None else env
    return "http://%s:%s/openboxes/" % (env.get("OB_HOST", "localhost"), env.get("OB_PORT", DEFAULT_PORT))


# --- podman / containers ----------------------------------------------------

def ensure_machine(create=False):
    """Windows and macOS run containers inside a podman machine that has to be started first."""
    if not NEEDS_MACHINE:
        return
    proc = podman("machine", "inspect", "--format", "{{.State}}", check=False, capture=True)
    if proc.returncode:
        if not create:
            raise Fail("no podman machine found; run: python obx.py init")
        say("Creating podman machine...")
        podman("machine", "init")
        state = ""
    else:
        state = proc.stdout.strip()
    if state != "running":
        say("Starting podman machine...")
        podman("machine", "start")
    for _ in range(30):  # the machine can report running before the API answers
        if podman("info", check=False, quiet=True).returncode == 0:
            return
        time.sleep(5)
    raise Fail("podman machine is not responding")


def container_state(name):
    """Return (status, health), or (None, None) when the container does not exist."""
    proc = podman("inspect", "-f", "{{.State.Status}} {{.State.Health.Status}}", name,
                  check=False, capture=True)
    if proc.returncode:
        return None, None
    parts = proc.stdout.split()
    return (parts[0] if parts else None), (parts[1] if len(parts) > 1 else None)


def require_running(name):
    if container_state(name)[0] != "running":
        raise Fail("%s is not running; start it with: python obx.py start" % name)


def wait_healthy(timeout=1800):
    say("Waiting for OpenBoxes to become healthy (the first boot can take several minutes)...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, health = container_state(APP)
        if health == "healthy":
            return
        if status in (None, "exited", "dead"):
            raise Fail("%s is not running; see: python obx.py logs" % APP)
        time.sleep(5)
    raise Fail("%s did not become healthy within %d minutes; see: python obx.py logs" % (APP, timeout // 60))


def require_env():
    if not ENV_FILE.exists():
        raise Fail(".env is missing; run: python obx.py init")


def up():
    require_env()
    ensure_machine()
    compose("up", "-d")


def recreate():
    """Recreate the containers so changed settings apply. Named volumes (the data) are kept."""
    compose("down")
    compose("up", "-d")


# --- seed / backup / restore ------------------------------------------------

def seed():
    if not SEED_FILE.exists():
        say("No seed.sql next to obx.py; nothing to seed")
        return
    wait_healthy()
    podman("cp", SEED_FILE.name, "%s:/tmp/seed.sql" % DB)
    in_container(DB, "MYSQL_PWD=$MARIADB_PASSWORD mariadb -uopenboxes openboxes < /tmp/seed.sql")
    # the app caches locations, categories and products, so restart it to pick up the new rows
    podman("restart", APP, quiet=True)
    say("Seed applied")


def host_path(path):
    """Path to hand to 'podman cp': relative to ROOT where possible (avoids C: drive colons)."""
    try:
        return os.path.relpath(str(path), str(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def uploads_file(db_file):
    return db_file.with_name(db_file.name.replace(".sql.gz", "-uploads.tar.gz"))


def backup(keep=14):
    require_running(DB)
    BACKUP_DIR.mkdir(exist_ok=True)
    db_file = BACKUP_DIR / ("openboxes-%s.sql.gz" % time.strftime("%Y%m%d-%H%M%S"))
    in_container(DB, "MYSQL_PWD=$MARIADB_PASSWORD mariadb-dump -uopenboxes --single-transaction "
                     "--routines --triggers openboxes | gzip > /tmp/obx-backup.sql.gz "
                     "&& zcat /tmp/obx-backup.sql.gz | tail -n 1 | grep -q 'Dump completed'")
    podman("cp", "%s:/tmp/obx-backup.sql.gz" % DB, host_path(db_file))
    in_container(DB, "rm -f /tmp/obx-backup.sql.gz")
    say("Database -> %s (%s)" % (host_path(db_file), human_size(db_file.stat().st_size)))

    if container_state(APP)[0] == "running":
        up_file = uploads_file(db_file)
        in_container(APP, "tar czf /tmp/obx-uploads.tar.gz -C /app uploads")
        podman("cp", "%s:/tmp/obx-uploads.tar.gz" % APP, host_path(up_file))
        in_container(APP, "rm -f /tmp/obx-uploads.tar.gz")
        say("Uploads  -> %s (%s)" % (host_path(up_file), human_size(up_file.stat().st_size)))
    else:
        say("Uploads skipped (%s is not running)" % APP)

    if keep and keep > 0:
        for old in sorted(BACKUP_DIR.glob("openboxes-*.sql.gz"))[:-keep]:
            old.unlink()
            if uploads_file(old).exists():
                uploads_file(old).unlink()
            say("Pruned old backup %s" % old.name)
    return db_file


def load_dump(dump, database="openboxes"):
    """Drop and recreate `database`, then load a .sql.gz dump into it."""
    if not re.match(r"^\w+$", database):
        raise Fail("invalid database name: %s" % database)
    podman("cp", host_path(dump), "%s:/tmp/obx-restore.sql.gz" % DB)
    in_container(DB, "export MYSQL_PWD=$MARIADB_ROOT_PASSWORD; "
                     "mariadb -uroot -e 'DROP DATABASE IF EXISTS {0}; CREATE DATABASE {0}' "
                     "&& gunzip -c /tmp/obx-restore.sql.gz | mariadb -uroot {0}; "
                     "rc=$?; rm -f /tmp/obx-restore.sql.gz; exit $rc".format(database))


def restore(dump, assume_yes=False):
    dump = Path(dump).resolve()
    if not dump.is_file() or not dump.name.endswith(".sql.gz"):
        raise Fail("not a .sql.gz backup file: %s" % dump)
    require_running(DB)
    if not assume_yes:
        confirm("This REPLACES the current OpenBoxes database with %s." % dump.name, "restore")
    say("Taking a safety backup of the current state first...")
    backup(keep=0)
    podman("stop", APP, quiet=True, check=False)
    say("Loading %s..." % dump.name)
    load_dump(dump)
    podman("start", APP, quiet=True)
    up_file = uploads_file(dump)
    if up_file.exists():
        podman("cp", host_path(up_file), "%s:/tmp/obx-uploads.tar.gz" % APP)
        in_container(APP, "tar xzf /tmp/obx-uploads.tar.gz -C /app; rc=$?; rm -f /tmp/obx-uploads.tar.gz; exit $rc")
        say("Uploads restored")
    wait_healthy()
    say("Restore complete: %s" % app_url())


def confirm(message, word):
    if not sys.stdin or not sys.stdin.isatty():
        raise Fail("%s Re-run with --yes to confirm." % message)
    if input("%s\nType '%s' to continue: " % (message, word)).strip() != word:
        raise Fail("cancelled")


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


def human_age(seconds):
    for limit, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if seconds >= limit:
            return "%d %s ago" % (seconds // limit, unit)
    return "just now"


# --- start on boot ----------------------------------------------------------

def powershell(script, **kw):
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return run("powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded, **kw)


def unit_path():
    config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config) / "systemd" / "user" / UNIT_NAME


def launcher():
    """Command that re-runs this script without a console: through uv when it is installed."""
    script = str(Path(__file__).resolve())
    if WINDOWS:  # uvw.exe / pythonw.exe do not flash a console window at logon
        uvw = shutil.which("uvw")
        if uvw or UV:
            return [uvw or UV, "run", "--script", script]
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        return [str(pythonw if pythonw.exists() else sys.executable), script]
    return [UV, "run", "--script", script] if UV else [sys.executable, script]


def autostart_install():
    command = launcher()
    if WINDOWS:
        def q(value):
            return str(value).replace("'", "''")

        arguments = " ".join('"%s"' % a if " " in a else a for a in command[1:] + ["start", "--headless"])
        powershell("""
$ErrorActionPreference = 'Stop'
$user = "$env:USERDOMAIN\\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute '%s' -Argument '%s' -WorkingDirectory '%s'
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName '%s' -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Force | Out-Null
""" % (q(command[0]), q(arguments), q(ROOT), TASK_NAME), capture=True)
        say("Autostart installed: scheduled task '%s' runs at logon (output in obx.log)" % TASK_NAME)
    else:
        unit = unit_path()
        unit.parent.mkdir(parents=True, exist_ok=True)
        exe = " ".join('"%s"' % a for a in command)
        unit.write_text("""[Unit]
Description=OpenBoxes (podman compose)

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=%s
ExecStart=%s start
ExecStop=%s stop
TimeoutStartSec=1800
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
""" % (ROOT, exe, exe), encoding="utf-8")
        user = os.environ.get("USER") or Path.home().name
        # linger starts the user's services at boot, without anyone logging in
        if run("loginctl", "enable-linger", user, check=False, quiet=True).returncode:
            run("sudo", "loginctl", "enable-linger", user, interactive=True)
        run("systemctl", "--user", "daemon-reload")
        run("systemctl", "--user", "enable", UNIT_NAME, quiet=True)
        say("Autostart installed: systemd user service %s starts at boot" % UNIT_NAME)


def autostart_uninstall():
    if WINDOWS:
        powershell("Unregister-ScheduledTask -TaskName '%s' -Confirm:$false -ErrorAction Stop" % TASK_NAME,
                   capture=True)
    else:
        run("systemctl", "--user", "disable", UNIT_NAME, check=False, quiet=True)
        if unit_path().exists():
            unit_path().unlink()
        run("systemctl", "--user", "daemon-reload", check=False)
    say("Autostart removed (containers were left as they are)")


def autostart_installed():
    if WINDOWS:
        proc = powershell("(Get-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue | "
                          "Measure-Object).Count" % TASK_NAME, capture=True, check=False)
        return proc.stdout.strip() == "1"
    return run("systemctl", "--user", "is-enabled", UNIT_NAME, check=False, quiet=True).returncode == 0


# --- checks -----------------------------------------------------------------

def port_in_use(port):
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", int(port))) == 0


def app_responds(env):
    url = "http://localhost:%s/openboxes/health" % env.get("OB_PORT", DEFAULT_PORT)
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status == 200
    except Exception:
        return False


def doctor():
    """Print a checklist; return True when nothing is broken."""
    results = []

    def check(ok, label, hint=""):
        results.append(ok)
        mark = {True: " ok ", False: "FAIL", None: "warn"}[ok]
        say("[%s] %s%s" % (mark, label, "" if ok or not hint else "  -> " + hint))

    has_podman = shutil.which("podman") is not None
    check(has_podman, "podman installed",
          "winget install RedHat.Podman" if WINDOWS else "sudo dnf install -y podman")
    check(bool(shutil.which("podman-compose") or shutil.which("docker-compose")), "compose provider installed",
          COMPOSE_HINT if WINDOWS else "sudo dnf install -y podman-compose")
    check((ROOT / "compose.yaml").exists(), "compose.yaml next to obx.py")
    env = read_env()
    missing = [k for k in ("DB_PASSWORD", "DB_ROOT_PASSWORD") if not env.get(k)]
    check(not missing, ".env has database passwords", "python obx.py init")
    if not has_podman:
        return False

    if NEEDS_MACHINE:
        proc = podman("machine", "inspect", "--format", "{{.State}}", check=False, capture=True)
        state = proc.stdout.strip() if proc.returncode == 0 else "missing"
        check(state == "running", "podman machine is %s" % state, "python obx.py start")
        if state != "running":
            return False
    proc = podman("info", "--format", "{{.Host.MemTotal}}", check=False, capture=True)
    reachable = proc.returncode == 0
    check(reachable, "podman is responding")
    if not reachable:
        return False
    memory = int(proc.stdout.strip() or 0)
    check(True if memory >= 3 * 1024 ** 3 else None, "%.1f GB memory available to containers" % (memory / 1024.0 ** 3),
          "OpenBoxes wants about 3 GB; it may run out of memory")

    port = env.get("OB_PORT", DEFAULT_PORT)
    for name in (DB, APP):
        status, health = container_state(name)
        check(status == "running" and health == "healthy", "%s is %s%s" % (
            name, status or "not created", " (%s)" % health if health else ""),
            "python obx.py start, then python obx.py logs")
    if container_state(APP)[0] == "running":
        check(app_responds(env), "app answers on http://localhost:%s/openboxes/" % port,
              "still booting? python obx.py logs -f")
    else:
        check(not port_in_use(port), "port %s is free" % port, "another program uses it; change OB_PORT in .env")
    check(True if autostart_installed() else None, "starts automatically on boot",
          "python obx.py autostart install")
    newest = sorted(BACKUP_DIR.glob("openboxes-*.sql.gz"))[-1:] if BACKUP_DIR.exists() else []
    age = time.time() - newest[0].stat().st_mtime if newest else None
    check(True if age is not None and age < 7 * 86400 else None,
          "last backup: %s" % (human_age(age) if newest else "never"), "python obx.py backup")
    return False not in results


# --- commands ---------------------------------------------------------------

def cmd_init(args):
    missing = [tool for tool in ("podman", "podman-compose") if not shutil.which(tool)]
    if "podman-compose" in missing and shutil.which("docker-compose"):
        missing.remove("podman-compose")
    if missing and shutil.which("dnf"):
        say("Installing %s..." % ", ".join(missing))
        run("sudo", "dnf", "install", "-y", *missing, interactive=True)
    elif missing == ["podman-compose"] and UV:
        say("Installing podman-compose with uv...")
        run(UV, "tool", "install", "podman-compose")
        if not shutil.which("podman-compose"):
            raise Fail("podman-compose was installed but is not on PATH; run 'uv tool update-shell', "
                       "open a new terminal and re-run init")
    elif missing:
        hints = {"podman": "winget install RedHat.Podman", "podman-compose": COMPOSE_HINT}
        raise Fail("missing: %s\n  install with: %s\n  then open a new terminal and re-run init" % (
            ", ".join(missing), " ; ".join(hints[m] for m in missing)))
    ensure_machine(create=True)
    create_env(port=args.port, lan_host=args.lan)
    env = read_env()
    if container_state(APP)[0] != "running" and port_in_use(env.get("OB_PORT", DEFAULT_PORT)):
        raise Fail("port %s is already in use; set another OB_PORT in .env" % env.get("OB_PORT", DEFAULT_PORT))

    say("Pulling images (about 1 GB the first time)...")
    compose("pull")
    compose("up", "-d")
    if args.no_seed:
        wait_healthy()
    else:
        seed()
        wait_healthy()
    if not args.no_autostart:
        autostart_install()

    say()
    say("OpenBoxes is ready: %s" % app_url())
    say("First login: admin / password  (change it under the user menu)")
    if env.get("OB_BIND", "127.0.0.1") != "127.0.0.1":
        lan_hint(env)
    say("Next: 'python obx.py status', 'python obx.py backup', 'python obx.py -h'")


def lan_hint(env):
    port = env.get("OB_PORT", DEFAULT_PORT)
    if WINDOWS:
        say("Note: on Windows the podman machine normally forwards localhost only; other PCs may need")
        say("      extra port forwarding (netsh portproxy + firewall rule) to reach port %s." % port)
    elif shutil.which("firewall-cmd"):
        say("Open the firewall for other PCs:")
        say("  sudo firewall-cmd --permanent --add-port=%s/tcp && sudo firewall-cmd --reload" % port)


def cmd_start(args):
    up()
    say("Started: %s" % app_url())


def cmd_stop(args):
    compose("stop")
    say("Stopped (data is kept)")


def cmd_restart(args):
    if args.all:
        compose("stop")
        compose("up", "-d")
    else:
        podman("restart", APP, quiet=True)
    wait_healthy()
    say("Running: %s" % app_url())


def cmd_status(args):
    env = read_env()
    say("OpenBoxes %s   %s" % (env.get("OB_VERSION", DEFAULT_VERSION), app_url(env)))
    for name in (APP, DB):
        status, health = container_state(name)
        say("  %-14s %s%s" % (name, status or "not created", " (%s)" % health if health else ""))
    bind = env.get("OB_BIND", "127.0.0.1")
    say("  reachable from: %s" % ("this PC only" if bind == "127.0.0.1" else "the network (%s)" % bind))
    say("  autostart:      %s" % ("installed" if autostart_installed() else "not installed"))
    backups = sorted(BACKUP_DIR.glob("openboxes-*.sql.gz")) if BACKUP_DIR.exists() else []
    if backups:
        newest = backups[-1]
        say("  backups:        %d, newest %s (%s, %s)" % (
            len(backups), newest.name, human_size(newest.stat().st_size),
            human_age(time.time() - newest.stat().st_mtime)))
    else:
        say("  backups:        none yet (python obx.py backup)")


def cmd_logs(args):
    cmd = ["logs", "--tail", str(args.tail)] + (["-f"] if args.follow else [])
    try:
        podman(*cmd, DB if args.service == "db" else APP, check=False, interactive=True)
    except KeyboardInterrupt:
        pass


def cmd_backup(args):
    backup(keep=args.keep)


def cmd_restore(args):
    restore(args.file, assume_yes=args.yes)


def latest_release():
    request = urllib.request.Request("https://api.github.com/repos/openboxes/openboxes/releases/latest",
                                     headers={"User-Agent": "obx.py"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)["tag_name"]


def cmd_update(args):
    require_env()
    current = read_env().get("OB_VERSION", DEFAULT_VERSION)
    try:
        target = args.version or latest_release()
    except Exception as error:
        raise Fail("could not look up the latest release (%s); pass a version, e.g. update v0.9.8" % error)
    if target == current:
        say("Already on %s (the latest release)" % current if not args.version else "Already on %s" % current)
        return
    say("Updating %s -> %s" % (current, target))
    podman("pull", "%s:%s" % (IMAGE, target))
    if not args.no_backup:
        backup(keep=0)
    set_env("OB_VERSION", target)
    recreate()
    wait_healthy()
    say("Now running %s: %s" % (target, app_url()))
    say("The database was migrated in place. To go back: python obx.py update %s, then restore the backup." % current)


def cmd_seed(args):
    require_running(DB)
    seed()
    wait_healthy()


def cmd_lan(args):
    require_env()
    if args.mode == "on":
        if not args.host:
            raise Fail("give the name or IP other PCs will use, e.g.: python obx.py lan on 192.168.1.20")
        set_env("OB_BIND", "0.0.0.0")
        set_env("OB_HOST", args.host)
    else:
        set_env("OB_BIND", None)
        set_env("OB_HOST", None)
    if container_state(APP)[0] is not None:
        recreate()
        wait_healthy()
    env = read_env()
    say("OpenBoxes now at %s" % app_url(env))
    if args.mode == "on":
        lan_hint(env)


def cmd_autostart(args):
    autostart_install() if args.mode == "install" else autostart_uninstall()


def cmd_config(args):
    require_env()
    for key, value in read_env().items():
        say("%s=%s" % (key, "********" if "PASSWORD" in key else value))


def cmd_doctor(args):
    if not doctor():
        raise Fail("some checks failed")


def cmd_db(args):
    require_running(DB)
    podman("exec", "-it", DB, "bash", "-c", "MYSQL_PWD=$MARIADB_PASSWORD mariadb -uopenboxes openboxes",
           check=False, interactive=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = True

    def add(name, func, help_text):
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    p = add("init", cmd_init, "first-time setup: .env, containers, seed data, start on boot")
    p.add_argument("--port", help="host port (default %s)" % DEFAULT_PORT)
    p.add_argument("--lan", metavar="HOST", help="expose to the network; HOST is the name/IP other PCs will use")
    p.add_argument("--no-seed", action="store_true", help="do not apply seed.sql")
    p.add_argument("--no-autostart", action="store_true", help="do not start on boot")
    p = add("start", cmd_start, "start the containers (and the podman machine on Windows)")
    p.add_argument("--headless", action="store_true", help="write all output to obx.log (used by autostart)")
    add("stop", cmd_stop, "stop the containers, keeping all data")
    p = add("restart", cmd_restart, "restart the application")
    p.add_argument("--all", action="store_true", help="restart the database too")
    add("status", cmd_status, "show what is running")
    p = add("logs", cmd_logs, "show logs")
    p.add_argument("service", nargs="?", choices=["app", "db"], default="app")
    p.add_argument("-f", "--follow", action="store_true", help="keep following (Ctrl+C to stop)")
    p.add_argument("-n", "--tail", type=int, default=100, help="lines to show (default 100)")
    p = add("backup", cmd_backup, "dump database + uploads into ./backups")
    p.add_argument("--keep", type=int, default=14, help="backups to keep, 0 = keep all (default 14)")
    p = add("restore", cmd_restore, "replace the database with a backup (.sql.gz)")
    p.add_argument("file")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p = add("update", cmd_update, "back up, then switch OpenBoxes version (default: latest release)")
    p.add_argument("version", nargs="?")
    p.add_argument("--no-backup", action="store_true")
    add("seed", cmd_seed, "(re)apply seed.sql; safe to repeat")
    p = add("lan", cmd_lan, "expose the app to the network, or back to this PC only")
    p.add_argument("mode", choices=["on", "off"])
    p.add_argument("host", nargs="?", help="name or IP other PCs will use (for 'on')")
    p = add("autostart", cmd_autostart, "start OpenBoxes automatically on boot")
    p.add_argument("mode", choices=["install", "uninstall"])
    add("config", cmd_config, "show .env with passwords hidden")
    add("doctor", cmd_doctor, "check prerequisites and common problems")
    add("db", cmd_db, "open a SQL prompt on the database")

    args = parser.parse_args()
    if getattr(args, "headless", False) or sys.stdout is None:
        go_headless()
    try:
        args.func(args)
    except Fail as error:
        say("error: %s" % error)
        sys.exit(1)
    except KeyboardInterrupt:
        say("interrupted")
        sys.exit(130)


if __name__ == "__main__":
    main()
