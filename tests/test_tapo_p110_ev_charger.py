"""
Test suite for the Tapo EV Charger Controller.

Covers:
  - Config load / save / back-fill of missing keys
  - History load / append
  - ChargeSession.calc_estimates
  - ChargeSession.remaining_min
  - ChargeSession.cancel
  - send_email_notification (disabled, SMTP error, success, body content)
  - tapo_get_power / tapo_turn_on / tapo_turn_off (mocked tapo library)

Run with:
    pytest test_tapo_ev_charger.py -v
"""

import asyncio
import email as email_lib
import json
import smtplib
from datetime import datetime, timedelta
from email.header import decode_header as _decode_header
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tapo_p110_ev_charger import tapo_p110_ev_charger as app

# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _decode_mime(raw: str) -> tuple[str, str, str]:
    """
    Parse a raw MIME string as produced by MIMEText.as_string().
    Returns (from_addr, to_addr, decoded_body).

    When the subject contains non-ASCII characters (the ⚡ emoji) Python
    base64-encodes the entire message body.  get_payload(decode=True)
    reverses that regardless of Content-Transfer-Encoding.
    """
    msg  = email_lib.message_from_string(raw)
    body = msg.get_payload(decode=True).decode("utf-8")
    return msg["From"] or "", msg["To"] or "", body


def _decode_subject(raw: str) -> str:
    """Decode an RFC-2047 encoded subject header (e.g. =?utf-8?q?...?=)."""
    msg = email_lib.message_from_string(raw)
    return "".join(
        part.decode(enc or "utf-8") if isinstance(part, bytes) else part
        for part, enc in _decode_header(msg["Subject"] or "")
    )


def _make_smtp_mock():
    """Return (mock_smtp_ctx, mock_server) ready for use as smtplib.SMTP."""
    mock_server   = MagicMock()
    mock_smtp_ctx = MagicMock()
    mock_smtp_ctx.__enter__ = MagicMock(return_value=mock_server)
    mock_smtp_ctx.__exit__  = MagicMock(return_value=False)
    return mock_smtp_ctx, mock_server


def _capture_email(email_cfg, kwh, cost, duration_min):
    """
    Call send_email_notification with a mocked SMTP server and return
    (from_addr, to_addr, decoded_subject, decoded_body).
    """
    captured = {}
    mock_smtp_ctx, mock_server = _make_smtp_mock()
    mock_server.sendmail.side_effect = (
        lambda f, t, m: captured.update({"from": f, "to": t, "raw": m})
    )
    with patch("smtplib.SMTP", return_value=mock_smtp_ctx):
        app.send_email_notification(email_cfg, kwh, cost, duration_min)
    from_addr, to_addr, body = _decode_mime(captured["raw"])
    subject = _decode_subject(captured["raw"])
    return from_addr, to_addr, subject, body


def _make_tapo_mock(current_power: float = 1500.0):
    """Build a mock tapo module hierarchy returning the given power value."""
    power_obj = MagicMock()
    power_obj.to_dict.return_value = {"current_power": current_power}

    device = AsyncMock()
    device.get_current_power = AsyncMock(return_value=power_obj)
    device.on  = AsyncMock()
    device.off = AsyncMock()

    client = AsyncMock()
    client.p110 = AsyncMock(return_value=device)

    tapo_mod = MagicMock()
    tapo_mod.ApiClient = MagicMock(return_value=client)

    return tapo_mod, client, device


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    """Redirect CONFIG_FILE and HISTORY_FILE to a temp directory for every test."""
    monkeypatch.setattr(app, "CONFIG_FILE",  tmp_path / "config.json")
    monkeypatch.setattr(app, "HISTORY_FILE", tmp_path / "history.json")


@pytest.fixture
def base_cfg() -> dict:
    return {
        "tapo_ip":              "192.168.1.160",
        "tapo_email":           "user@example.com",
        "tapo_password":        "secret",
        "battery_size_kwh":     60.0,
        "charge_rate_kw":       3.3,
        "tariff_periods":       [{"time": "00:00", "rate": 0.25}],
        "notify_email_enabled": False,
        "notify_email_to":      "",
        "smtp_host":            "smtp.gmail.com",
        "smtp_port":            587,
        "smtp_user":            "",
        "smtp_password":        "",
    }


@pytest.fixture
def tou_cfg(base_cfg) -> dict:
    cfg = dict(base_cfg)
    cfg["tariff_periods"] = [
        {"time": "00:00", "rate": 0.07},
        {"time": "05:30", "rate": 0.2672},
    ]
    return cfg


@pytest.fixture
def email_cfg(base_cfg) -> dict:
    cfg = dict(base_cfg)
    cfg.update({
        "notify_email_enabled": True,
        "notify_email_to":      "dest@example.com",
        "smtp_user":            "sender@gmail.com",
        "smtp_password":        "app-password",
        "smtp_host":            "smtp.gmail.com",
        "smtp_port":            587,
    })
    return cfg


@pytest.fixture
def fresh_session() -> app.ChargeSession:
    return app.ChargeSession()


# ═══════════════════════════════════════════════════════════════════════════════
# Config persistence
# ═══════════════════════════════════════════════════════════════════════════════

class TestLoadConfig:
    def test_returns_defaults_when_file_missing(self):
        assert app.load_config() == app.DEFAULT_CONFIG

    def test_loads_saved_config(self, base_cfg):
        app.save_config(base_cfg)
        loaded = app.load_config()
        assert loaded["tapo_ip"]          == "192.168.1.160"
        assert loaded["battery_size_kwh"] == 60.0
        assert loaded["charge_rate_kw"]   == 3.3

    def test_back_fills_missing_keys(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "CONFIG_FILE", tmp_path / "config.json")
        partial = {
            "tapo_ip": "1.2.3.4", "battery_size_kwh": 40.0, "charge_rate_kw": 7.4,
            "tapo_email": "", "tapo_password": "", "tariff_periods": [],
        }
        (tmp_path / "config.json").write_text(json.dumps(partial))
        cfg = app.load_config()
        assert cfg["notify_email_enabled"] == False
        assert cfg["smtp_host"]            == "smtp.gmail.com"
        assert cfg["smtp_port"]            == 587

    def test_returns_defaults_on_corrupt_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "CONFIG_FILE", tmp_path / "config.json")
        (tmp_path / "config.json").write_text("{ not valid json }")
        assert app.load_config() == app.DEFAULT_CONFIG

    def test_preserves_tariff_periods(self, base_cfg):
        base_cfg["tariff_periods"] = [
            {"time": "00:00", "rate": 0.07},
            {"time": "05:30", "rate": 0.2672},
        ]
        app.save_config(base_cfg)
        loaded = app.load_config()
        assert len(loaded["tariff_periods"]) == 2
        assert loaded["tariff_periods"][0]["rate"] == 0.07


class TestSaveConfig:
    def test_writes_json_file(self, base_cfg):
        app.save_config(base_cfg)
        assert app.CONFIG_FILE.exists()
        on_disk = json.loads(app.CONFIG_FILE.read_text())
        assert on_disk["tapo_ip"] == "192.168.1.160"

    def test_roundtrip(self, base_cfg):
        app.save_config(base_cfg)
        assert app.load_config()["charge_rate_kw"] == pytest.approx(3.3)


# ═══════════════════════════════════════════════════════════════════════════════
# History persistence
# ═══════════════════════════════════════════════════════════════════════════════

class TestLoadHistory:
    def test_returns_empty_list_when_file_missing(self):
        assert app.load_history() == []

    def test_loads_existing_history(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "HISTORY_FILE", tmp_path / "history.json")
        records = [{"date": "2025-01-01 22:00", "kwh": 10.0, "cost": 2.5, "duration_min": 182.0}]
        (tmp_path / "history.json").write_text(json.dumps(records))
        loaded = app.load_history()
        assert len(loaded) == 1
        assert loaded[0]["kwh"] == 10.0

    def test_returns_empty_list_on_corrupt_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "HISTORY_FILE", tmp_path / "history.json")
        (tmp_path / "history.json").write_text("corrupted")
        assert app.load_history() == []


class TestAppendHistory:
    def test_appends_to_empty_file(self):
        record = {"date": "2025-06-01 23:00", "kwh": 15.3, "cost": 3.82, "duration_min": 278.0}
        app.append_history(record)
        history = app.load_history()
        assert len(history) == 1
        assert history[0]["kwh"] == 15.3

    def test_appends_multiple_records(self):
        for i in range(3):
            app.append_history({"date": f"2025-06-0{i+1} 22:00", "kwh": float(i),
                                 "cost": 0.0, "duration_min": 60.0})
        assert len(app.load_history()) == 3

    def test_preserves_existing_records(self):
        app.append_history({"date": "2025-01-01 00:00", "kwh": 5.0, "cost": 1.25, "duration_min": 90.0})
        app.append_history({"date": "2025-01-02 00:00", "kwh": 8.0, "cost": 2.00, "duration_min": 145.0})
        history = app.load_history()
        assert history[0]["kwh"] == 5.0
        assert history[1]["kwh"] == 8.0


# ═══════════════════════════════════════════════════════════════════════════════
# ChargeSession
# ═══════════════════════════════════════════════════════════════════════════════

class TestChargeSessionCalcEstimates:
    def test_basic_calculation(self, fresh_session, base_cfg):
        fresh_session.calc_estimates(20.0, 80.0, base_cfg)
        assert fresh_session.kwh_needed == pytest.approx(36.0)

    def test_duration_minutes(self, fresh_session, base_cfg):
        fresh_session.calc_estimates(20.0, 80.0, base_cfg)
        assert fresh_session.duration_min == pytest.approx((36.0 / 3.3) * 60.0, rel=1e-4)

    def test_cost_uses_cheapest_rate(self, fresh_session, tou_cfg):
        fresh_session.calc_estimates(0.0, 100.0, tou_cfg)
        assert fresh_session.estimated_cost == pytest.approx(60.0 * 0.07)

    def test_zero_cost_when_no_tariff(self, fresh_session, base_cfg):
        base_cfg["tariff_periods"] = []
        fresh_session.calc_estimates(0.0, 50.0, base_cfg)
        assert fresh_session.estimated_cost == pytest.approx(0.0)

    def test_target_below_current_gives_zero(self, fresh_session, base_cfg):
        fresh_session.calc_estimates(80.0, 20.0, base_cfg)
        assert fresh_session.kwh_needed     == pytest.approx(0.0)
        assert fresh_session.duration_min   == pytest.approx(0.0)
        assert fresh_session.estimated_cost == pytest.approx(0.0)

    def test_target_equal_to_current_gives_zero(self, fresh_session, base_cfg):
        fresh_session.calc_estimates(50.0, 50.0, base_cfg)
        assert fresh_session.kwh_needed == pytest.approx(0.0)

    def test_full_charge_from_empty(self, fresh_session, base_cfg):
        fresh_session.calc_estimates(0.0, 100.0, base_cfg)
        assert fresh_session.kwh_needed == pytest.approx(60.0)

    def test_zero_charge_rate_gives_zero_duration(self, fresh_session, base_cfg):
        base_cfg["charge_rate_kw"] = 0.0
        fresh_session.calc_estimates(0.0, 100.0, base_cfg)
        assert fresh_session.duration_min == pytest.approx(0.0)

    def test_different_battery_size(self, fresh_session, base_cfg):
        base_cfg["battery_size_kwh"] = 40.0
        fresh_session.calc_estimates(0.0, 50.0, base_cfg)
        assert fresh_session.kwh_needed == pytest.approx(20.0)

    def test_fractional_percentages(self, fresh_session, base_cfg):
        fresh_session.calc_estimates(33.3, 66.6, base_cfg)
        assert fresh_session.kwh_needed == pytest.approx(((66.6 - 33.3) / 100.0) * 60.0, rel=1e-4)


class TestChargeSessionRemainingMin:
    def test_returns_zero_when_inactive(self, fresh_session):
        assert fresh_session.remaining_min == pytest.approx(0.0)

    def test_returns_zero_when_no_end_time(self, fresh_session):
        fresh_session.active = True
        assert fresh_session.remaining_min == pytest.approx(0.0)

    def test_returns_correct_remaining(self, fresh_session):
        fresh_session.active        = True
        fresh_session.charge_end_at = datetime.now() + timedelta(minutes=90)
        assert fresh_session.remaining_min == pytest.approx(90.0, abs=0.1)

    def test_returns_zero_when_past_end(self, fresh_session):
        fresh_session.active        = True
        fresh_session.charge_end_at = datetime.now() - timedelta(minutes=10)
        assert fresh_session.remaining_min == pytest.approx(0.0)

    def test_nearly_complete(self, fresh_session):
        fresh_session.active        = True
        fresh_session.charge_end_at = datetime.now() + timedelta(seconds=30)
        assert 0.0 <= fresh_session.remaining_min < 1.0


class TestChargeSessionCancel:
    def test_sets_active_false(self, fresh_session):
        fresh_session.active = True
        fresh_session.cancel()
        assert fresh_session.active is False

    def test_clears_timestamps(self, fresh_session):
        fresh_session.active        = True
        fresh_session.started_at    = datetime.now()
        fresh_session.charge_end_at = datetime.now() + timedelta(hours=2)
        fresh_session.cancel()
        assert fresh_session.started_at    is None
        assert fresh_session.charge_end_at is None

# PJA
#    def test_cancels_pending_task(self, fresh_session):
#        mock_task = MagicMock()
#        mock_task.done.return_value = False
#        fresh_session._task  = mock_task
#        fresh_session.active = True
#        fresh_session.cancel()
#        mock_task.cancel.assert_called_once()

    def test_does_not_cancel_completed_task(self, fresh_session):
        mock_task = MagicMock()
        mock_task.done.return_value = True
        fresh_session._task  = mock_task
        fresh_session.active = True
        fresh_session.cancel()
        mock_task.cancel.assert_not_called()

    def test_cancel_with_no_task_is_safe(self, fresh_session):
        fresh_session.active = True
        fresh_session._task  = None
        fresh_session.cancel()
        assert fresh_session.active is False


# ═══════════════════════════════════════════════════════════════════════════════
# send_email_notification
# ═══════════════════════════════════════════════════════════════════════════════

class TestSendEmailNotification:
    def test_returns_false_when_disabled(self, base_cfg):
        base_cfg["notify_email_enabled"] = False
        assert app.send_email_notification(base_cfg, 10.0, 2.50, 180.0) is False

    def test_returns_false_on_smtp_error(self, email_cfg):
        with patch("smtplib.SMTP", side_effect=smtplib.SMTPException("refused")):
            assert app.send_email_notification(email_cfg, 10.0, 2.50, 180.0) is False

    def test_returns_true_on_success(self, email_cfg):
        mock_smtp_ctx, _ = _make_smtp_mock()
        with patch("smtplib.SMTP", return_value=mock_smtp_ctx):
            assert app.send_email_notification(email_cfg, 10.0, 2.50, 180.0) is True

    def test_email_subject_and_recipients(self, email_cfg):
        from_addr, to_addr, subject, _ = _capture_email(email_cfg, 10.0, 2.50, 180.0)
        assert from_addr == "sender@gmail.com"
        assert to_addr   == "dest@example.com"
        assert "EV Charging Complete" in subject

    def test_email_body_contains_kwh_and_cost(self, email_cfg):
        _, _, _, body = _capture_email(email_cfg, 23.5, 4.93, 427.0)
        assert "23.50 kWh" in body
        assert "£4.93"     in body

    def test_duration_formatting_hours_and_minutes(self, email_cfg):
        _, _, _, body = _capture_email(email_cfg, 10.0, 2.50, 185.0)  # 3h 05m
        assert "3h 05m" in body

    def test_duration_formatting_minutes_only(self, email_cfg):
        _, _, _, body = _capture_email(email_cfg, 1.0, 0.25, 18.0)   # 18m
        assert "18m" in body
        duration_line = next(l for l in body.splitlines() if "Duration" in l)
        assert "h" not in duration_line

    def test_zero_cost_shows_unknown(self, email_cfg):
        _, _, _, body = _capture_email(email_cfg, 10.0, 0.0, 180.0)
        assert "unknown" in body

    def test_uses_starttls(self, email_cfg):
        mock_smtp_ctx, mock_server = _make_smtp_mock()
        with patch("smtplib.SMTP", return_value=mock_smtp_ctx):
            app.send_email_notification(email_cfg, 10.0, 2.50, 180.0)
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("sender@gmail.com", "app-password")


# ═══════════════════════════════════════════════════════════════════════════════
# Tapo helpers — run async functions via asyncio.run() so no plugin needed
# ═══════════════════════════════════════════════════════════════════════════════

class TestTapoGetPower:
    def test_returns_power_on_success(self, base_cfg):
        tapo_mod, _, _ = _make_tapo_mock(current_power=2750.0)
        with patch.dict("sys.modules", {"tapo": tapo_mod}):
            result = asyncio.run(app.tapo_get_power(base_cfg))
        assert result == pytest.approx(2750.0)

    def test_returns_none_on_import_error(self, base_cfg):
        with patch.dict("sys.modules", {"tapo": None}):
            result = asyncio.run(app.tapo_get_power(base_cfg))
        assert result is None

    def test_returns_none_on_connection_error(self, base_cfg):
        tapo_mod = MagicMock()
        tapo_mod.ApiClient = MagicMock(side_effect=Exception("connection refused"))
        with patch.dict("sys.modules", {"tapo": tapo_mod}):
            result = asyncio.run(app.tapo_get_power(base_cfg))
        assert result is None

    def test_uses_correct_credentials(self, base_cfg):
        tapo_mod, client, _ = _make_tapo_mock()
        with patch.dict("sys.modules", {"tapo": tapo_mod}):
            asyncio.run(app.tapo_get_power(base_cfg))
        tapo_mod.ApiClient.assert_called_once_with("user@example.com", "secret")
        client.p110.assert_called_once_with("192.168.1.160")

    def test_returns_zero_power_correctly(self, base_cfg):
        tapo_mod, _, _ = _make_tapo_mock(current_power=0.0)
        with patch.dict("sys.modules", {"tapo": tapo_mod}):
            result = asyncio.run(app.tapo_get_power(base_cfg))
        assert result == pytest.approx(0.0)


class TestTapoTurnOn:
    def test_returns_true_on_success(self, base_cfg):
        tapo_mod, _, device = _make_tapo_mock()
        with patch.dict("sys.modules", {"tapo": tapo_mod}):
            result = asyncio.run(app.tapo_turn_on(base_cfg))
        assert result is True
        device.on.assert_called_once()

    def test_returns_false_on_error(self, base_cfg):
        tapo_mod = MagicMock()
        tapo_mod.ApiClient = MagicMock(side_effect=Exception("unreachable"))
        with patch.dict("sys.modules", {"tapo": tapo_mod}):
            result = asyncio.run(app.tapo_turn_on(base_cfg))
        assert result is False

    def test_returns_false_on_import_error(self, base_cfg):
        with patch.dict("sys.modules", {"tapo": None}):
            result = asyncio.run(app.tapo_turn_on(base_cfg))
        assert result is False


class TestTapoTurnOff:
    def test_returns_true_on_success(self, base_cfg):
        tapo_mod, _, device = _make_tapo_mock()
        with patch.dict("sys.modules", {"tapo": tapo_mod}):
            result = asyncio.run(app.tapo_turn_off(base_cfg))
        assert result is True
        device.off.assert_called_once()

    def test_returns_false_on_error(self, base_cfg):
        tapo_mod = MagicMock()
        tapo_mod.ApiClient = MagicMock(side_effect=RuntimeError("timeout"))
        with patch.dict("sys.modules", {"tapo": tapo_mod}):
            result = asyncio.run(app.tapo_turn_off(base_cfg))
        assert result is False

    def test_returns_false_on_import_error(self, base_cfg):
        with patch.dict("sys.modules", {"tapo": None}):
            result = asyncio.run(app.tapo_turn_off(base_cfg))
        assert result is False


# ═══════════════════════════════════════════════════════════════════════════════
# Integration-style: calc_estimates → history round-trip
# ═══════════════════════════════════════════════════════════════════════════════

class TestSessionHistoryIntegration:
    def test_completed_session_logged_correctly(self, base_cfg):
        s = app.ChargeSession()
        s.calc_estimates(20.0, 80.0, base_cfg)
        app.append_history({
            "date":         "2025-06-15 23:00",
            "kwh":          round(s.kwh_needed, 3),
            "cost":         round(s.estimated_cost, 4),
            "duration_min": round(s.duration_min, 1),
        })
        history = app.load_history()
        assert len(history) == 1
        assert history[0]["kwh"]  == pytest.approx(36.0)
        assert history[0]["cost"] == pytest.approx(36.0 * 0.25)

    def test_monthly_totals_calculation(self):
        sessions = [
            {"date": "2025-06-01 22:00", "kwh": 10.0, "cost": 2.50, "duration_min": 181.8},
            {"date": "2025-06-08 23:30", "kwh": 18.0, "cost": 4.50, "duration_min": 327.3},
            {"date": "2025-05-31 01:00", "kwh": 25.0, "cost": 6.25, "duration_min": 454.5},  # prior month
        ]
        for r in sessions:
            app.append_history(r)

        history      = app.load_history()
        june_records = [r for r in history if r["date"].startswith("2025-06")]
        assert sum(r["kwh"]  for r in june_records) == pytest.approx(28.0)
        assert sum(r["cost"] for r in june_records) == pytest.approx(7.00)
        assert len(june_records) == 2
