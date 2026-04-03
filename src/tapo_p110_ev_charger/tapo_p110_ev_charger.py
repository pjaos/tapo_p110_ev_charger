"""
Tapo P110 EV Charger Controller
NiceGUI 3.9 — explicit @ui.page('/') with a main() entry point.

Features:
  - Charge tab: current/target %, optional scheduled start, live status
  - Auto-stop: turns plug off when calculated charge duration elapses
  - Web Push notification when charging completes
  - Email (SMTP) notification when charging completes
  - History tab: per-session log, daily/weekly bar chart, monthly cost total
  - Config tab: Tapo credentials, EV params, tariff editor, email settings
"""

import json
import asyncio
import smtplib
import ssl
import argparse
import queue
import threading
from datetime import datetime, time as dt_time, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from p3lib.uio import UIO
from p3lib.helper import logTraceBack
from p3lib.boot_manager import BootManager
from p3lib.helper import get_program_version, getHomePath

from nicegui import ui

__version__ = get_program_version('tapo_p110_ev_charger')

# ── Config persistence ────────────────────────────────────────────────────────

def get_app_cfg_path():
    """@return The path into which all app files are saved."""
    app_cfg_path = None
    home_path   = Path(getHomePath())
    cfg_path   = home_path / Path('.config')
    if cfg_path.is_dir():
        app_cfg_path = cfg_path / Path('tapo_p110_ev_charger')
        if not app_cfg_path.is_dir():
            app_cfg_path.mkdir(parents=True, exist_ok=True)

    else:
        app_cfg_path = home_path / Path('.tapo_p110_ev_charger')
        if not app_cfg_path.is_dir():
            app_cfg_path.mkdir(parents=True, exist_ok=True)
    return app_cfg_path

def get_config_file():
    app_cfg_path = get_app_cfg_path()
    return app_cfg_path / Path("tapo_ev_config.json")

def get_history_file():
    app_cfg_path = get_app_cfg_path()
    return app_cfg_path / Path("tapo_ev_history.json")

CONFIG_FILE   = get_config_file()
HISTORY_FILE  = get_history_file()

DEFAULT_CONFIG: dict = {
    "tapo_ip": "",
    "tapo_email": "",
    "tapo_password": "",
    "battery_size_kwh": 60.0,
    "charge_rate_kw": 2.9,
    "tariff_periods": [],
    "notify_email_enabled": False,
    "notify_email_to": "",
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ── History persistence ───────────────────────────────────────────────────────

def load_history() -> list[dict]:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except Exception:
            pass
    return []


def append_history(record: dict) -> None:
    history = load_history()
    history.append(record)
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


# ── Tapo helpers ──────────────────────────────────────────────────────────────

async def tapo_get_device_status(cfg: dict) -> tuple[Optional[bool], Optional[float]]:
    """
    Return (is_on, current_power_watts).
    Both values are None if the device is unreachable or an error occurs.
    Uses get_device_info() for the switch state so that PLUG ON/OFF reflects
    the actual relay state, not just whether the device is reachable.
    """
    try:
        import tapo as tapo_lib  # type: ignore
        client = tapo_lib.ApiClient(cfg["tapo_email"], cfg["tapo_password"])
        device = await client.p110(cfg["tapo_ip"])
        info   = await device.get_device_info()
        power  = await device.get_current_power()
        is_on  = bool(info.to_dict().get("device_on", False))
        watts  = float(power.to_dict()["current_power"])
        return is_on, watts
    except ImportError:
        return None, None
    except Exception:
        import traceback
        traceback.print_exc()
        return None, None


async def tapo_get_power(cfg: dict) -> Optional[float]:
    """Return current power draw in watts, or None on error."""
    try:
        import tapo as tapo_lib  # type: ignore
        client = tapo_lib.ApiClient(cfg["tapo_email"], cfg["tapo_password"])
        device = await client.p110(cfg["tapo_ip"])
        power = await device.get_current_power()
        return float(power.to_dict()["current_power"])
    except ImportError:
        return None
    except Exception:
        return None


async def tapo_turn_on(cfg: dict) -> bool:
    try:
        import tapo as tapo_lib  # type: ignore
        client = tapo_lib.ApiClient(cfg["tapo_email"], cfg["tapo_password"])
        device = await client.p110(cfg["tapo_ip"])
        await device.on()
        return True
    except Exception:
        return False


async def tapo_turn_off(cfg: dict) -> bool:
    try:
        import tapo as tapo_lib  # type: ignore
        client = tapo_lib.ApiClient(cfg["tapo_email"], cfg["tapo_password"])
        device = await client.p110(cfg["tapo_ip"])
        await device.off()
        return True
    except Exception:
        return False


# ── Email notification ────────────────────────────────────────────────────────

def send_email_notification(cfg: dict, kwh: float, cost: float, duration_min: float) -> bool:
    """Send a charge-complete email via SMTP. Returns True on success."""
    try:
        if not cfg.get("notify_email_enabled"):
            return False
        h, m = int(duration_min // 60), int(duration_min % 60)
        dur_str  = f"{h}h {m:02d}m" if h else f"{m}m"
        cost_str = f"£{cost:.2f}" if cost > 0 else "unknown (no tariff set)"
        body = (
            f"Your EV has finished charging.\n\n"
            f"  Energy delivered : {kwh:.2f} kWh\n"
            f"  Duration         : {dur_str}\n"
            f"  Estimated cost   : {cost_str}\n\n"
            f"The Tapo P110 plug has been switched off automatically.\n"
        )
        msg            = MIMEText(body)
        msg["Subject"] = "⚡ EV Charging Complete"
        msg["From"]    = cfg["smtp_user"]
        msg["To"]      = cfg["notify_email_to"]

        context = ssl.create_default_context()
        with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"])) as server:
            server.starttls(context=context)
            server.login(cfg["smtp_user"], cfg["smtp_password"])
            server.sendmail(cfg["smtp_user"], cfg["notify_email_to"], msg.as_string())
        return True
    except Exception:
        return False


# ── Charge session state ──────────────────────────────────────────────────────

class ChargeSession:
    def __init__(self) -> None:
        self.active:          bool               = False
        self.kwh_needed:      float              = 0.0
        self.estimated_cost:  float              = 0.0
        self.duration_min:    float              = 0.0
        self.started_at:      Optional[datetime] = None
        self.charge_end_at:   Optional[datetime] = None
        self._thread:         Optional[threading.Thread] = None
        self._stop_event:     threading.Event    = threading.Event()

    def calc_estimates(self, current_pct: float, target_pct: float, cfg: dict) -> None:
        delta               = max(0.0, target_pct - current_pct)
        self.kwh_needed     = (delta / 100.0) * cfg["battery_size_kwh"]
        rate                = cfg["charge_rate_kw"]
        self.duration_min   = (self.kwh_needed / rate) * 60.0 if rate > 0 else 0.0
        periods             = cfg.get("tariff_periods", [])
        cheapest            = min((p["rate"] for p in periods), default=0.0)
        self.estimated_cost = self.kwh_needed * cheapest

    @property
    def remaining_min(self) -> float:
        if not self.active or self.charge_end_at is None:
            return 0.0
        return max(0.0, (self.charge_end_at - datetime.now()).total_seconds() / 60.0)

    def cancel(self) -> None:
        self._stop_event.set()
        self.active        = False
        self.started_at    = None
        self.charge_end_at = None


session = ChargeSession()


# ── Styles ────────────────────────────────────────────────────────────────────

STYLES = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;1,9..40,300&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --surface2: #1f2733;
    --border: #30363d; --accent: #39d353; --accent2: #58a6ff;
    --warn: #f78166; --text: #e6edf3; --muted: #8b949e; --r: 10px;
  }
  body, .q-page { background: var(--bg) !important; color: var(--text) !important; font-family: 'DM Sans', sans-serif; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--r); padding: 20px; margin-bottom: 16px; }
  .section-title { font-family: 'Space Mono', monospace; font-size: 11px; letter-spacing: 2px; text-transform: uppercase; color: var(--muted); margin-bottom: 12px; }
  .stat-val { font-family: 'Space Mono', monospace; font-size: 26px; color: var(--accent); line-height: 1; }
  .stat-lbl { font-size: 12px; color: var(--muted); margin-top: 4px; }
  .pill { display: inline-block; padding: 2px 10px; border-radius: 20px; font-family: 'Space Mono', monospace; font-size: 11px; font-weight: 700; background: var(--surface2); color: var(--muted); border: 1px solid var(--border); }
  .pill.on  { background: rgba(57,211,83,.15); color: var(--accent); border-color: var(--accent); }
  .pill.off { background: rgba(247,129,102,.12); color: var(--warn); border-color: var(--warn); }
  .tariff-row  { display: flex; align-items: center; gap: 12px; padding: 8px 12px; background: var(--surface2); border-radius: 6px; margin-bottom: 6px; border: 1px solid var(--border); }
  .tariff-time { font-family: 'Space Mono', monospace; color: var(--accent2); min-width: 52px; }
  .tariff-rate { flex: 1; font-family: 'Space Mono', monospace; }
  .history-row  { display: flex; align-items: center; gap: 12px; padding: 8px 12px; background: var(--surface2); border-radius: 6px; margin-bottom: 6px; border: 1px solid var(--border); font-size: 13px; }
  .history-date { font-family: 'Space Mono', monospace; color: var(--muted); min-width: 130px; font-size: 11px; }
  .history-val  { font-family: 'Space Mono', monospace; color: var(--accent2); min-width: 70px; }
  .header-bar { display: flex; align-items: center; gap: 12px; padding: 14px 20px; background: var(--surface); border-bottom: 1px solid var(--border); }
  .app-title { font-family: 'Space Mono', monospace; font-size: 15px; font-weight: 700; color: var(--text); }
  .app-sub   { font-size: 12px; color: var(--muted); }
  .nicegui-input .q-field__control, .nicegui-number .q-field__control { background: var(--surface2) !important; border-radius: 6px !important; color: var(--text) !important; }
  .nicegui-input .q-field__label, .nicegui-number .q-field__label { color: var(--muted) !important; }
  .q-tab { color: var(--muted) !important; font-family: 'Space Mono', monospace; font-size: 12px; letter-spacing: 1px; }
  .q-tab--active { color: var(--accent) !important; }
  .q-tab-indicator { background: var(--accent) !important; }
  .q-tabs { border-bottom: 1px solid var(--border); }
  .accent-btn { background: var(--accent) !important; color: #0d1117 !important; font-family: 'Space Mono', monospace !important; font-weight: 700 !important; border-radius: 6px !important; }
  .danger-btn { background: var(--warn) !important; color: #0d1117 !important; font-family: 'Space Mono', monospace !important; font-weight: 700 !important; border-radius: 6px !important; }
  .ghost-btn  { background: transparent !important; border: 1px solid var(--border) !important; color: var(--text) !important; font-family: 'Space Mono', monospace !important; border-radius: 6px !important; }
  @layer utilities { .w-full { width: 100%; } .flex-1 { flex: 1 1 0%; } }
</style>
"""

WEB_PUSH_JS = """
<script>
async function requestNotifyPermission() {
  if (!('Notification' in window)) return;
  await Notification.requestPermission();
}
function sendBrowserNotification(title, body) {
  if (Notification.permission === 'granted') {
    new Notification(title, { body: body });
  }
}
requestNotifyPermission();
</script>
"""


# ── Page ─────────────────────────────────────────────────────────────────────

@ui.page('/')
def build_page() -> None:
    ui.add_head_html(STYLES)
    ui.add_head_html(WEB_PUSH_JS)

    # ── Header ────────────────────────────────────────────────────────────────
    with ui.element("div").classes("header-bar"):
        ui.icon("electric_car", size="28px").style("color: var(--accent)")
        with ui.column().style("gap:0"):
            ui.html('<span class="app-title">TAPO EV CHARGER</span>')
            ui.html(f'<span class="app-sub">P110 Smart Plug Controller &nbsp;·&nbsp; v{__version__}</span>')

    # ── Tabs ──────────────────────────────────────────────────────────────────
    with ui.tabs().classes("w-full").style("background: var(--surface); padding: 0 16px;") as tabs:
        tab_charge  = ui.tab("CHARGE",  icon="bolt")
        tab_history = ui.tab("HISTORY", icon="bar_chart")
        tab_config  = ui.tab("CONFIG",  icon="settings")

    with ui.tab_panels(tabs, value=tab_charge).style("background: var(--bg); padding: 16px;"):

        # ══════════════════════════════════════════════════════════════════════
        # CHARGE TAB
        # ══════════════════════════════════════════════════════════════════════
        with ui.tab_panel(tab_charge):

            # ── Session setup ─────────────────────────────────────────────────
            with ui.element("div").classes("card"):
                ui.html('<div class="section-title">Session Setup</div>')

                with ui.row().classes("w-full gap-4"):
                    inp_current = ui.number(
                        "Current battery %", value=20, min=0, max=100, step=1, format="%.0f"
                    ).classes("flex-1")
                    inp_target = ui.number(
                        "Target battery %", value=80, min=0, max=100, step=1, format="%.0f"
                    ).classes("flex-1")

                inp_start_time = ui.input(
                    "Start time (HH:MM) — leave blank to start immediately",
                    placeholder="e.g. 23:30"
                ).classes("w-full").style("margin-top:8px")

                with ui.element("div").style("margin-top:16px; display:flex; gap:24px; flex-wrap:wrap;"):
                    lbl_kwh  = ui.html('<div class="stat-val">—</div><div class="stat-lbl">kWh needed</div>')
                    lbl_dur  = ui.html('<div class="stat-val">—</div><div class="stat-lbl">est. duration</div>')
                    lbl_cost = ui.html('<div class="stat-val">—</div><div class="stat-lbl">est. cost</div>')

                def update_estimates() -> None:
                    try:
                        cfg = load_config()
                        cur = float(inp_current.value or 0)
                        tgt = float(inp_target.value or 0)
                        session.calc_estimates(cur, tgt, cfg)
                        kwh      = session.kwh_needed
                        mins     = session.duration_min
                        h, m     = int(mins // 60), int(mins % 60)
                        dur      = f"{h}h {m:02d}m" if h else f"{m}m"
                        cost_str = f"£{session.estimated_cost:.2f}" if session.estimated_cost > 0 else "—"
                        lbl_kwh.set_content(f'<div class="stat-val">{kwh:.1f}</div><div class="stat-lbl">kWh needed</div>')
                        lbl_dur.set_content(f'<div class="stat-val">{dur}</div><div class="stat-lbl">est. duration</div>')
                        lbl_cost.set_content(f'<div class="stat-val">{cost_str}</div><div class="stat-lbl">est. cost</div>')
                    except Exception:
                        pass

                inp_current.on("update:model-value", lambda _: update_estimates())
                inp_target.on("update:model-value",  lambda _: update_estimates())
                update_estimates()

            # ── Live status ───────────────────────────────────────────────────
            with ui.element("div").classes("card"):
                ui.html('<div class="section-title">Charger Status</div>')

                with ui.row().classes("items-center gap-3"):
                    plug_pill = ui.html('<span class="pill off">UNREACHABLE</span>')
                    sess_pill = ui.html('<span class="pill">NO SESSION</span>')

                power_html  = ui.html(
                    '<div class="stat-val" style="margin-top:12px">— W</div>'
                    '<div class="stat-lbl">current draw</div>'
                )
                remain_html = ui.html('')

                async def refresh_status() -> None:
                    cfg = load_config()
                    is_on, pwr = await tapo_get_device_status(cfg)
                    if is_on is None:
                        # Device unreachable
                        plug_pill.set_content('<span class="pill off">UNREACHABLE</span>')
                        power_html.set_content(
                            '<div class="stat-val" style="margin-top:12px">— W</div>'
                            '<div class="stat-lbl">current draw</div>'
                        )
                    elif is_on:
                        plug_pill.set_content('<span class="pill on">PLUG ON</span>')
                        power_html.set_content(
                            f'<div class="stat-val" style="margin-top:12px">{pwr:.0f} W</div>'
                            '<div class="stat-lbl">current draw</div>'
                        )
                    else:
                        plug_pill.set_content('<span class="pill off">PLUG OFF</span>')
                        power_html.set_content(
                            '<div class="stat-val" style="margin-top:12px">0 W</div>'
                            '<div class="stat-lbl">current draw</div>'
                        )
                    if session.active:
                        sess_pill.set_content('<span class="pill on">CHARGING</span>')
                        rem    = session.remaining_min
                        rh, rm = int(rem // 60), int(rem % 60)
                        rem_str = f"{rh}h {rm:02d}m" if rh else f"{rm}m"
                        remain_html.set_content(
                            f'<div style="margin-top:10px;font-size:12px;color:var(--muted);">'
                            f'Auto-stop in <span style="color:var(--accent2);font-family:Space Mono,monospace;">'
                            f'{rem_str}</span></div>'
                        )
                    else:
                        sess_pill.set_content('<span class="pill">NO SESSION</span>')
                        remain_html.set_content('')

                ui.button("↻ Refresh", on_click=refresh_status).classes("ghost-btn").style(
                    "margin-top:12px; font-size:12px;"
                )

            # ── Action buttons ────────────────────────────────────────────────
            # Worker thread sends dicts to gui_queue; a ui.timer on the GUI
            # thread drains the queue and acts on each message — keeping all
            # UI calls strictly on the GUI thread.
            #
            # Message keys:
            #   notify  : {"notify": "text", "type": "positive"|"negative"|...}
            #   status  : {"status": True}   – triggers a plug status refresh
            #   complete: {"complete": True} – session finished, log & notify
            #   browser_notify: {"browser_notify": "body text"}

            gui_queue: queue.Queue = queue.Queue()

            def _charge_worker(cfg: dict, delay_seconds: float) -> None:
                """Runs in a plain thread — no NiceGUI calls allowed here."""
                # Wait for scheduled start if needed
                if delay_seconds > 0:
                    if session._stop_event.wait(timeout=delay_seconds):
                        return  # cancelled during wait
                    # Turn on plug
                    import asyncio as _asyncio
                    ok = _asyncio.run(tapo_turn_on(cfg))
                    if not ok:
                        gui_queue.put({"notify": "Failed to switch on plug — check config",
                                       "type": "negative"})
                        session.active = False
                        return
                    gui_queue.put({"notify": "⚡ Charging started", "type": "positive"})

                session.started_at    = datetime.now()
                session.charge_end_at = session.started_at + timedelta(minutes=session.duration_min)
                gui_queue.put({"status": True})

                # Sleep in 1-second ticks so stop_event is checked frequently
                total_seconds = int(session.duration_min * 60)
                for _ in range(total_seconds):
                    if session._stop_event.wait(timeout=1):
                        return  # cancelled
                if not session.active:
                    return

                # Auto-stop: turn off, log, send email
                import asyncio as _asyncio
                _asyncio.run(tapo_turn_off(cfg))

                record = {
                    "date":         datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "kwh":          round(session.kwh_needed, 3),
                    "cost":         round(session.estimated_cost, 4),
                    "duration_min": round(session.duration_min, 1),
                }
                append_history(record)

                notify_cfg = load_config()
                send_email_notification(
                    notify_cfg,
                    session.kwh_needed,
                    session.estimated_cost,
                    session.duration_min,
                    )

                h2, m2     = int(session.duration_min // 60), int(session.duration_min % 60)
                dur_str    = f"{h2}h {m2:02d}m" if h2 else f"{m2}m"
                cost_str   = f" · £{session.estimated_cost:.2f}" if session.estimated_cost > 0 else ""
                notif_body = f"{session.kwh_needed:.1f} kWh in {dur_str}{cost_str}"

                session.active        = False
                session.charge_end_at = None
                gui_queue.put({"browser_notify": notif_body})
                gui_queue.put({"notify": "✅ Charging complete — plug switched off",
                                "type": "positive"})
                gui_queue.put({"status": True})

            async def _drain_queue() -> None:
                """Called by ui.timer — runs on the GUI thread, safe to call ui.*"""
                while not gui_queue.empty():
                    msg = gui_queue.get_nowait()
                    if "notify" in msg:
                        ui.notify(msg["notify"], type=msg.get("type", "info"))
                    if "status" in msg:
                        await refresh_status()
                    if "browser_notify" in msg:
                        ui.run_javascript(
                            f'sendBrowserNotification("⚡ EV Charging Complete",'
                            f' {json.dumps(msg["browser_notify"])});'
                        )

            # Drain the queue every second
            ui.timer(0.1, _drain_queue)

            with ui.row().classes("w-full gap-3"):

                async def start_charge() -> None:
                    cfg       = load_config()
                    cur       = float(inp_current.value or 0)
                    tgt       = float(inp_target.value or 0)

                    # PJA, debug for testing quick charges
                    # tgt = cur + 0.01

                    start_str = (inp_start_time.value or "").strip()

                    if tgt <= cur:
                        ui.notify("Target % must be greater than current %", type="warning")
                        return

                    session.calc_estimates(cur, tgt, cfg)
                    session._stop_event.clear()

                    delay = 0.0
                    if start_str:
                        try:
                            sh, sm = map(int, start_str.split(":"))
                            dt_time(sh, sm)
                        except Exception:
                            ui.notify("Invalid time — use HH:MM (e.g. 23:30)", type="negative")
                            return
                        now   = datetime.now()
                        start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
                        if start <= now:
                            start += timedelta(days=1)
                        delay = (start - now).total_seconds()
                        session.charge_end_at = start + timedelta(minutes=session.duration_min)

                        # PJA, debug for testing quick charges
                        # session.charge_end_at = start + timedelta(minutes=2)

                        ui.notify(f"⏳ Scheduled to start at {start_str}", type="info")
                    else:
                        ok = await tapo_turn_on(cfg)
                        if not ok:
                            ui.notify("Failed to switch on plug — check config", type="negative")
                            return
                        ui.notify("⚡ Charging started!", type="positive")

                    session.active  = True
                    session._thread = threading.Thread(
                        target=_charge_worker, args=(cfg, delay), daemon=True
                    )
                    session._thread.start()
                    await refresh_status()

                async def stop_charge() -> None:
                    cfg = load_config()
                    if session.active and session.started_at:
                        elapsed_min = (datetime.now() - session.started_at).total_seconds() / 60.0
                        rate        = cfg["charge_rate_kw"]
                        kwh_done    = (elapsed_min / 60.0) * rate
                        cheapest    = min((p["rate"] for p in cfg.get("tariff_periods", [])), default=0.0)
                        append_history({
                            "date":         datetime.now().strftime("%Y-%m-%d %H:%M"),
                            "kwh":          round(kwh_done, 3),
                            "cost":         round(kwh_done * cheapest, 4),
                            "duration_min": round(elapsed_min, 1),
                        })
                    session.cancel()
                    ok = await tapo_turn_off(cfg)
                    if ok:
                        ui.notify("🔌 Charging stopped", type="warning")
                    else:
                        ui.notify("Failed to switch off plug", type="negative")
                    await refresh_status()

                ui.button("⚡ Start Charging", on_click=start_charge).classes("accent-btn flex-1").style(
                    "padding:14px; font-size:13px;"
                )
                ui.button("⏹ Stop", on_click=stop_charge).classes("danger-btn").style(
                    "padding:14px; font-size:13px; min-width:90px;"
                )

            ui.timer(30, refresh_status)

        # ══════════════════════════════════════════════════════════════════════
        # HISTORY TAB
        # ══════════════════════════════════════════════════════════════════════
        with ui.tab_panel(tab_history):

            history = load_history()
            now     = datetime.now()

            # ── Monthly totals ────────────────────────────────────────────────
            with ui.element("div").classes("card"):
                ui.html('<div class="section-title">This Month</div>')
                month          = now.strftime("%Y-%m")
                month_sessions = [r for r in history if r["date"].startswith(month)]
                month_kwh      = sum(r["kwh"]  for r in month_sessions)
                month_cost     = sum(r["cost"] for r in month_sessions)
                with ui.row().style("gap:32px;"):
                    ui.html(f'<div class="stat-val">{month_kwh:.1f}</div><div class="stat-lbl">kWh this month</div>')
                    ui.html(f'<div class="stat-val">£{month_cost:.2f}</div><div class="stat-lbl">cost this month</div>')
                    ui.html(f'<div class="stat-val">{len(month_sessions)}</div><div class="stat-lbl">sessions</div>')

            # ── 14-day bar chart ──────────────────────────────────────────────
            with ui.element("div").classes("card"):
                ui.html('<div class="section-title">Last 14 Days — Daily kWh</div>')
                days    = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)]
                day_kwh = {d: 0.0 for d in days}
                for r in history:
                    d = r["date"][:10]
                    if d in day_kwh:
                        day_kwh[d] += r["kwh"]

                labels  = [d[5:] for d in days]
                values  = [round(day_kwh[d], 2) for d in days]
                max_val = max(values) if any(v > 0 for v in values) else 1.0

                W, H, PL, PB = 560, 160, 40, 24
                bar_w   = (W - PL - 10) / len(days)
                chart_h = H - PB - 10
                bars    = ""
                for i, (lbl, val) in enumerate(zip(labels, values)):
                    bh  = int((val / max_val) * chart_h)
                    x   = PL + i * bar_w + bar_w * 0.1
                    y   = H - PB - bh
                    bw  = bar_w * 0.8
                    col = "var(--accent)" if val > 0 else "var(--surface2)"
                    bars += f'<rect x="{x:.1f}" y="{y}" width="{bw:.1f}" height="{bh}" fill="{col}" rx="2"/>'
                    if i % 2 == 0:
                        bars += (
                            f'<text x="{x + bw/2:.1f}" y="{H - 4}" '
                            f'text-anchor="middle" font-size="9" fill="var(--muted)">{lbl}</text>'
                        )
                svg = (
                    f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
                    f'style="width:100%;max-width:{W}px;display:block;">'
                    f'<line x1="{PL}" y1="10" x2="{PL}" y2="{H-PB}" stroke="var(--border)" stroke-width="1"/>'
                    f'<line x1="{PL}" y1="{H-PB}" x2="{W-5}" y2="{H-PB}" stroke="var(--border)" stroke-width="1"/>'
                    f'<text x="4" y="14" font-size="9" fill="var(--muted)">{max_val:.1f} kWh</text>'
                    f'{bars}</svg>'
                )
                ui.html(svg)

            # ── Session log ───────────────────────────────────────────────────
            with ui.element("div").classes("card"):
                ui.html('<div class="section-title">Session Log</div>')

                if not history:
                    ui.html('<div style="color:var(--muted);font-size:13px;">No sessions recorded yet.</div>')
                else:
                    with ui.element("div").classes("history-row").style(
                        "background:transparent;border-color:transparent;padding-bottom:2px;"
                    ):
                        ui.html('<span class="history-date" style="color:var(--muted);font-size:10px;">DATE</span>')
                        ui.html('<span class="history-val"  style="color:var(--muted);font-size:10px;">kWh</span>')
                        ui.html('<span class="history-val"  style="color:var(--muted);font-size:10px;">COST</span>')
                        ui.html('<span class="history-val"  style="color:var(--muted);font-size:10px;">DURATION</span>')

                    for r in reversed(history):
                        rh  = int(r["duration_min"] // 60)
                        rm  = int(r["duration_min"] % 60)
                        dur = f"{rh}h {rm:02d}m" if rh else f"{rm}m"
                        cs  = f'£{r["cost"]:.2f}' if r["cost"] > 0 else "—"
                        with ui.element("div").classes("history-row"):
                            ui.html(f'<span class="history-date">{r["date"]}</span>')
                            ui.html(f'<span class="history-val">{r["kwh"]:.2f}</span>')
                            ui.html(f'<span class="history-val">{cs}</span>')
                            ui.html(f'<span class="history-val">{dur}</span>')

                def clear_history() -> None:
                    HISTORY_FILE.write_text("[]")
                    ui.notify("History cleared", type="warning")

                ui.button("🗑 Clear History", on_click=clear_history).classes("ghost-btn").style(
                    "margin-top:12px; font-size:12px;"
                )

        # ══════════════════════════════════════════════════════════════════════
        # CONFIG TAB
        # ══════════════════════════════════════════════════════════════════════
        with ui.tab_panel(tab_config):

            cfg = load_config()

            # ── Tapo credentials ──────────────────────────────────────────────
            with ui.element("div").classes("card"):
                ui.html('<div class="section-title">Tapo P110 Credentials</div>')
                inp_ip    = ui.input("Plug IP Address", value=cfg["tapo_ip"],
                                     placeholder="192.168.1.100").classes("w-full")
                inp_email = ui.input("Tapo Username",   value=cfg["tapo_email"],
                                     placeholder="you@example.com").classes("w-full").style("margin-top:8px")
                inp_pw    = ui.input("Tapo Password",   value=cfg["tapo_password"],
                                     password=True, password_toggle_button=True
                                     ).classes("w-full").style("margin-top:8px")
                ui.html(
                    '<div style="margin-top:6px;font-size:12px;color:var(--muted);">'
                    'Use your TP-Link cloud account email and password. '
                    'Requires Third Party Compatibility enabled in Tapo app → Me → Third Party Services.'
                    '</div>'
                )

                async def test_conn() -> None:
                    test_cfg = {"tapo_ip": inp_ip.value, "tapo_email": inp_email.value, "tapo_password": inp_pw.value}
                    pwr = await tapo_get_power(test_cfg)
                    if pwr is not None:
                        ui.notify(f"✅ Connected — current draw: {pwr:.0f} W", type="positive")
                    else:
                        ui.notify("❌ Connection failed — check IP and credentials", type="negative")

                ui.button("Test Connection", on_click=test_conn).classes("ghost-btn").style(
                    "margin-top:10px; font-size:12px;"
                )

            # ── EV parameters ─────────────────────────────────────────────────
            with ui.element("div").classes("card"):
                ui.html('<div class="section-title">EV Parameters</div>')
                with ui.row().classes("w-full gap-4"):
                    inp_batt = ui.number("Battery size (kWh)", value=cfg["battery_size_kwh"],
                                          min=1, max=200, step=0.5, format="%.1f").classes("flex-1")
                    inp_rate = ui.number("Charge rate (kW)",   value=cfg["charge_rate_kw"],
                                          min=0.1, max=22, step=0.1, format="%.1f").classes("flex-1")
                ui.html(
                    '<div style="margin-top:8px;font-size:12px;color:var(--muted);">'
                    'Tapo P110 supports up to 3.68 kW. Set charge rate to match your EVSE cable.'
                    '</div>'
                )

            # ── Electricity tariff ────────────────────────────────────────────
            with ui.element("div").classes("card"):
                ui.html('<div class="section-title">Electricity Tariff</div>')
                ui.html(
                    '<div style="font-size:12px;color:var(--muted);margin-bottom:12px;">'
                    'Add one entry at 00:00 for a flat rate, or multiple for time-of-use. '
                    'Each entry is valid from that time until the next.'
                    '</div>'
                )
                tariff_periods: list[dict] = list(cfg.get("tariff_periods", []))
                tariff_container = ui.column().classes("w-full").style("gap:0")

                def render_tariff() -> None:
                    tariff_container.clear()
                    with tariff_container:
                        for i, period in enumerate(sorted(tariff_periods, key=lambda p: p["time"])):
                            with ui.element("div").classes("tariff-row"):
                                ui.html(f'<span class="tariff-time">{period["time"]}</span>')
                                ui.html(f'<span class="tariff-rate">£{period["rate"]:.4f} / kWh</span>')
                                def _make_remover(idx: int):
                                    def remove(_) -> None:
                                        sp = sorted(tariff_periods, key=lambda p: p["time"])
                                        tariff_periods.clear()
                                        tariff_periods.extend(sp)
                                        tariff_periods.pop(idx)
                                        render_tariff()
                                    return remove
                                ui.button(icon="delete", on_click=_make_remover(i)
                                          ).props("flat dense").style("color:var(--warn);")

                render_tariff()

                with ui.element("div").style(
                    "margin-top:10px;display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;"
                ):
                    new_time_inp = ui.input("Time (HH:MM)", placeholder="23:30").style("width:130px")
                    new_rate_inp = ui.number("Rate (£/kWh)", value=0.25, min=0, max=5,
                                             step=0.001, format="%.4f").style("width:150px")

                    def add_period() -> None:
                        t = (new_time_inp.value or "").strip()
                        try:
                            h2, m2 = map(int, t.split(":"))
                            dt_time(h2, m2)
                            formatted = f"{h2:02d}:{m2:02d}"
                        except Exception:
                            ui.notify("Invalid time — use HH:MM", type="warning")
                            return
                        if any(p["time"] == formatted for p in tariff_periods):
                            ui.notify(f"Entry for {formatted} already exists", type="warning")
                            return
                        tariff_periods.append({"time": formatted, "rate": float(new_rate_inp.value or 0)})
                        new_time_inp.set_value("")
                        render_tariff()

                    ui.button("+ ADD", on_click=add_period).classes("accent-btn").style(
                        "padding:8px 16px;font-size:12px;"
                    )

            # ── Email notifications ───────────────────────────────────────────
            with ui.element("div").classes("card"):
                ui.html('<div class="section-title">Email Notifications</div>')
                chk_email = ui.checkbox("Send email when charging completes",
                                         value=cfg["notify_email_enabled"])

                with ui.column().classes("w-full").style("gap:8px;margin-top:8px;") as email_fields:
                    inp_email_to  = ui.input("Send to (email address)", value=cfg["notify_email_to"],
                                              placeholder="you@example.com").classes("w-full")
                    inp_smtp_host = ui.input("SMTP host", value=cfg["smtp_host"],
                                              placeholder="smtp.gmail.com").classes("w-full")
                    with ui.row().classes("w-full gap-4"):
                        inp_smtp_port = ui.number("SMTP port", value=cfg["smtp_port"],
                                                   min=1, max=65535, step=1, format="%.0f").classes("flex-1")
                        inp_smtp_user = ui.input("SMTP username", value=cfg["smtp_user"],
                                                  placeholder="your@gmail.com").classes("flex-1")
                    inp_smtp_pw = ui.input("SMTP password / app password", value=cfg["smtp_password"],
                                            password=True, password_toggle_button=True).classes("w-full")
                    ui.html(
                        '<div style="font-size:12px;color:var(--muted);">'
                        'For Gmail use an App Password (Google Account → Security → App passwords). '
                        'Port 587 with STARTTLS is used automatically.'
                        '</div>'
                    )

                    async def test_email() -> None:
                        test_cfg = {
                            "notify_email_enabled": True,
                            "notify_email_to":  inp_email_to.value,
                            "smtp_host":        inp_smtp_host.value,
                            "smtp_port":        int(inp_smtp_port.value or 587),
                            "smtp_user":        inp_smtp_user.value,
                            "smtp_password":    inp_smtp_pw.value,
                        }
                        ok = await asyncio.get_event_loop().run_in_executor(
                            None, send_email_notification, test_cfg, 10.5, 1.75, 191.0
                        )
                        if ok:
                            ui.notify("✅ Test email sent", type="positive")
                        else:
                            ui.notify("❌ Failed — check SMTP settings", type="negative")

                    ui.button("Send test email", on_click=test_email).classes("ghost-btn").style(
                        "font-size:12px;"
                    )

                def toggle_email_fields() -> None:
                    email_fields.set_visibility(chk_email.value)

                chk_email.on("update:model-value", lambda _: toggle_email_fields())
                toggle_email_fields()

            # ── Save ──────────────────────────────────────────────────────────
            def save_all() -> None:
                save_config({
                    "tapo_ip":              inp_ip.value or "",
                    "tapo_email":           inp_email.value or "",
                    "tapo_password":        inp_pw.value or "",
                    "battery_size_kwh":     float(inp_batt.value or 60),
                    "charge_rate_kw":       float(inp_rate.value or 3.3),
                    "tariff_periods":       list(tariff_periods),
                    "notify_email_enabled": chk_email.value,
                    "notify_email_to":      inp_email_to.value or "",
                    "smtp_host":            inp_smtp_host.value or "smtp.gmail.com",
                    "smtp_port":            int(inp_smtp_port.value or 587),
                    "smtp_user":            inp_smtp_user.value or "",
                    "smtp_password":        inp_smtp_pw.value or "",
                })
                ui.notify("✅ Configuration saved", type="positive")

            ui.button("💾 Save Configuration", on_click=save_all).classes("accent-btn w-full").style(
                "padding:14px;font-size:14px;margin-top:4px;"
            )


# ── Entry point ───────────────────────────────────────────────────────────────

def gui_main(show: bool, port: int) -> None:
    ui.run(
        title=f"Tapo EV Charger v{__version__}",
        host="0.0.0.0",
        port=port,
        favicon="⚡",
        dark=True,
        reload=False,
        show=show
    )

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """@brief Program entry point"""
    uio = UIO()

    app_cfg_path = get_app_cfg_path()
    uio.info(f"Config path: {app_cfg_path}")

    options = None
    try:
        parser = argparse.ArgumentParser(description="An app to allow a Tapo P110 smart plug (connected to 13A mains EV charger) to charge your EV.",
                                         formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument("-d", "--debug",  action='store_true', help="Enable debugging.")
        parser.add_argument("-p", "--port",    type=int, help="The TCP port to start the nicegui server on (default=8080).", default=8080)
        parser.add_argument("-n", "--no_web_launch",  action='store_true', help="Do not open web browser.")
        BootManager.AddCmdArgs(parser)
        options = parser.parse_args()

        uio.enableDebug(options.debug)
        uio.logAll(True)
        uio.enableSyslog(True, programName="tapo_p110_ev_charger")

        uio.info(f"tapo_p110_ev_charger: v{__version__}")

        handled = BootManager.HandleOptions(uio, options, True)
        if not handled:
            gui_main(not options.no_web_launch, options.port)

    # If the program throws a system exit exception
    except SystemExit:
        pass
    # Don't print error information if CTRL C pressed
    except KeyboardInterrupt:
        pass
    except Exception as ex:
        logTraceBack(uio)

        if options and options.debug:
            raise
        else:
            uio.error(str(ex))

if __name__ in {"__main__", "__mp_main__"}:
    main()
