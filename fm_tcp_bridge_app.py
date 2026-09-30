"""
FM TCP Bridge - macOS menu bar app with a live traffic monitor window.

Menu bar icon: ⇄  (shows ⇄! when the last delivery failed)
"""

import datetime
import queue
import subprocess
import threading

import rumps
import objc
from AppKit import (
    NSApp, NSWindow, NSScrollView, NSTextView, NSButton, NSTextField, NSFont, NSColor,
    NSAttributedString, NSForegroundColorAttributeName, NSFontAttributeName,
    NSWindowStyleMaskTitled, NSWindowStyleMaskClosable, NSWindowStyleMaskResizable,
    NSWindowStyleMaskMiniaturizable, NSBackingStoreBuffered, NSMakeRect,
)
from Foundation import NSObject

import bridge_core
from bridge_core import Engine, APP_NAME, CONFIG_PATH, SUPPORT_DIR, LOG_DIR

# autoresizing masks
W_SIZABLE, H_SIZABLE, MIN_Y_MARGIN, MIN_X_MARGIN = 2, 16, 8, 1
MAX_MONITOR_CHARS = 2_000_000
MAX_DETAIL_CHARS = 20_000


# ---------------------------------------------------------------------------
# Monitor window
# ---------------------------------------------------------------------------

class MonitorController(NSObject):
    """Owns the traffic window. All methods run on the main thread."""

    def init(self):
        self = objc.super(MonitorController, self).init()
        if self is None:
            return None
        self.autoscroll = True
        self.show_payloads = True
        self._build()
        return self

    @objc.python_method
    def _build(self):
        w, h = 980, 600
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                 NSWindowStyleMaskResizable | NSWindowStyleMaskMiniaturizable)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, w, h), style, NSBackingStoreBuffered, False)
        win.setTitle_(f"{APP_NAME} — Traffic")
        win.setReleasedWhenClosed_(False)
        win.setMinSize_((520, 300))
        win.center()
        win.setFrameAutosaveName_("FMTcpBridgeMonitor")
        content = win.contentView()

        bar_y = h - 36

        def button(title, x, width, action, switch=False):
            b = NSButton.alloc().initWithFrame_(NSMakeRect(x, bar_y, width, 28))
            b.setTitle_(title)
            if switch:
                b.setButtonType_(3)  # NSButtonTypeSwitch (checkbox)
                b.setState_(1)
            else:
                b.setBezelStyle_(1)  # rounded
            b.setTarget_(self)
            b.setAction_(action)
            b.setAutoresizingMask_(MIN_Y_MARGIN)
            content.addSubview_(b)
            return b

        button("Clear", 10, 80, "clear:")
        button("Copy All", 92, 90, "copyAll:")
        button("Auto-scroll", 196, 110, "toggleScroll:", switch=True)
        button("Show payloads", 310, 130, "togglePayloads:", switch=True)

        self.status = NSTextField.alloc().initWithFrame_(NSMakeRect(450, bar_y + 4, w - 460, 20))
        self.status.setEditable_(False)
        self.status.setBordered_(False)
        self.status.setDrawsBackground_(False)
        self.status.setAlignment_(1)  # right
        self.status.setTextColor_(NSColor.secondaryLabelColor())
        self.status.setAutoresizingMask_(MIN_Y_MARGIN | W_SIZABLE)
        content.addSubview_(self.status)

        sv = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h - 44))
        sv.setHasVerticalScroller_(True)
        sv.setAutoresizingMask_(W_SIZABLE | H_SIZABLE)
        cs = sv.contentSize()
        tv = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, cs.width, cs.height))
        tv.setMinSize_((0.0, cs.height))
        tv.setMaxSize_((1.0e7, 1.0e7))
        tv.setVerticallyResizable_(True)
        tv.setHorizontallyResizable_(False)
        tv.setAutoresizingMask_(W_SIZABLE)
        tv.textContainer().setContainerSize_((cs.width, 1.0e7))
        tv.textContainer().setWidthTracksTextView_(True)
        tv.setEditable_(False)
        tv.setSelectable_(True)
        tv.setRichText_(True)
        tv.setTextContainerInset_((6, 6))
        sv.setDocumentView_(tv)
        content.addSubview_(sv)

        self.window, self.text = win, tv
        self.font = NSFont.monospacedSystemFontOfSize_weight_(11.5, 0.0)
        self.bold = NSFont.monospacedSystemFontOfSize_weight_(11.5, 0.4)
        self.colors = {
            "in": NSColor.systemBlueColor(),
            "out": NSColor.systemPurpleColor(),
            "fm": NSColor.systemGreenColor(),
            "fm_bad": NSColor.systemOrangeColor(),
            "reply": NSColor.systemTealColor(),
            "error": NSColor.systemRedColor(),
            "info": NSColor.secondaryLabelColor(),
            "detail": NSColor.labelColor(),
        }
        self.labels = {
            "in": "◀ IN   ", "out": "▶ FM   ", "fm": "◀ FM   ",
            "reply": "▶ REPLY", "error": "✖ ERROR", "info": "• INFO ",
        }

    # ----- button actions (ObjC selectors) -----
    def clear_(self, sender):
        self.text.setString_("")

    def copyAll_(self, sender):
        self.text.selectAll_(None)
        self.text.copy_(None)
        self.text.setSelectedRange_((0, 0))

    def toggleScroll_(self, sender):
        self.autoscroll = bool(sender.state())

    def togglePayloads_(self, sender):
        self.show_payloads = bool(sender.state())

    # ----- python API -----
    @objc.python_method
    def show(self):
        NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    @objc.python_method
    def set_status(self, s):
        self.status.setStringValue_(s)

    @objc.python_method
    def _append(self, s, color, font):
        attrs = {NSForegroundColorAttributeName: color, NSFontAttributeName: font}
        self.text.textStorage().appendAttributedString_(
            NSAttributedString.alloc().initWithString_attributes_(s, attrs))

    @objc.python_method
    def add_events(self, events):
        storage = self.text.textStorage()
        storage.beginEditing()
        for ev in events:
            kind = ev["kind"]
            color_key = "fm_bad" if kind == "fm" and not ev.get("ok", True) else kind
            color = self.colors.get(color_key, self.colors["info"])
            self._append(f"{ev['time']:%H:%M:%S.%f}"[:-3] + "  ", self.colors["info"], self.font)
            self._append(self.labels.get(kind, kind) + "  ", color, self.bold)
            self._append(ev["text"] + "\n", color, self.font)
            detail = ev.get("detail")
            if detail and self.show_payloads:
                if len(detail) > MAX_DETAIL_CHARS:
                    detail = detail[:MAX_DETAIL_CHARS] + f"\n… ({len(detail)} chars, truncated)"
                body = "    " + detail.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\n    ")
                self._append(body + "\n", self.colors["detail"], self.font)
        excess = storage.length() - MAX_MONITOR_CHARS
        if excess > 0:
            storage.deleteCharactersInRange_((0, excess))
        storage.endEditing()
        if self.autoscroll:
            self.text.scrollRangeToVisible_((self.text.string().length(), 0))


# ---------------------------------------------------------------------------
# Menu bar app
# ---------------------------------------------------------------------------

def _osascript(script):
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def _as_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class BridgeApp(rumps.App):
    def __init__(self):
        super().__init__(APP_NAME, title="⇄", quit_button=None)
        self.events = queue.Queue()
        self.engine = Engine(on_event=self.events.put)
        self.monitor = MonitorController.alloc().init()
        self.last_ok = True

        self.status_item = rumps.MenuItem("Starting…")
        self.queue_item = rumps.MenuItem("Queue: 0 pending")
        self.last_item = rumps.MenuItem("Last: —")
        self.toggle_item = rumps.MenuItem("Stop Listener", callback=self.toggle_listener)
        self.menu = [
            self.status_item, self.queue_item, self.last_item,
            None,
            rumps.MenuItem("Show Traffic Window", callback=self.show_monitor, key="t"),
            self.toggle_item,
            rumps.MenuItem("Retry Queue Now", callback=self.retry_now),
            rumps.MenuItem("Test FileMaker Connection", callback=self.test_fm),
            None,
            rumps.MenuItem("Edit Settings…", callback=self.edit_settings, key=","),
            rumps.MenuItem("Reload Settings (restart listener)", callback=self.reload_settings),
            rumps.MenuItem("Set FileMaker Password…", callback=self.set_password),
            None,
            rumps.MenuItem("Open Queue Folder", callback=lambda _: subprocess.run(["open", SUPPORT_DIR])),
            rumps.MenuItem("Open Log Folder", callback=lambda _: subprocess.run(["open", LOG_DIR])),
            None,
            rumps.MenuItem("Quit", callback=self.quit, key="q"),
        ]
        self.timer = rumps.Timer(self.pump, 0.25)
        self.timer.start()
        self._start()

    # ----- helpers -----
    def _start(self):
        try:
            self.engine.start()
        except OSError as e:
            self.engine.reload_config()
            self.events.put({"kind": "error", "time": datetime.datetime.now(),
                             "text": f"Could not listen on port {self.engine.cfg['listen_port']}: {e}",
                             "detail": None})
        self._refresh()

    def _refresh(self):
        cfg = self.engine.cfg or {}
        if self.engine.running:
            s = f"Listening on {cfg.get('listen_host')}:{cfg.get('listen_port')}"
            self.toggle_item.title = "Stop Listener"
        else:
            s = "Stopped"
            self.toggle_item.title = "Start Listener"
        target = f"{cfg.get('fm_database') or '?'} ▸ {cfg.get('fm_script') or '?'}"
        self.status_item.title = s
        n = self.engine.pending_count()
        self.queue_item.title = f"Queue: {n} pending"
        st = self.engine.stats
        self.monitor.set_status(f"{s}   →  {target}   |   received {st['received']}   "
                                f"delivered {st['delivered']}   errors {st['errors']}   queued {n}")
        self.title = "⇄" if (self.last_ok and self.engine.running) else "⇄!"

    # ----- timer: move engine events onto the main thread -----
    def pump(self, _):
        batch = []
        try:
            while len(batch) < 500:
                batch.append(self.events.get_nowait())
        except queue.Empty:
            pass
        if batch:
            self.monitor.add_events(batch)
            for ev in batch:
                if ev["kind"] == "fm":
                    self.last_ok = ev.get("ok", True)
                    self.last_item.title = f"Last: {ev['time']:%H:%M:%S} " + (
                        "delivered ✓" if self.last_ok else "failed ✖")
                elif ev["kind"] == "error" and "Delivery failed" in ev["text"]:
                    self.last_ok = False
                    self.last_item.title = f"Last: {ev['time']:%H:%M:%S} failed ✖"
        self._refresh()

    # ----- menu actions -----
    def show_monitor(self, _):
        self.monitor.show()

    def toggle_listener(self, _):
        if self.engine.running:
            self.engine.stop()
        else:
            self._start()
        self._refresh()

    def retry_now(self, _):
        self.engine.retry_now()

    def test_fm(self, _):
        self.monitor.show()
        threading.Thread(target=self.engine.test_connection, daemon=True).start()

    def edit_settings(self, _):
        bridge_core.load_config()  # make sure the file exists
        subprocess.run(["open", "-e", CONFIG_PATH])

    def reload_settings(self, _):
        self.engine.stop()
        self._start()
        self.last_ok = True

    def set_password(self, _):
        cfg = self.engine.reload_config()
        user = cfg.get("fm_username")
        if not user:
            rumps.alert(APP_NAME, "Set fm_username in Settings first, then Reload Settings.")
            return
        NSApp.activateIgnoringOtherApps_(True)
        rc, pw = _osascript(
            "text returned of (display dialog " + _as_str(f"FileMaker password for account “{user}”:")
            + ' default answer "" with hidden answer with title ' + _as_str(APP_NAME) + ")")
        if rc != 0:
            return  # cancelled
        try:
            bridge_core.set_password(user, pw)
            self.events.put({"kind": "info", "time": datetime.datetime.now(),
                             "text": f"Saved FileMaker password for {user} in the Keychain",
                             "detail": None})
            self.engine.retry_now()
        except RuntimeError as e:
            rumps.alert(APP_NAME, f"Could not save password: {e}")

    def quit(self, _):
        self.engine.shutdown()
        rumps.quit_application()


if __name__ == "__main__":
    BridgeApp().run()
