#!/usr/bin/env python3
"""herdr plugin: after a server restart, resume Claude Code in each pane on its
first focus, never starting two claude processes at once.

State (per herdr session, keyed by socket path, because the plugin state dir is
shared by every session):
  panes:   {pane_id: session_id|None}  claude seen running and not exited since
  terms:   {pane_id: terminal_id}      last seen; follows panes across moves
  boot:    server-start token (socket inode + mtime)
  pending: {pane_id: session_id}       panes to resume during this boot
  prompts: {pane_id: hash}             reference "empty prompt" line for this boot

Records are removed on explicit events (claude released, pane closed, another
agent in the pane), when a pane is skipped as busy, or when a sent resume never
brought claude up. A pane list without the agent never removes one. herdr fires
no events while shutting down, so what is recorded then is what was running.
"""
import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
DEFAULTS = {"min_interval_seconds": 5, "command": "claude --resume {id}"}
READY_TIMEOUT = 20  # restored shell must reach an idle prompt within this
START_TIMEOUT = 30  # cap on waiting for a resumed claude to finish starting
POLL = 0.3
CAPTURE_WINDOW = 60  # after startup, keep refreshing unfocused panes' prompt references this long
EXITED = "command exited, shell idle"

HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"
SOCK = os.environ.get("HERDR_SOCKET_PATH", "")
BASE = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.expanduser("~/.local/state/herdr/plugins/local.lazy-resume")
CONF = os.path.join(os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or BASE, "config.json")
DIR = os.path.join(BASE, hashlib.sha1(SOCK.encode()).hexdigest()[:12])
STATE_LOCK = os.path.join(DIR, "state.lock")
START_LOCK = os.path.join(BASE, "start.lock")  # machine-wide: keychain contention is not per session
LAST_START = os.path.join(BASE, "last_start")


# ---------- pure logic (see test_lazy_resume.py) ----------

def is_uuid(s):
    return isinstance(s, str) and UUID_RE.fullmatch(s) is not None


def line_hash(line):
    # Whitespace-normalised so a right prompt re-padded on resize still matches.
    # Only equality matters; the screen text itself is not kept.
    return hashlib.sha1(" ".join(line.split()).encode()).hexdigest()[:16]


def claim_boot(state, boot, live):
    """First call after a server start: every recorded claude pane that came
    back as a plain shell becomes pending for this boot."""
    if state.get("boot") == boot:
        return False
    agents = {p["pane_id"]: p.get("agent") for p in live}
    recs = {pid: sid for pid, sid in state.get("panes", {}).items() if pid in agents}
    state.update(boot=boot, panes=recs, prompts={},
                 pending={pid: sid for pid, sid in recs.items() if sid and not agents[pid]})
    return True


def rename(state, old, new):
    for key in ("panes", "pending", "prompts"):
        d = state.get(key, {})
        if old in d:
            d[new] = d.pop(old)


def sync(state, live, gone=()):
    """Update records from an authoritative pane list. `gone` lists panes whose
    claude was released or which were closed (from the triggering event)."""
    recs = state.setdefault("panes", {})
    pending = state.setdefault("pending", {})
    ids = {p["pane_id"] for p in live}
    old_by_term = {t: pid for pid, t in state.get("terms", {}).items() if t}
    for p in live:  # a pane moved to another workspace gets a new id, same terminal
        old = old_by_term.get(p.get("terminal_id"))
        if old and old != p["pane_id"] and old not in ids and old not in gone:
            rename(state, old, p["pane_id"])
    state["terms"] = {p["pane_id"]: p.get("terminal_id") for p in live}
    for p in live:
        pid, agent = p["pane_id"], p.get("agent")
        if agent == "claude":
            pending.pop(pid, None)
            s = p.get("agent_session") or {}
            sid = s.get("value") if s.get("agent") == "claude" else None
            recs[pid] = sid if is_uuid(sid) else recs.get(pid)
        elif agent:  # another agent took the pane
            pending.pop(pid, None)
            recs.pop(pid, None)
    # herdr clears agent/agent_session before it emits the release (checked from
    # inside the hook), and lists are read under the state lock, so a later
    # list can't re-add a released pane.
    forget(state, gone)


def forget(state, pids):
    for pid in pids:
        for key in ("panes", "pending", "prompts"):
            state.get(key, {}).pop(pid, None)


def load_config(path):
    cfg = dict(DEFAULTS)
    try:
        with open(path) as f:
            user = json.load(f)
    except (OSError, ValueError):
        return cfg
    if not isinstance(user, dict):
        return cfg
    iv = user.get("min_interval_seconds")
    if isinstance(iv, (int, float)) and not isinstance(iv, bool) and math.isfinite(iv) and iv >= 0:
        cfg["min_interval_seconds"] = iv
    cmd = user.get("command")
    if isinstance(cmd, str) and "{id}" in cmd and "\n" not in cmd:
        cfg["command"] = cmd
    return cfg


def build_command(template, sid):
    # The id goes into a shell line typed into the pane: accept only a UUID.
    return template.replace("{id}", sid) if is_uuid(sid) else None


# ---------- herdr / filesystem ----------

def herdr(*args):
    try:
        r = subprocess.run([HERDR, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def herdr_json(*args):
    out = herdr(*args)
    try:
        res = json.loads(out)["result"] if out else None
    except (ValueError, KeyError, TypeError):
        return None
    return res if isinstance(res, dict) else None


def log(msg):
    os.makedirs(DIR, exist_ok=True)
    with open(os.path.join(DIR, "lazy-resume.log"), "a") as f:
        f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))


@contextmanager
def locked(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def load():
    try:
        with open(os.path.join(DIR, "state.json")) as f:
            state = json.load(f)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save(state):
    path = os.path.join(DIR, "state.json")
    with open(path + ".tmp", "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(path + ".tmp", path)


def boot_id():
    try:
        st = os.stat(SOCK)
    except OSError:
        return None
    return "%d-%d" % (st.st_ino, st.st_mtime_ns)


def last_start():
    try:
        with open(LAST_START) as f:
            t = float(f.read())
    except (OSError, ValueError):
        return 0.0
    return t if 0 <= t <= time.time() else 0.0  # ignore a future/garbage stamp (clock step)


def set_last_start(t):
    with open(LAST_START + ".tmp", "w") as f:
        f.write(repr(t))
    os.replace(LAST_START + ".tmp", LAST_START)


def refresh(gone=()):
    """Claim the boot if new, then re-sync records from an authoritative pane list."""
    with locked(STATE_LOCK):
        boot = boot_id()
        live = (herdr_json("pane", "list") or {}).get("panes")
        if boot is None:
            return None
        state = load()
        if not isinstance(live, list):
            if gone and state.get("boot") == boot:  # don't lose an exit/close if the list read failed
                forget(state, gone)
                save(state)
            return None
        state["socket"] = SOCK
        if claim_boot(state, boot, live):
            log("boot %s: pending %s" % (boot, sorted(state["pending"]) or "none"))
        sync(state, live, gone)
        save(state)
        return state, live


def pane_status(pid):
    """(focused, idle_shell, last_visible_line) or None if unreadable."""
    pane = (herdr_json("pane", "get", pid) or {}).get("pane")
    info = (herdr_json("pane", "process-info", "--pane", pid) or {}).get("process_info")
    txt = herdr("pane", "read", pid, "--source", "visible")
    if not isinstance(pane, dict) or not isinstance(info, dict) or txt is None:
        return None
    shell = info.get("shell_pid")
    idle = (bool(shell) and not pane.get("agent")
            and info.get("foreground_process_group_id") == shell
            and all(p.get("pid") == shell for p in info.get("foreground_processes") or []))
    lines = [l.rstrip() for l in txt.splitlines() if l.strip()]
    return bool(pane.get("focused")), idle, lines[-1] if lines else ""


def focused(pid):
    return bool(((herdr_json("pane", "get", pid) or {}).get("pane") or {}).get("focused"))


def wait_ready(pid, settle):
    """Wait for an idle shell prompt in the focused pane. settle=True (shell may
    still be initialising) also wants the line stable across two polls."""
    prev, deadline = None, time.time() + READY_TIMEOUT
    while time.time() < deadline:
        st = pane_status(pid)
        if st:
            is_focused, idle, line = st
            if not is_focused:
                return "unfocused", None
            if idle and line and (not settle or line == prev):
                return "ready", line
            prev = line if idle else None
        time.sleep(POLL)
    return "timeout", None


def wait_started(pid, sent_at):
    """Hold the start lock until the resumed claude is up (or the command ended)."""
    while time.time() < sent_at + START_TIMEOUT:
        time.sleep(POLL)
        try:
            pane = (herdr_json("pane", "get", pid) or {}).get("pane") or {}
            if pane.get("agent") == "claude" and pane.get("agent_status") not in (None, "unknown"):
                return "claude ready (%s)" % pane.get("agent_status")
            st = pane_status(pid) if time.time() > sent_at + 1 else None
            if st and st[1]:
                return EXITED
        except Exception:  # keep holding the lock until the cap rather than release early
            pass
    return "start timeout"


def update_state(boot, fn):
    """Apply fn(state) under the lock if the boot still matches; returns fn's result."""
    with locked(STATE_LOCK):
        state = load()
        if state.get("boot") != boot:
            return None
        res = fn(state)
        save(state)
        return res


def drop(pid, boot, reason, sid=None, keep_record=False):
    """Skip pid for this boot. Its record goes too (no claude runs there now, so
    it is no target at the next restart) unless keep_record, or an agent occupies
    the pane (sync owns that record)."""
    agent = ((herdr_json("pane", "get", pid) or {}).get("pane") or {}).get("agent")

    def fn(s):
        got = s.get("pending", {}).pop(pid, None)
        if keep_record or agent:
            return got or s.get("panes", {}).get(pid)
        return s.get("panes", {}).pop(pid, None) or got

    got = update_state(boot, fn)
    log("%s: skipped (%s); session %s %s" % (pid, reason, sid or got,
        "kept for the next restart" if keep_record and not agent else "not resumed"))


def capture_refs(boot, pids):
    """Startup: while a pending pane is unfocused (it cannot receive keystrokes),
    keep its latest stable idle prompt line as the reference for "empty
    prompt". Watching for CAPTURE_WINDOW absorbs late shell-startup output and
    the redraw when a client attaches at another size."""
    def store(pid, h):
        def fn(s):
            if pid in s.get("pending", {}):
                s.setdefault("prompts", {})[pid] = h
        update_state(boot, fn)

    def one(pid):
        prev, end = None, time.time() + CAPTURE_WINDOW
        while time.time() < end:
            st = pane_status(pid)
            if st:
                is_focused, idle, line = st
                if is_focused:
                    return  # from here the focus hook owns the pane
                if idle and line and line == prev:
                    store(pid, line_hash(line))
                prev = line if idle else None
            time.sleep(1)

    threads = [threading.Thread(target=one, args=(pid,)) for pid in pids]
    for t in threads:
        t.start()
    return threads


def try_resume(pid, boot, settle):
    cfg = load_config(CONF)
    outcome, line = wait_ready(pid, settle)
    if outcome == "unfocused":
        return  # stays pending; the next focus retries
    if outcome == "timeout":
        return drop(pid, boot, "shell not at an idle prompt")
    # Reference for "empty prompt": captured at startup while unfocused, else the
    # first idle line seen on focus.
    base = update_state(boot, lambda s: s.setdefault("prompts", {}).setdefault(pid, line_hash(line))
                        if pid in s.get("pending", {}) else None)
    if base is None:
        return  # no longer pending (resumed by another hook, or new boot)
    if line_hash(line) != base:
        return drop(pid, boot, "prompt line differs from its reference", keep_record=True)

    fd = os.open(START_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while True:  # queue: wait our turn, but give up (stay pending) if focus leaves
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not focused(pid):
                    return
                time.sleep(POLL)
        while time.time() < last_start() + cfg["min_interval_seconds"]:
            if not focused(pid):
                return
            time.sleep(POLL)
        if boot_id() != boot:
            return  # server restarted under us

        # once per boot: take it off the list first, then check and send with no lock in between
        sid = update_state(boot, lambda s: s.get("pending", {}).pop(pid, None))
        if not sid:
            return
        st = pane_status(pid)
        settle_until = time.time() + 1.5  # a prompt redraw (theme running git) looks busy briefly
        while st and st[0] and not st[1] and time.time() < settle_until:
            time.sleep(POLL)
            st = pane_status(pid)
        if not st or not st[0]:
            update_state(boot, lambda s: s.setdefault("pending", {}).setdefault(pid, sid))
            return
        if not st[1]:
            return drop(pid, boot, "pane busy when its turn came", sid)
        if line_hash(st[2]) != base:
            return drop(pid, boot, "prompt line changed while queued", sid, keep_record=True)
        cmd = build_command(cfg["command"], sid)
        if not cmd:
            return
        sent_at = time.time()
        set_last_start(sent_at)
        sent = herdr("pane", "run", pid, cmd) is not None
        log("%s: %s for %s" % (pid, "sent resume" if sent else
                                "pane run reported failure (still waiting before releasing the start lock)", sid))
        outcome = wait_started(pid, sent_at)
        log("%s: %s after %.1fs" % (pid, outcome, time.time() - sent_at))
        if outcome == EXITED and sent:  # claude never came up: don't retype it after every restart
            update_state(boot, lambda s: s.get("panes", {}).pop(pid, None))
        refresh()
    finally:
        os.close(fd)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    name = os.environ.get("HERDR_PLUGIN_EVENT", "")
    try:
        data = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}").get("data")
    except (ValueError, AttributeError):
        data = None
    data = data if isinstance(data, dict) else {}
    gone = []
    if (name == "pane.agent_detected" and data.get("released") is True) or name == "pane.closed":
        if isinstance(data.get("pane_id"), str):
            gone.append(data["pane_id"])
    res = refresh(gone)
    if not res:
        return
    state, live = res
    pending = state.get("pending", {})
    if mode == "startup":
        target = next((p["pane_id"] for p in live if p.get("focused")), None)
        threads = capture_refs(state["boot"], [pid for pid in pending if pid != target])
        if target in pending:
            try_resume(target, state["boot"], settle=True)
        for t in threads:
            t.join()
    elif name == "pane.focused" and data.get("pane_id") in pending:
        try_resume(data["pane_id"], state["boot"], settle=False)


if __name__ == "__main__":
    main()
