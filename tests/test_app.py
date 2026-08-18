"""Tests for app.py — the Flask dashboard (route/state validation, delete
endpoint, and cron schedule logic). Uses Flask's test client; no live server,
no live crontab (subprocess.run is mocked wherever it would touch the real
crontab)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock
from urllib.parse import quote

import pytest

# Ensure the project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as webapp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    webapp.app.testing = True
    return webapp.app.test_client()


@pytest.fixture()
def state_file(tmp_path):
    path = tmp_path / "state.json"
    with mock.patch.object(webapp, "STATE_FILE", path):
        yield path


@pytest.fixture()
def routes_file(tmp_path):
    path = tmp_path / "routes.json"
    with mock.patch.object(webapp, "ROUTES_FILE", path):
        yield path


def write_state(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# validate_routes / normalize_route
# ---------------------------------------------------------------------------

class TestValidateRoutes:
    def test_valid_minimal_route(self):
        routes = [{"origin": "yvr", "destination": "fra", "departure_date": "2027-03-15"}]
        assert webapp.validate_routes(routes) == []

    def test_not_a_list(self):
        assert webapp.validate_routes({"origin": "YVR"}) != []

    def test_empty_list(self):
        assert webapp.validate_routes([]) != []

    def test_route_not_a_dict(self):
        errors = webapp.validate_routes(["not a dict"])
        assert any("must be an object" in e for e in errors)

    def test_missing_required_field(self):
        errors = webapp.validate_routes([{"origin": "YVR", "destination": "FRA"}])
        assert any("departure_date" in e for e in errors)

    def test_bad_iata_code(self):
        errors = webapp.validate_routes(
            [{"origin": "YV", "destination": "FRA", "departure_date": "2027-03-15"}]
        )
        assert any("origin must be a 3-letter IATA code" in e for e in errors)

    def test_bad_date_format(self):
        errors = webapp.validate_routes(
            [{"origin": "YVR", "destination": "FRA", "departure_date": "03/15/2027"}]
        )
        assert any("departure_date must be an ISO date" in e for e in errors)

    def test_bad_return_date_format(self):
        errors = webapp.validate_routes([{
            "origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15",
            "return_date": "not-a-date",
        }])
        assert any("return_date must be an ISO date" in e for e in errors)

    def test_negative_adults(self):
        errors = webapp.validate_routes([{
            "origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15", "adults": 0,
        }])
        assert any("adults must be a positive integer" in e for e in errors)

    def test_non_numeric_adults(self):
        errors = webapp.validate_routes([{
            "origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15", "adults": "many",
        }])
        assert any("adults must be a positive integer" in e for e in errors)

    def test_bad_travel_class(self):
        errors = webapp.validate_routes([{
            "origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15",
            "travel_class": "COACH",
        }])
        assert any("travel_class must be one of" in e for e in errors)

    def test_bad_run_hours(self):
        errors = webapp.validate_routes([{
            "origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15",
            "run_hours": [7, 25],
        }])
        assert any("run_hours must be a list of hours" in e for e in errors)

    def test_error_on_second_route_is_indexed_1_based(self):
        errors = webapp.validate_routes([
            {"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15"},
            {"origin": "YV", "destination": "FRA", "departure_date": "2027-03-15"},
        ])
        assert errors and all(e.startswith("Route 2") for e in errors)


class TestNormalizeRoute:
    def test_uppercases_iata_codes(self):
        out = webapp.normalize_route({"origin": "yvr", "destination": "fra", "departure_date": "2027-03-15"})
        assert out["origin"] == "YVR"
        assert out["destination"] == "FRA"

    def test_omits_absent_optional_fields(self):
        out = webapp.normalize_route({"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15"})
        assert set(out) == {"origin", "destination", "departure_date"}

    def test_includes_return_date_when_present(self):
        out = webapp.normalize_route({
            "origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15",
            "return_date": "2027-03-27",
        })
        assert out["return_date"] == "2027-03-27"

    def test_run_hours_sorted_and_deduped(self):
        out = webapp.normalize_route({
            "origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15",
            "run_hours": [19, 7, 7, 13],
        })
        assert out["run_hours"] == [7, 13, 19]

    def test_adults_coerced_to_int(self):
        out = webapp.normalize_route({
            "origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15", "adults": "2",
        })
        assert out["adults"] == 2


# ---------------------------------------------------------------------------
# /api/routes
# ---------------------------------------------------------------------------

class TestApiRoutes:
    def test_get_missing_file_returns_empty_list(self, client, routes_file):
        resp = client.get("/api/routes")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_get_returns_saved_routes(self, client, routes_file):
        routes = [{"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15"}]
        routes_file.write_text(json.dumps(routes), encoding="utf-8")
        resp = client.get("/api/routes")
        assert resp.get_json() == routes

    def test_post_rejects_non_list(self, client, routes_file):
        resp = client.post("/api/routes", json={"origin": "YVR"})
        assert resp.status_code == 400
        assert "errors" in resp.get_json()

    def test_post_rejects_invalid_route(self, client, routes_file):
        resp = client.post("/api/routes", json=[{"origin": "YV"}])
        assert resp.status_code == 400

    def test_post_saves_normalized_routes(self, client, routes_file):
        payload = [{"origin": "yvr", "destination": "fra", "departure_date": "2027-03-15"}]
        resp = client.post("/api/routes", json=payload)
        assert resp.status_code == 200
        saved = json.loads(routes_file.read_text())
        assert saved == [{"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15"}]


# ---------------------------------------------------------------------------
# DELETE /api/routes/<label> — remove a route's price history/chart data
# ---------------------------------------------------------------------------

class TestApiRouteDataDelete:
    def _seed(self, path):
        write_state(path, {
            "prices": {
                "YVR-FRA 2027-03-15": {"price": 2135.0, "history": [{"price": 2135.0, "timestamp": "t"}]},
                "YVR-CUN 2026-12-23": {"price": 400.0, "history": [{"price": 400.0, "timestamp": "t"}]},
            },
            "flex_scans": {
                "YVR-FRA 2027-03-15": {"base_date": "2027-03-15", "days": 3, "results": [], "cheapest": None},
            },
            "api_calls": {"2026-08": 10},
            "last_run": "2026-08-17T19:30:00-07:00",
        })

    def test_delete_removes_prices_and_flex_scan(self, client, state_file):
        self._seed(state_file)
        resp = client.delete("/api/routes/" + quote("YVR-FRA 2027-03-15"))
        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}

        saved = json.loads(state_file.read_text())
        assert "YVR-FRA 2027-03-15" not in saved["prices"]
        assert "YVR-FRA 2027-03-15" not in saved["flex_scans"]

    def test_delete_leaves_other_routes_untouched(self, client, state_file):
        self._seed(state_file)
        client.delete("/api/routes/" + quote("YVR-FRA 2027-03-15"))
        saved = json.loads(state_file.read_text())
        assert saved["prices"]["YVR-CUN 2026-12-23"]["price"] == 400.0
        assert saved["api_calls"] == {"2026-08": 10}
        assert saved["last_run"] == "2026-08-17T19:30:00-07:00"

    def test_delete_unknown_label_returns_404(self, client, state_file):
        self._seed(state_file)
        resp = client.delete("/api/routes/" + quote("XXX-YYY 2099-01-01"))
        assert resp.status_code == 404
        assert "errors" in resp.get_json()

    def test_delete_twice_second_call_404s(self, client, state_file):
        self._seed(state_file)
        label = quote("YVR-FRA 2027-03-15")
        assert client.delete("/api/routes/" + label).status_code == 200
        assert client.delete("/api/routes/" + label).status_code == 404

    def test_delete_label_present_only_in_flex_scans(self, client, state_file):
        # A route can have a flex scan without (or no longer with) price history.
        write_state(state_file, {
            "prices": {},
            "flex_scans": {"YVR-FRA 2027-03-15": {"base_date": "2027-03-15", "days": 3, "results": []}},
        })
        resp = client.delete("/api/routes/" + quote("YVR-FRA 2027-03-15"))
        assert resp.status_code == 200
        saved = json.loads(state_file.read_text())
        assert saved["flex_scans"] == {}


# ---------------------------------------------------------------------------
# /api/state and dashboard rendering
# ---------------------------------------------------------------------------

class TestApiState:
    def test_missing_state_file_returns_empty_object(self, client, state_file):
        resp = client.get("/api/state")
        assert resp.get_json() == {}

    def test_returns_state_contents(self, client, state_file):
        write_state(state_file, {"prices": {}, "last_run": "2026-08-17T19:30:00-07:00"})
        resp = client.get("/api/state")
        assert resp.get_json()["last_run"] == "2026-08-17T19:30:00-07:00"


class TestDashboard:
    def test_empty_state_shows_empty_message(self, client, state_file):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"No price data yet" in resp.data

    def test_route_card_shows_label_and_price(self, client, state_file):
        write_state(state_file, {
            "prices": {
                "YVR-FRA 2027-03-15": {
                    "price": 2135.0, "previous_price": 1972.0,
                    "updated": "2026-08-17T19:30:00-07:00",
                    "details": {}, "history": [{"price": 2135.0, "timestamp": "2026-08-17T19:30:00-07:00"}],
                },
            },
        })
        resp = client.get("/")
        assert b"YVR-FRA 2027-03-15" in resp.data
        assert b"2135.00" in resp.data

    def test_delete_button_label_is_html_attribute_safe(self, client, state_file):
        # Regression check: the onclick attribute must survive a label that,
        # once JSON-encoded, contains double quotes (i.e. every label) without
        # prematurely closing the surrounding HTML attribute.
        write_state(state_file, {
            "prices": {
                "YVR-FRA 2027-03-15": {
                    "price": 2135.0, "history": [{"price": 2135.0, "timestamp": "t"}],
                },
            },
        })
        resp = client.get("/")
        html = resp.data.decode()
        assert "onclick='deleteRouteData(\"YVR-FRA 2027-03-15\"" in html


# ---------------------------------------------------------------------------
# Cron schedule
# ---------------------------------------------------------------------------

class TestTimeRe:
    @pytest.mark.parametrize("value", ["07:30", "0:00", "23:59", "13:05"])
    def test_valid_times(self, value):
        assert webapp.TIME_RE.match(value)

    @pytest.mark.parametrize("value", ["24:00", "7:60", "7-30", "abc", ""])
    def test_invalid_times(self, value):
        assert not webapp.TIME_RE.match(value)


class TestBuildCronBlock:
    def test_contains_markers_and_sorted_times(self):
        block = webapp.build_cron_block(["19:30", "07:30", "13:30"])
        assert block.startswith(webapp.CRON_BEGIN)
        assert block.endswith(webapp.CRON_END)
        lines = [l for l in block.splitlines() if l not in (webapp.CRON_BEGIN, webapp.CRON_END)]
        assert [l.split()[:2] for l in lines] == [["30", "7"], ["30", "13"], ["30", "19"]]

    def test_uses_venv_interpreter_and_monitor_script(self):
        block = webapp.build_cron_block(["07:30"])
        assert webapp.sys.executable in block
        assert str(webapp.MONITOR_SCRIPT) in block

    def test_dedupes_times(self):
        block = webapp.build_cron_block(["07:30", "07:30"])
        lines = [l for l in block.splitlines() if l not in (webapp.CRON_BEGIN, webapp.CRON_END)]
        assert len(lines) == 1


class TestCurrentSchedule:
    def test_no_crontab_returns_empty(self, monkeypatch):
        monkeypatch.setattr(webapp, "read_crontab", lambda: "")
        assert webapp.current_schedule() == []

    def test_no_faremonkey_block_returns_empty(self, monkeypatch):
        monkeypatch.setattr(webapp, "read_crontab", lambda: "0 3 * * * some-other-job\n")
        assert webapp.current_schedule() == []

    def test_extracts_times_from_managed_block(self, monkeypatch):
        body = (
            "0 3 * * * unrelated-job\n"
            + webapp.CRON_BEGIN + "\n"
            + "30 7 * * * cd /x && /x/.venv/bin/python /x/flight_monitor.py >/dev/null 2>&1\n"
            + "30 19 * * * cd /x && /x/.venv/bin/python /x/flight_monitor.py >/dev/null 2>&1\n"
            + webapp.CRON_END + "\n"
        )
        monkeypatch.setattr(webapp, "read_crontab", lambda: body)
        assert webapp.current_schedule() == ["07:30", "19:30"]


class TestApiSchedule:
    def test_get_reflects_current_schedule(self, client, monkeypatch):
        monkeypatch.setattr(webapp, "read_crontab", lambda: "")
        resp = client.get("/api/schedule")
        body = resp.get_json()
        assert body["times"] == []
        assert webapp.CRON_BEGIN in body["preview"]

    def test_post_rejects_empty_times(self, client):
        resp = client.post("/api/schedule", json={"times": []})
        assert resp.status_code == 400

    def test_post_rejects_malformed_time(self, client):
        resp = client.post("/api/schedule", json={"times": ["25:00"]})
        assert resp.status_code == 400
        assert "25:00" in resp.get_json()["errors"][0]

    def test_post_publishes_via_crontab_command(self, client, monkeypatch):
        calls = []

        def fake_run(args, input=None, capture_output=None, text=None):
            calls.append((args, input))
            if args == ["crontab", "-l"]:
                return mock.Mock(returncode=0, stdout="")
            return mock.Mock(returncode=0, stderr="")

        monkeypatch.setattr(webapp.subprocess, "run", fake_run)
        resp = client.post("/api/schedule", json={"times": ["07:30", "13:30", "19:30"]})
        assert resp.status_code == 200
        assert resp.get_json()["times"] == ["07:30", "13:30", "19:30"]

        install_call = next(c for c in calls if c[0] == ["crontab", "-"])
        assert webapp.CRON_BEGIN in install_call[1]
        assert "30 7 * * *" in install_call[1]

    def test_post_returns_500_on_crontab_failure(self, client, monkeypatch):
        def fake_run(args, input=None, capture_output=None, text=None):
            if args == ["crontab", "-l"]:
                return mock.Mock(returncode=0, stdout="")
            return mock.Mock(returncode=1, stderr="permission denied")

        monkeypatch.setattr(webapp.subprocess, "run", fake_run)
        resp = client.post("/api/schedule", json={"times": ["07:30"]})
        assert resp.status_code == 500
        assert "permission denied" in resp.get_json()["errors"][0]

    def test_publish_preserves_unrelated_crontab_entries(self, monkeypatch):
        existing = "0 3 * * * some-other-job\n"
        calls = []

        def fake_run(args, input=None, capture_output=None, text=None):
            calls.append((args, input))
            if args == ["crontab", "-l"]:
                return mock.Mock(returncode=0, stdout=existing)
            return mock.Mock(returncode=0, stderr="")

        monkeypatch.setattr(webapp.subprocess, "run", fake_run)
        webapp.publish_schedule(["07:30"])
        install_call = next(c for c in calls if c[0] == ["crontab", "-"])
        assert "some-other-job" in install_call[1]
        assert webapp.CRON_BEGIN in install_call[1]

    def test_publish_replaces_existing_faremonkey_block(self, monkeypatch):
        existing = (
            "0 3 * * * some-other-job\n"
            + webapp.CRON_BEGIN + "\n30 7 * * * old-line\n" + webapp.CRON_END + "\n"
        )
        calls = []

        def fake_run(args, input=None, capture_output=None, text=None):
            calls.append((args, input))
            if args == ["crontab", "-l"]:
                return mock.Mock(returncode=0, stdout=existing)
            return mock.Mock(returncode=0, stderr="")

        monkeypatch.setattr(webapp.subprocess, "run", fake_run)
        webapp.publish_schedule(["13:30"])
        install_call = next(c for c in calls if c[0] == ["crontab", "-"])
        assert "old-line" not in install_call[1]
        assert "some-other-job" in install_call[1]
        assert "30 13 * * *" in install_call[1]


# ---------------------------------------------------------------------------
# Port helpers
# ---------------------------------------------------------------------------

class TestPortHelpers:
    def test_free_port_is_then_available(self):
        port = webapp._free_port("127.0.0.1")
        assert webapp._port_available("127.0.0.1", port) is True

    def test_bound_port_is_unavailable(self):
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            port = s.getsockname()[1]
            assert webapp._port_available("127.0.0.1", port) is False
