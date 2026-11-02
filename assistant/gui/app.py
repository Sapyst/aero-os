#!/usr/bin/env python3
"""Aero Assistant: the window. All logic lives in the aero-assistant engine, so the terminal and
this app share the same triggers, guard and tools. Models and the engine download only on consent."""
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

HOME = os.path.expanduser("~")
DATA = os.path.join(HOME, ".local/share/aero-assistant")
MODELS = os.path.join(DATA, "models")
SERVER = os.path.join(DATA, "janas/janas/janas-server")
CONF = os.path.join(HOME, ".config/aero-assistant/model.conf")
ENGINE = os.path.join(HOME, ".local/bin/aero-assistant")
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

CSS = """
.msg { border: 1px solid alpha(currentColor, 0.16); border-radius: 12px; padding: 10px 14px;
       background: alpha(currentColor, 0.04); }
.msg.me { background: alpha(currentColor, 0.10); }
.msg.note { border-style: dashed; background: transparent; }
.msg.proposal { border-color: alpha(currentColor, 0.35); }
.msg.high { border-color: @error_color; }
.who { font-family: monospace; font-size: 0.82em; opacity: 0.6; }
.body { font-size: 1.02em; }
.tool { font-family: monospace; font-size: 0.82em; opacity: 0.6; }
.code { font-family: monospace; }
.composer { border: 1px solid alpha(currentColor, 0.18); border-radius: 14px; padding: 6px;
            background: alpha(currentColor, 0.05); }
.composer textview, .composer textview text { background: transparent; }
.status { font-family: monospace; font-size: 0.85em; opacity: 0.65; }
.panel { border-left: 1px solid alpha(currentColor, 0.12); padding: 14px; }
.panel-title { font-weight: 600; font-size: 0.85em; opacity: 0.7; }
.metric { font-family: monospace; font-size: 0.85em; }
.badge { border: 1px solid alpha(currentColor, 0.3); border-radius: 8px; padding: 1px 7px; font-size: 0.8em; }
.warn { color: @warning_color; }
"""


def on_ui(fn, *args):
    GLib.idle_add(lambda: (fn(*args), False)[1])


def read_catalog():
    with open(os.path.join(DATA, "models.json")) as f:
        return json.load(f)


def read_conf():
    conf = {}
    try:
        with open(CONF) as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    conf[k] = v
    except OSError:
        pass
    return conf


def write_conf(conf):
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    with open(CONF, "w") as f:
        for k, v in conf.items():
            f.write(f"{k}={v}\n")


def gb(size):
    return f"{size / 1024 ** 3:.1f} GB" if size >= 1024 ** 3 else f"{size / 1024 ** 2:.0f} MB"


def free_disk():
    os.makedirs(DATA, exist_ok=True)
    return os.statvfs(DATA).f_bavail * os.statvfs(DATA).f_frsize


def total_memory_gb():
    with open("/proc/meminfo") as f:
        return int(f.readline().split()[1]) / 1024 ** 2


def sha256(path, progress=None):
    h = hashlib.sha256()
    size = max(os.path.getsize(path), 1)
    done = 0
    with open(path, "rb") as f:
        while chunk := f.read(1 << 24):
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done / size)
    return h.hexdigest()


def download(url, target, expected, size, progress, cancelled):
    """Downloads with resume through curl and checks the hash. Returns (ok, message, hash)."""
    part = target + ".part"
    os.makedirs(os.path.dirname(target), exist_ok=True)
    proc = subprocess.Popen(["curl", "-fsSL", "--proto", "=https", "--proto-redir", "=https", "-C", "-", "--retry", "5", "-o", part, url], stderr=subprocess.PIPE)
    last_size, last_time, speed = 0, time.time(), 0
    while proc.poll() is None:
        if cancelled():
            proc.terminate()
            proc.wait()
            return False, "Paused. Download again to resume.", ""
        time.sleep(0.5)
        have = os.path.getsize(part) if os.path.exists(part) else 0
        now = time.time()
        if now - last_time >= 1:
            speed = (have - last_size) / (now - last_time)
            last_size, last_time = have, now
        progress(have / size if size else 0, f"{gb(have)} of {gb(size) if size else '?'}" + (f", {gb(speed)}/s, {int((size - have) / speed / 60)} min left" if speed > 0 and size else ""))
    if proc.returncode != 0:
        return False, proc.stderr.read().decode(errors="replace").strip() or f"curl failed ({proc.returncode})", ""
    progress(1, "Checking the file...")
    actual = sha256(part)
    if expected and actual != expected:
        os.remove(part)
        return False, "The file did not match its published hash, so it was removed.", actual
    os.replace(part, target)
    return True, "", actual


def install_engine(catalog, progress, cancelled):
    e = catalog["engine"]
    archive = os.path.join(DATA, "janas.tar.gz")
    ok, msg, _ = download(e["url"], archive, e["sha256"], e["size"], progress, cancelled)
    if not ok:
        return ok, msg
    dest = os.path.join(DATA, "janas")
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(archive) as t:
        t.extractall(dest, filter="data")
    os.remove(archive)
    return True, ""


def label(text="", css=None, **kw):
    w = Gtk.Label(label=text, xalign=0, **kw)
    if css:
        w.add_css_class(css)
    return w


class Bubble(Gtk.Revealer):
    def __init__(self, who, text="", me=False, note=False):
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_UP, transition_duration=200)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, halign=Gtk.Align.END if me else Gtk.Align.START)
        self.box.add_css_class("msg")
        if me:
            self.box.add_css_class("me")
        if note:
            self.box.add_css_class("note")
        self.box.append(label(who, "who"))
        self.tools = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.box.append(self.tools)
        self.text = text
        self.body = label(text, "body", wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True, max_width_chars=70)
        self.body.set_visible(bool(text))
        self.box.append(self.body)
        self.set_child(self.box)
        GLib.idle_add(lambda: (self.set_reveal_child(True), False)[1])

    def add_text(self, piece):
        self.text += piece
        self.body.set_text(self.text)
        self.body.set_visible(True)

    def polish(self):
        clean = re.sub(r"\*\*(.+?)\*\*", r"\1", self.text).replace("`", "")
        clean = re.sub(r"^#+ *(\S)", lambda m: m.group(1).upper(), clean, flags=re.M).strip()
        self.body.set_text(clean)
        self.body.set_visible(bool(clean))

    def add_tool(self, line):
        self.tools.append(label(line, "tool", wrap=True, wrap_mode=Pango.WrapMode.CHAR, max_width_chars=70))


class ProposalCard(Gtk.Revealer):
    def __init__(self, event, answer):
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_UP, transition_duration=200)
        self.answer = answer
        high = event.get("high")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.START)
        box.add_css_class("msg")
        box.add_css_class("proposal")
        box.append(label("Proposed command", "who"))
        box.append(label(event["command"], "code", wrap=True, wrap_mode=Pango.WrapMode.CHAR, selectable=True, max_width_chars=70))
        box.append(label(event.get("reason", ""), wrap=True, max_width_chars=70))
        buttons = Gtk.Box(spacing=8)
        self.run = Gtk.Button(label="Run")
        self.cancel = Gtk.Button(label="Cancel")
        self.run.connect("clicked", lambda *_: self.reply("YES" if high else "yes"))
        self.cancel.connect("clicked", lambda *_: self.reply("no"))
        if high:
            box.add_css_class("high")
            box.append(label(f"High risk: {event.get('why', '')}.", "warn", wrap=True, max_width_chars=70))
            self.confirm = Gtk.Entry(placeholder_text="Type YES to enable Run")
            self.run.set_sensitive(False)
            self.run.add_css_class("destructive-action")
            self.confirm.connect("changed", lambda e: self.run.set_sensitive(e.get_text() == "YES"))
            box.append(self.confirm)
        else:
            self.confirm = None
            self.run.add_css_class("suggested-action")
            box.append(label("Risk: normal", "who"))
        buttons.append(self.run)
        buttons.append(self.cancel)
        box.append(buttons)
        self.result = label("", "who")
        box.append(self.result)
        self.set_child(box)
        GLib.idle_add(lambda: (self.set_reveal_child(True), False)[1])

    def reply(self, value):
        self.run.set_sensitive(False)
        self.cancel.set_sensitive(False)
        if self.confirm:
            self.confirm.set_sensitive(False)
        self.answer(value)

    def finish(self, event):
        if not event.get("ran"):
            self.result.set_text("Not run.")
        elif event.get("exit") == 0:
            self.result.set_text("Ran successfully.")
        else:
            self.result.set_text(f"Ran, and failed with exit code {event.get('exit')}.")


class Engine:
    """Runs the engine process and delivers its output on the GTK thread."""

    def __init__(self, handler):
        self.handler = handler
        self.events_r, events_w = os.pipe()
        self.proc = subprocess.Popen([ENGINE, "events"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     preexec_fn=lambda: (os.dup2(events_w, 3), os.set_inheritable(3, True)), close_fds=False)
        os.close(events_w)
        threading.Thread(target=self.pump_events, daemon=True).start()
        threading.Thread(target=self.pump, args=(self.proc.stdout, "text"), daemon=True).start()
        threading.Thread(target=self.pump_lines, args=(self.proc.stderr,), daemon=True).start()

    def pump_events(self):
        with os.fdopen(self.events_r) as f:
            for line in f:
                try:
                    on_ui(self.handler, json.loads(line))
                except ValueError:
                    pass

    def pump(self, stream, kind):
        while True:
            chunk = os.read(stream.fileno(), 4096)
            if not chunk:
                return
            on_ui(self.handler, {"type": kind, "text": ANSI.sub("", chunk.decode(errors="replace"))})

    def pump_lines(self, stream):
        for raw in stream:
            line = ANSI.sub("", raw.decode(errors="replace")).strip()
            if line:
                on_ui(self.handler, {"type": "line", "text": line})

    def send(self, obj):
        try:
            self.proc.stdin.write((json.dumps(obj) + "\n").encode())
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass

    def stop_reply(self):
        self.proc.send_signal(signal.SIGINT)

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        threading.Timer(3, lambda: self.proc.poll() is None and self.proc.terminate()).start()


class ChatPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.busy = False
        self.queue = []
        self.release_first = 0
        self.release_all = 0
        self.batch = None
        self.current = None
        self.card = None
        self.tokens = 0
        self.context = None
        self.since = None
        self.state = "Ready"
        self.first = True

        panes = Gtk.Box()
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=16, margin_bottom=8, margin_start=24, margin_end=24, valign=Gtk.Align.END)
        self.scroll = Gtk.ScrolledWindow(child=self.list, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.scroll.get_vadjustment().connect("notify::upper", lambda a, _p: a.set_value(a.get_upper()))
        left.append(self.scroll)
        self.warning = label("", "warn", margin_start=26, margin_bottom=2, wrap=True, visible=False)
        left.append(self.warning)
        self.status = label("Ready", "status", margin_start=26, margin_bottom=4, ellipsize=Pango.EllipsizeMode.END)
        left.append(self.status)

        quick = Gtk.Box(spacing=6, margin_start=20, margin_end=20, margin_bottom=6)
        for text, request in (("Errors", "are there any errors"), ("Report", "make a system report"), ("Updates", "are there any updates")):
            b = Gtk.Button(label=text)
            b.add_css_class("flat")
            b.connect("clicked", lambda _b, r=request: self.submit(r))
            quick.append(b)
        self.send_one = Gtk.Button(label="Send next", visible=False)
        self.send_one.connect("clicked", lambda *_: self.command("-send"))
        self.send_all = Gtk.Button(label="Send all", visible=False)
        self.send_all.connect("clicked", lambda *_: self.command("--send"))
        self.clear_queue = Gtk.Button(label="Clear queue", visible=False)
        self.clear_queue.add_css_class("flat")
        self.clear_queue.connect("clicked", lambda *_: self.command("-clear"))
        spacer = Gtk.Box(hexpand=True)
        for w in (spacer, self.send_one, self.send_all, self.clear_queue):
            quick.append(w)
        left.append(quick)

        comp = Gtk.Box(spacing=6, margin_start=20, margin_end=20, margin_bottom=16)
        comp.add_css_class("composer")
        self.entry = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, hexpand=True, top_margin=8, bottom_margin=8, left_margin=6, right_margin=6, accepts_tab=False)
        box = Gtk.ScrolledWindow(child=self.entry, hexpand=True, propagate_natural_height=True, max_content_height=180, hscrollbar_policy=Gtk.PolicyType.NEVER)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        self.entry.add_controller(keys)
        self.button = Gtk.Button(icon_name="go-up-symbolic", valign=Gtk.Align.END, tooltip_text="Send (Enter)")
        self.button.add_css_class("suggested-action")
        self.button.add_css_class("circular")
        self.button.connect("clicked", lambda *_: self.stop() if self.busy and not self.text() else self.submit_entry())
        comp.append(box)
        comp.append(self.button)
        left.append(comp)
        panes.append(left)

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, width_request=270)
        panel.add_css_class("panel")
        panel.append(label("PROCESSES", "panel-title"))
        self.rows = label("measuring...", "metric")
        panel.append(self.rows)
        panel.append(label("HARDWARE", "panel-title", margin_top=10))
        self.hardware = label("measuring...", "metric", wrap=True, wrap_mode=Pango.WrapMode.WORD)
        panel.append(self.hardware)
        panel.append(label("MODEL", "panel-title", margin_top=10))
        self.model = label("", "metric", wrap=True, wrap_mode=Pango.WrapMode.CHAR)
        panel.append(self.model)
        panes.append(panel)
        self.append(panes)
        self.engine = Engine(self.on_event)
        self.refresh_model()
        GLib.timeout_add_seconds(1, self.tick)

    def text(self):
        b = self.entry.get_buffer()
        return b.get_text(b.get_start_iter(), b.get_end_iter(), False).strip()

    def refresh_model(self):
        conf = read_conf()
        file = conf.get("file", "qwen3.6-35b-a3b-q4km.jns")
        self.model.set_text(f"{conf.get('name', 'Qwen3.6-35B-A3B')}\n{conf.get('tier', 'Tested')}\ncontext {conf.get('context', '16384')}\n{file}")
        notes = []
        if conf.get("tier", "Tested") != "Tested":
            notes.append(f"{conf.get('tier')} model: answers untested.")
        if conf.get("tools") == "off":
            notes.append("This model failed the tool check, so it cannot read your system. Treat answers as unverified.")
        self.warning.set_text(" ".join(notes))
        self.warning.set_visible(bool(notes))

    def on_key(self, _c, keyval, _code, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not state & Gdk.ModifierType.SHIFT_MASK:
            self.submit_entry()
            return True
        return False

    def submit_entry(self):
        text = self.text()
        self.entry.get_buffer().set_text("")
        if text:
            self.submit(text)

    def command(self, text):
        if text == "-send":
            if self.queue:
                self.release_first += 1
        elif text == "--send":
            self.release_all = len(self.queue)
        elif text == "-clear":
            self.queue.clear()
            self.release_first = self.release_all = 0
        self.show_status()
        self.pump_queue()

    def submit(self, text):
        if text in ("-send", "--send", "-clear"):
            self.command(text)
        elif self.busy:
            self.queue.append(text)
            self.show_status()
        else:
            self.dispatch(text, None)

    def dispatch(self, text, cascade):
        self.list.append(Bubble("you", text, me=True))
        self.current = None
        self.busy = True
        self.tokens = 0
        self.since = time.time()
        request = {"type": "message", "text": text}
        if cascade:
            request["cascade"] = cascade
        self.engine.send(request)
        self.update_button()
        self.show_status()

    def pump_queue(self):
        if self.busy or not self.queue:
            return
        if self.release_all > 0:
            self.release_all -= 1
            if not self.batch:
                self.batch = [0, len(self.queue)]
        elif self.release_first > 0:
            self.release_first -= 1
            self.batch = None
        else:
            self.batch = None
            return
        cascade = None
        if self.batch:
            self.batch[0] += 1
            cascade = list(self.batch)
            if self.release_all == 0:
                self.batch = None
        self.dispatch(self.queue.pop(0), cascade)

    def stop(self):
        self.engine.stop_reply()

    def reply_bubble(self):
        if self.current is None:
            self.current = Bubble("assistant")
            self.list.append(self.current)
        return self.current

    def update_button(self):
        self.button.set_icon_name("media-playback-stop-symbolic" if self.busy else "go-up-symbolic")
        self.button.set_tooltip_text("Stop the reply" if self.busy else "Send (Enter)")

    def tick(self):
        if self.busy:
            self.show_status()
        return True

    def show_status(self):
        parts = [self.state]
        if self.busy and self.since:
            parts[0] += f" {int(time.time() - self.since)}s"
        if self.tokens:
            parts.append(f"{self.tokens} tokens")
        parts.append(f"context {self.context if self.context is not None else '-'}/{read_conf().get('context', '16384')}")
        parts.append(f"queue {len(self.queue)}")
        if self.queue:
            parts.append(f"next: {self.queue[0]}")
        self.status.set_text(" · ".join(parts))
        waiting = bool(self.queue)
        for w in (self.send_one, self.send_all, self.clear_queue):
            w.set_visible(waiting)

    def on_event(self, ev):
        kind = ev.get("type")
        if kind == "text":
            text = re.sub(r"^(RAN, exit=-?\d+|NOT RUN:.*)\n?", "", ev["text"], flags=re.M)
            if text:
                self.reply_bubble().add_text(text)
        elif kind == "line":
            line = ev["text"]
            if not line.startswith(("(rule match", "Starting the local model")):
                self.reply_bubble().add_tool(line)
        elif kind == "status":
            self.state = ev["text"]
            self.show_status()
        elif kind == "token":
            self.tokens += 1
        elif kind == "context":
            self.context = ev["used"]
        elif kind == "metrics":
            lines = []
            for r in ev["rows"]:
                pid = f" {r['pid']}" if r.get("pid") else ""
                lines.append(f"{r['name']}{pid}\n  {r['memory']}  {r['cpu']}")
                if r.get("detail"):
                    lines.append(f"  {r['detail']}")
            self.rows.set_text("\n".join(lines))
            self.hardware.set_text(ev["hardware"].replace(" | ", "\n"))
        elif kind == "proposal":
            self.card = ProposalCard(ev, lambda a: self.engine.send({"type": "approval", "answer": a}))
            self.list.append(self.card)
            self.current = None
            self.state = "Waiting for your approval"
            self.show_status()
        elif kind == "ran":
            if self.card:
                self.card.finish(ev)
        elif kind == "error":
            self.list.append(Bubble("error", ev["text"], note=True))
        elif kind == "interrupted":
            self.list.append(Bubble("assistant", "Stopped.", note=True))
        elif kind == "done":
            if self.current:
                self.current.polish()
            self.busy = False
            self.state = "Ready"
            self.since = None
            self.current = None
            self.update_button()
            self.show_status()
            self.pump_queue()


class ModelsPage(Adw.PreferencesPage):
    def __init__(self, win):
        super().__init__(title="Models", icon_name="emblem-system-symbolic")
        self.win = win
        self.group = None
        self.custom_group = None
        self.cancel_flag = False
        self.refresh()

    def refresh(self):
        if self.group:
            self.remove(self.group)
        if self.custom_group:
            self.remove(self.custom_group)
        catalog = read_catalog()
        conf = read_conf()
        active = conf.get("file", "qwen3.6-35b-a3b-q4km.jns")
        self.group = Adw.PreferencesGroup(title="Models", description=f"Files in {MODELS}. Only Tested models passed the full test suite.")
        known = set()
        for m in catalog["models"]:
            known.add(m["file"])
            self.group.add(self.row(m, active))
        extra = []
        if os.path.isdir(MODELS):
            extra = sorted(f for f in os.listdir(MODELS) if f.endswith((".jns", ".gguf")) and f not in known)
        for f in extra:
            self.group.add(self.row({"name": f, "file": f, "tier": "Custom", "size": os.path.getsize(os.path.join(MODELS, f)), "context": 8192, "license": "not checked", "memory_gb": 0}, active))
        self.add(self.group)
        self.custom_group = Adw.PreferencesGroup(title="Add from a link", description="HTTPS only. A file from an unknown source can be harmful: the model reader may have flaws, and answers are untested.")
        entry = Adw.EntryRow(title="HTTPS link to a .jns file")
        button = Gtk.Button(label="Download", valign=Gtk.Align.CENTER)
        button.connect("clicked", lambda *_: self.download_link(entry.get_text().strip()))
        entry.add_suffix(button)
        self.custom_group.add(entry)
        self.add(self.custom_group)

    def row(self, m, active):
        path = os.path.join(MODELS, m["file"])
        present = os.path.exists(path)
        row = Adw.ActionRow(title=m["name"], subtitle=f"{gb(m.get('size', 0))} · needs {m.get('memory_gb', '?')} GB RAM · license {m.get('license')}")
        badge = label(m["tier"], "badge", valign=Gtk.Align.CENTER)
        row.add_suffix(badge)
        if present:
            if m["file"] == active:
                row.add_suffix(Gtk.Image(icon_name="object-select-symbolic"))
            else:
                use = Gtk.Button(label="Use", valign=Gtk.Align.CENTER)
                use.connect("clicked", lambda *_: self.select(m))
                row.add_suffix(use)
        else:
            get = Gtk.Button(label="Download", valign=Gtk.Align.CENTER)
            get.connect("clicked", lambda *_: self.win.show_download(m))
            row.add_suffix(get)
        return row

    def select(self, m):
        write_conf({"name": m["name"], "file": m["file"], "context": m.get("context", 8192), "tier": m["tier"]})
        subprocess.run([ENGINE, "stop"], check=False)
        self.win.chat.refresh_model()
        self.refresh()
        self.win.toast(f"Using {m['name']}. It opens with the next question.")
        if m["tier"] != "Tested":
            threading.Thread(target=self.tool_check, args=(m,), daemon=True).start()

    def tool_check(self, m):
        """A quick check: can this model read a real value through the tools?"""
        truth = os.uname().release
        try:
            out = subprocess.run([ENGINE, "--model", "--no-exec", "What kernel am I running?"], capture_output=True, text=True, timeout=240, stdin=subprocess.DEVNULL).stdout
        except subprocess.TimeoutExpired:
            out = ""
        passed = truth in out
        conf = read_conf()
        if conf.get("file") != m["file"]:
            return
        if not passed:
            conf["tools"] = "off"
            write_conf(conf)
        on_ui(self.win.chat.refresh_model)
        on_ui(self.win.toast, f"{m['name']}: tool check {'passed' if passed else 'failed, tools turned off'}.")

    def download_link(self, url):
        parsed = urllib.parse.urlparse(url)
        name = os.path.basename(parsed.path)
        if parsed.scheme != "https" or not name.endswith(".jns") or "/" in name or name.startswith("."):
            self.win.toast("Use an HTTPS link to a .jns file.")
            return
        self.win.show_download({"name": name, "file": name, "url": url, "sha256": "", "size": 0, "memory_gb": "?", "context": 8192, "license": "not checked", "tier": "Custom"})


class Welcome(Gtk.Box):
    """The consent screen. Nothing downloads until the button is pressed."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, width_request=520)
        self.win = win
        self.cancelled = False
        title = label("Aero Assistant")
        title.add_css_class("title-1")
        self.append(title)
        self.append(label("A local assistant for Arch Linux. It runs on your machine, and nothing changes without your approval.", wrap=True))
        self.info = label("", "metric", margin_top=10)
        self.append(self.info)
        self.warn = label("", "warn", wrap=True)
        self.append(self.warn)
        self.bar = Gtk.ProgressBar(show_text=True, visible=False, margin_top=8)
        self.append(self.bar)
        self.message = label("", wrap=True)
        self.append(self.message)
        buttons = Gtk.Box(spacing=8, margin_top=10)
        self.go = Gtk.Button(label="Download")
        self.go.add_css_class("suggested-action")
        self.go.connect("clicked", lambda *_: self.start())
        self.other = Gtk.Button(label="Choose another")
        self.other.connect("clicked", lambda *_: win.show_models())
        self.later = Gtk.Button(label="Not now")
        self.later.connect("clicked", lambda *_: win.show_chat())
        self.pause = Gtk.Button(label="Pause", visible=False)
        self.pause.connect("clicked", lambda *_: setattr(self, "cancelled", True))
        for b in (self.go, self.other, self.later, self.pause):
            buttons.append(b)
        self.append(buttons)
        self.model = None

    def present_model(self, m):
        self.model = m
        catalog = read_catalog()
        e = catalog["engine"]
        need_engine = not os.path.exists(SERVER)
        total = m.get("size", 0) + (e["size"] if need_engine else 0)
        memory = total_memory_gb()
        lines = [f"Model     {m['name']}  [{m['tier']}]",
                 f"Download  {gb(m['size']) if m.get('size') else 'size unknown'}",
                 f"Free disk {gb(free_disk())}",
                 f"Memory    needs {m.get('memory_gb', '?')} GB, this machine has {memory:.0f} GB",
                 f"Source    {urllib.parse.urlparse(m['url']).netloc}",
                 f"License   {m.get('license')}"]
        if need_engine:
            lines.append(f"Engine    {e['name']} {e['version']}, {e['license']}, {gb(e['size'])}")
        self.info.set_text("\n".join(lines))
        warnings = []
        if total and free_disk() < total * 1.1:
            warnings.append("Not enough free disk.")
        if isinstance(m.get("memory_gb"), int) and memory < m["memory_gb"]:
            warnings.append("This machine has less memory than the model needs.")
        if m["tier"] == "Custom":
            warnings.append("Custom link: the file is not checked against a published hash, and answers are untested.")
        self.warn.set_text(" ".join(warnings))
        self.go.set_sensitive(not (total and free_disk() < total * 1.1))
        self.go.set_visible(True)
        self.message.set_text("")

    def progress(self, fraction, text):
        on_ui(self.bar.set_fraction, min(fraction, 1))
        on_ui(self.bar.set_text, text)

    def start(self):
        self.cancelled = False
        self.go.set_visible(False)
        self.pause.set_visible(True)
        self.bar.set_visible(True)
        threading.Thread(target=self.work, daemon=True).start()

    def work(self):
        catalog = read_catalog()
        m = self.model
        cancelled = lambda: self.cancelled  # noqa: E731
        ok, msg = True, ""
        if not os.path.exists(SERVER):
            self.progress(0, "Downloading the engine")
            ok, msg = install_engine(catalog, self.progress, cancelled)
        digest = ""
        if ok:
            ok, msg, digest = download(m["url"], os.path.join(MODELS, m["file"]), m.get("sha256", ""), m.get("size", 0), self.progress, cancelled)
        on_ui(self.finished, ok, msg, digest)

    def finished(self, ok, msg, digest):
        self.pause.set_visible(False)
        self.go.set_visible(True)
        self.message.set_text(msg)
        if ok:
            self.bar.set_visible(False)
            if digest and not self.model.get("sha256"):
                self.win.toast(f"Saved. SHA-256 recorded on first download: {digest[:16]}...")
            self.win.models.refresh()
            self.win.models.select(self.model)
            self.win.show_chat()


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Aero Assistant", default_width=1100, default_height=760)
        self.chat = ChatPage(self)
        self.models = ModelsPage(self)
        self.welcome = Welcome(self)
        self.pages = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=200)
        self.pages.add_named(self.chat, "chat")
        self.pages.add_named(self.models, "models")
        self.pages.add_named(self.welcome, "welcome")
        self.title_widget = Adw.WindowTitle(title="Aero Assistant", subtitle="")
        head = Adw.HeaderBar(title_widget=self.title_widget)
        new = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="New conversation")
        new.connect("clicked", lambda *_: self.new_chat())
        head.pack_start(new)
        chat_b = Gtk.Button(icon_name="user-available-symbolic", tooltip_text="Conversation")
        chat_b.connect("clicked", lambda *_: self.show_chat())
        head.pack_start(chat_b)
        settings = Gtk.Button(icon_name="emblem-system-symbolic", tooltip_text="Models")
        settings.connect("clicked", lambda *_: self.show_models())
        head.pack_end(settings)
        self.toasts = Adw.ToastOverlay(child=self.pages)
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(head)
        self.set_content(view)
        self.connect("close-request", self.on_close)
        self.first_screen()

    def first_screen(self):
        conf = read_conf()
        active = conf.get("file", "qwen3.6-35b-a3b-q4km.jns")
        if os.path.exists(SERVER) and os.path.exists(os.path.join(MODELS, active)):
            self.show_chat()
        else:
            self.show_download(read_catalog()["models"][0])

    def show_download(self, m):
        self.welcome.present_model(m)
        self.pages.set_visible_child_name("welcome")

    def show_models(self):
        self.models.refresh()
        self.pages.set_visible_child_name("models")

    def show_chat(self):
        self.pages.set_visible_child_name("chat")

    def new_chat(self):
        self.chat.engine.close()
        self.pages.remove(self.chat)
        self.chat = ChatPage(self)
        self.pages.add_named(self.chat, "chat")
        self.show_chat()

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=5))

    def on_close(self, *_):
        self.chat.engine.close()
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.aero.Assistant")

    def do_startup(self):
        Adw.Application.do_startup(self)
        p = Gtk.CssProvider()
        p.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), p, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        win = self.props.active_window or Window(self)
        win.present()
        demo = os.environ.get("AERO_DEMO")
        if demo:
            self.run_demo(win, demo.split("||"))

    def run_demo(self, win, steps):
        """Test hook: types each request after the previous step's delay."""
        def step(i):
            if i >= len(steps):
                return False
            text = steps[i]
            if text.startswith("wait:"):
                GLib.timeout_add_seconds(int(text[5:]), step, i + 1)
                return False
            if text == "models":
                win.show_models()
            elif text == "chat":
                win.show_chat()
            elif text == "approve":
                win.chat.card.reply("yes")
            elif text == "stop":
                win.chat.stop()
            elif text == "download":
                win.welcome.start()
            else:
                win.chat.submit(text)
            GLib.timeout_add(300, step, i + 1)
            return False
        GLib.timeout_add(800, step, 0)


if __name__ == "__main__":
    sys.exit(App().run([]))
