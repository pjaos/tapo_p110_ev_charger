# Tapo EV Charger Controller

A NiceGUI **3.9** web app for controlling a TP-Link Tapo P110 smart plug connected to an EV charger. Designed to run on a Raspberry Pi and be accessed from a phone browser. The app should run on any Linux, MAC or Windows machine that has the correct version of python installed.

## Features

- **Charge tab** — Enter current and target battery %, optional scheduled start time, live power draw, live countdown to auto-stop, and instant kWh/duration/cost estimates.
- **Auto-stop** — Automatically switches the plug off when the calculated charge duration elapses (based on elapsed time × charge rate). Logs the session on completion.
- **History tab** — Per-session log (date, kWh, cost, duration), 14-day daily kWh bar chart, and monthly kWh/cost/session totals.
- **Notifications** — Browser Web Push notification and optional SMTP email notification when charging completes.
- **Config tab** — Tapo P110 credentials, EV parameters (battery size, charge rate), flexible electricity tariff editor (flat-rate or time-of-use), and email notification settings.

## Requirements

- Python 3.11.2+

## Who is this for?

This tool is only useful if you have **all three** of the following:

- An electric vehicle (EV)
- A **Tapo P110 Smart Plug**
- A **13A Mains AC EV charger** connected to the above smart plug

---

### Tapo app prerequisite

Before the app can connect to your P110, you must enable **Third Party Compatibility** in the Tapo app:

> Tapo app → **Me** (bottom-right) → **Third Party Services** → **Third-Party Compatibility** → toggle **ON**

This is required for the `tapo` Python library to authenticate with the plug.

## Installation

The python wheel installer file can be found in the linux folder.

### Using the bundled installer

```bash
python3 install.py linux/tapo_p110_ev_charger-<version>-py3-none-any.whl
```

This creates a virtual environment, installs all dependencies, and adds a `tapo_p110_ev_charger` launcher to your PATH.

### Manual installation with pip

```bash
pip install linux/tapo_p110_ev_charger-<version>-py3-none-any.whl
```

## Running

```bash
tapo_p110_ev_charger
```

This starts the nicegui server and opens a web browser connected to it.

## Configuration

Config is saved to `tapo_ev_config.json` in the config folder. On a Linux machine this
will be the ~/.config/tapo_p110_ev_charger folder. On other platforms the ~/.tapo_p110_ev_charger folder will be used.

### Tapo P110 Credentials

| Field | Description |
|---|---|
| Plug IP Address | Local IP of your P110 — find it in the Tapo app under the device's settings |
| Tapo Username | Your TP-Link cloud account email |
| Tapo Password | Your TP-Link cloud account password |

Use the **Test Connection** button to verify credentials before starting a charge session.

### EV Parameters

| Field | Description |
|---|---|
| Battery size (kWh) | Your EV's usable battery capacity |
| Charge rate (kW) | Your EVSE cable/charger rate (P110 hardware max ≈ 3.0 kW) |

### Electricity Tariff

Add one entry at `00:00` for a flat rate, or multiple time-stamped entries for a time-of-use tariff. Each entry sets the rate from that time until the next entry. Used to estimate session cost — the cheapest applicable rate is used.

### Email Notifications

Requires an SMTP server. For Gmail, generate an **App Password** (Google Account → Security → 2-Step Verification → App passwords) rather than using your main account password. Port 587 with STARTTLS is used automatically.

Use the **Send test email** button to verify settings.

## Charge Tab

1. Enter **Current %** and **Target %** — kWh needed, estimated duration, and estimated cost update live.
2. Optionally enter a **Start time** (`HH:MM`) to schedule off-peak charging. If left blank, charging starts immediately.
3. Press **⚡ Start Charging** — the plug turns on (or waits until the scheduled time) and a background timer counts down to auto-stop.
4. The status card shows live power draw and a countdown to auto-stop, refreshed every 30 seconds.
5. Press **⏹ Stop** at any time to cut power immediately. A partial session is logged automatically.

When the timer elapses, the plug is switched off, the session is logged, a browser notification is sent, and an email is sent if configured.

## History Tab

- **This Month** — total kWh delivered, total estimated cost, and number of sessions for the current calendar month.
- **Last 14 Days** — bar chart of daily kWh.
- **Session Log** — all completed and manually stopped sessions in reverse chronological order.

Session history is saved to `tapo_ev_history.json` in the working directory. Use the **Clear History** button to reset it.


### Command-line options

| Flag | Description |
|---|---|
| `-h / --help` | Show this help message and exit |
| `-p PORT / --port PORT` | The TCP port to start the nicegui server on (default=8080) |
| `-n / --no_web_launch` | Do not open web browser. By default a local web browser session is started |
| `-d / --debug` | Enable verbose debug logging |
| `--enable_auto_start` | Register the tool to start on system boot |
| `--disable_auto_start` | Un-register the tool to start on system boot |
| `--check_auto_start` | Check the running status |

### Running as a service

Use the built-in boot manager to have the tool start automatically (Linux only):

```bash
tapo_p110_ev_charger -n --enable_auto_start
```

## Author

Paul Austen — [pjaos@gmail.com](mailto:pjaos@gmail.com)


## Acknowledgements

Development of this project was assisted by [Claude](https://claude.ai) (Anthropic's AI assistant),
which contributed to code review, bug identification, test generation, and this documentation.