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

@pytest.fixture(autouse=True)
def no_live_crontab(monkeypatch):
    """Every schedule-aware endpoint reads the crontab, including POST /api/routes.

    Default all of them to "no crontab installed"; tests that care about the
    published schedule override read_crontab themselves.
    """
    monkeypatch.setattr(webapp, "read_crontab", lambda: "")


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

    def test_valid_teens(self):
        routes = [{"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15", "teens": 2}]
        assert webapp.validate_routes(routes) == []

    def test_zero_teens_is_valid(self):
        routes = [{"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15", "teens": 0}]
        assert webapp.validate_routes(routes) == []

    def test_negative_teens(self):
        errors = webapp.validate_routes([{
            "origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15", "teens": -1,
        }])
        assert any("teens must be a non-negative integer" in e for e in errors)

    def test_non_numeric_teens(self):
        errors = webapp.validate_routes([{
            "origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15", "teens": "two",
        }])
        assert any("teens must be a non-negative integer" in e for e in errors)

    def test_valid_max_duration_hours(self):
        routes = [{"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15",
                   "max_duration_hours": 24}]
        assert webapp.validate_routes(routes) == []

    def test_zero_max_duration_hours_invalid(self):
        errors = webapp.validate_routes([{
            "origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15",
            "max_duration_hours": 0,
        }])
        assert any("max_duration_hours must be a positive number" in e for e in errors)

    def test_negative_max_duration_hours(self):
        errors = webapp.validate_routes([{
            "origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15",
            "max_duration_hours": -5,
        }])
        assert any("max_duration_hours must be a positive number" in e for e in errors)

    def test_valid_run_times(self):
        routes = [{"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15",
                   "run_times": ["7:30", "19:30"]}]
        assert webapp.validate_routes(routes) == []

    @pytest.mark.parametrize("value", [["25:00"], ["7:60"], ["0730"], [730], "07:30"])
    def test_bad_run_times(self, value):
        routes = [{"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15",
                   "run_times": value}]
        errors = webapp.validate_routes(routes)
        assert any("run_times must be a list" in e for e in errors)

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

    def test_valid_legs_route(self):
        routes = [{"legs": [
            {"origin": "JFK", "destination": "HEL", "date": "2026-09-15"},
            {"origin": "HEL", "destination": "BER", "date": "2026-09-20"},
            {"origin": "BER", "destination": "JFK", "date": "2026-09-25"},
        ]}]
        assert webapp.validate_routes(routes) == []

    def test_legs_route_not_flagged_for_missing_simple_fields(self):
        # A legs route has no origin/destination/departure_date of its own —
        # those checks must not fire for it.
        routes = [{"legs": [
            {"origin": "JFK", "destination": "HEL", "date": "2026-09-15"},
            {"origin": "HEL", "destination": "BER", "date": "2026-09-20"},
        ]}]
        errors = webapp.validate_routes(routes)
        assert not any("origin" in e or "destination" in e or "departure_date" in e for e in errors)

    def test_legs_route_needs_at_least_two_legs(self):
        errors = webapp.validate_routes(
            [{"legs": [{"origin": "JFK", "destination": "HEL", "date": "2026-09-15"}]}]
        )
        assert any("at least 2 legs" in e for e in errors)

    def test_legs_route_missing_leg_field(self):
        errors = webapp.validate_routes([{"legs": [
            {"origin": "JFK", "destination": "HEL"},
            {"origin": "HEL", "destination": "BER", "date": "2026-09-20"},
        ]}])
        assert any("missing required field" in e for e in errors)


class TestNormalizeRoute:
    def test_uppercases_iata_codes(self):
        out = webapp.normalize_route({"origin": "yvr", "destination": "fra", "departure_date": "2027-03-15"})
        assert out["origin"] == "YVR"
        assert out["destination"] == "FRA"

    def test_omits_absent_optional_fields(self):
        out = webapp.normalize_route({"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15"})
        assert set(out) == {"origin", "destination", "departure_date"}

    def test_teens_coerced_to_int(self):
        out = webapp.normalize_route({
            "origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15", "teens": "2",
        })
        assert out["teens"] == 2

    def test_zero_teens_included(self):
        out = webapp.normalize_route({
            "origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15", "teens": 0,
        })
        assert out["teens"] == 0

    def test_max_duration_hours_coerced_to_float(self):
        out = webapp.normalize_route({
            "origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15",
            "max_duration_hours": "24",
        })
        assert out["max_duration_hours"] == 24.0

    def test_max_duration_hours_omitted_when_absent(self):
        out = webapp.normalize_route({"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15"})
        assert "max_duration_hours" not in out

    def test_includes_return_date_when_present(self):
        out = webapp.normalize_route({
            "origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15",
            "return_date": "2027-03-27",
        })
        assert out["return_date"] == "2027-03-27"

    def test_run_times_padded_sorted_and_deduped(self):
        out = webapp.normalize_route({
            "origin": "yvr", "destination": "fra", "departure_date": "2027-03-15",
            "run_times": ["19:30", "7:30", "07:30"],
        })
        assert out["run_times"] == ["07:30", "19:30"]

    def test_run_times_supersede_legacy_run_hours(self):
        out = webapp.normalize_route({
            "origin": "yvr", "destination": "fra", "departure_date": "2027-03-15",
            "run_times": ["19:30"], "run_hours": [7],
        })
        assert out["run_times"] == ["19:30"]
        assert "run_hours" not in out

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

    def test_legs_passed_through_verbatim(self):
        legs = [
            {"origin": "JFK", "destination": "HEL", "date": "2026-09-15"},
            {"origin": "HEL", "destination": "BER", "date": "2026-09-20"},
            {"origin": "BER", "destination": "JFK", "date": "2026-09-25"},
        ]
        out = webapp.normalize_route({"legs": legs})
        assert out["legs"] == legs
        assert "origin" not in out
        assert "departure_date" not in out

    def test_legs_route_still_gets_shared_optional_fields(self):
        out = webapp.normalize_route({
            "legs": [
                {"origin": "JFK", "destination": "HEL", "date": "2026-09-15"},
                {"origin": "HEL", "destination": "BER", "date": "2026-09-20"},
            ],
            "adults": "3",
            "run_times": ["19:30", "7:30"],
        })
        assert out["adults"] == 3
        assert out["run_times"] == ["07:30", "19:30"]


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

    def test_post_legs_route_round_trips_unchanged(self, client, routes_file):
        # No editor UI builds these yet, but POST /api/routes (used e.g. by the
        # /routes page's Save action) must not corrupt a hand-added multi-leg
        # route saved for an unrelated reason.
        legs = [
            {"origin": "JFK", "destination": "HEL", "date": "2026-09-15"},
            {"origin": "HEL", "destination": "BER", "date": "2026-09-20"},
            {"origin": "BER", "destination": "JFK", "date": "2026-09-25"},
        ]
        resp = client.post("/api/routes", json=[{"legs": legs}])
        assert resp.status_code == 200
        saved = json.loads(routes_file.read_text())
        assert saved == [{"legs": legs}]


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


class TestOutsideActiveHours:
    def test_flags_only_times_outside_the_window(self, monkeypatch):
        monkeypatch.setenv("ACTIVE_START", "7")
        monkeypatch.setenv("ACTIVE_END", "22")
        assert webapp.outside_active_hours(["06:59", "07:00", "21:59", "22:00"]) == ["06:59", "22:00"]

    def test_falls_back_to_defaults_on_junk_env(self, monkeypatch):
        monkeypatch.setenv("ACTIVE_START", "not-a-number")
        assert webapp.outside_active_hours(["05:00", "13:30"]) == ["05:00"]


class TestRouteSearchCost:
    def test_simple_route_costs_one(self):
        assert webapp.route_search_cost({"origin": "A", "destination": "B", "departure_date": "2026-01-01"}) == 1

    def test_legs_route_costs_leg_count(self):
        route = {"legs": [
            {"origin": "A", "destination": "B", "date": "2026-01-01"},
            {"origin": "B", "destination": "C", "date": "2026-01-05"},
        ]}
        assert webapp.route_search_cost(route) == 2


class TestSchedulePlan:
    def test_union_of_route_run_times(self, routes_file):
        routes_file.write_text(json.dumps([
            {"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15",
             "run_times": ["07:30", "19:30"]},
            {"origin": "SFO", "destination": "NRT", "departure_date": "2027-04-01",
             "run_times": ["13:30"]},
        ]))
        plan = webapp.schedule_plan()
        assert plan["times"] == ["07:30", "13:30", "19:30"]
        assert plan["by_time"]["13:30"] == ["SFO-NRT 2027-04-01"]
        assert plan["by_time"]["07:30"] == ["YVR-FRA 2027-03-15"]
        assert plan["searches_per_day"] == 3
        assert plan["searches_per_month"] == 90

    def test_route_without_times_runs_at_every_firing(self, routes_file):
        routes_file.write_text(json.dumps([
            {"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15",
             "run_times": ["07:30", "19:30"]},
            {"origin": "SFO", "destination": "NRT", "departure_date": "2027-04-01"},
        ]))
        plan = webapp.schedule_plan()
        assert plan["times"] == ["07:30", "19:30"]
        assert plan["unscheduled"] == ["SFO-NRT 2027-04-01"]
        assert all("SFO-NRT 2027-04-01" in labels for labels in plan["by_time"].values())
        assert plan["searches_per_day"] == 4

    def test_legacy_run_hours_keeps_its_published_firing(self, routes_file, monkeypatch):
        # Publishing before migrating must not drop the firings a run_hours route
        # relies on: hour 13 maps onto the published 13:30, not a new 13:00 line.
        monkeypatch.setattr(webapp, "current_schedule", lambda: ["07:30", "13:30"])
        routes_file.write_text(json.dumps([
            {"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15",
             "run_times": ["07:30"]},
            {"origin": "SFO", "destination": "NRT", "departure_date": "2027-04-01",
             "run_hours": [13]},
        ]))
        plan = webapp.schedule_plan()
        assert plan["times"] == ["07:30", "13:30"]
        assert plan["by_time"]["07:30"] == ["YVR-FRA 2027-03-15"]
        assert plan["by_time"]["13:30"] == ["SFO-NRT 2027-04-01"]
        assert plan["legacy"] == [
            {"label": "SFO-NRT 2027-04-01", "run_hours": [13], "as_times": ["13:30"]}
        ]

    def test_legacy_run_hours_falls_back_to_the_whole_hour(self, routes_file):
        routes_file.write_text(json.dumps([
            {"origin": "SFO", "destination": "NRT", "departure_date": "2027-04-01",
             "run_hours": [13]},
        ]))
        plan = webapp.schedule_plan()  # nothing published, so no minutes to inherit
        assert plan["times"] == ["13:00"]
        assert plan["by_time"]["13:00"] == ["SFO-NRT 2027-04-01"]

    def test_falls_back_to_published_schedule_when_no_route_has_times(self, routes_file, monkeypatch):
        routes_file.write_text(json.dumps([
            {"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15"},
        ]))
        monkeypatch.setattr(webapp, "current_schedule", lambda: ["09:00"])
        assert webapp.schedule_plan()["times"] == ["09:00"]

    def test_falls_back_to_defaults_when_nothing_published(self, routes_file):
        routes_file.write_text(json.dumps([
            {"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15"},
        ]))
        assert webapp.schedule_plan()["times"] == list(webapp.DEFAULT_TIMES)

    def test_flags_times_outside_active_hours(self, routes_file, monkeypatch):
        monkeypatch.setenv("ACTIVE_START", "7")
        monkeypatch.setenv("ACTIVE_END", "22")
        routes_file.write_text(json.dumps([
            {"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15",
             "run_times": ["05:00", "13:30", "23:00"]},
        ]))
        assert webapp.schedule_plan()["outside_active_hours"] == ["05:00", "23:00"]

    def test_legs_route_gets_a_distinct_correct_label(self, routes_file):
        routes_file.write_text(json.dumps([
            {"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15",
             "run_times": ["07:30"]},
            {"legs": [
                {"origin": "JFK", "destination": "HEL", "date": "2026-09-15"},
                {"origin": "HEL", "destination": "BER", "date": "2026-09-20"},
                {"origin": "BER", "destination": "JFK", "date": "2026-09-25"},
             ], "run_times": ["07:30"]},
        ]))
        plan = webapp.schedule_plan()
        labels = plan["by_time"]["07:30"]
        assert "YVR-FRA 2027-03-15" in labels
        assert "JFK-HEL-BER-JFK 2026-09-15" in labels
        assert "?-? ?" not in labels

    def test_legs_route_search_budget_counts_one_per_leg(self, routes_file):
        # A 3-leg route is priced as 3 independent one-way searches (see
        # flight_monitor._search_multi_leg), so the /schedule page's search
        # budget must count 3 for it, not 1 — understating this would let
        # someone publish a schedule that actually exceeds their plan.
        routes_file.write_text(json.dumps([
            {"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15",
             "run_times": ["07:30"]},
            {"legs": [
                {"origin": "JFK", "destination": "HEL", "date": "2026-09-15"},
                {"origin": "HEL", "destination": "BER", "date": "2026-09-20"},
                {"origin": "BER", "destination": "JFK", "date": "2026-09-25"},
             ], "run_times": ["07:30"]},
        ]))
        plan = webapp.schedule_plan()
        assert plan["searches_per_day"] == 4  # 1 simple route + 3 legs
        assert plan["searches_per_month"] == 120


class TestApiSchedule:
    def test_get_derives_times_from_routes(self, client, routes_file):
        routes_file.write_text(json.dumps([
            {"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15",
             "run_times": ["07:30"]},
        ]))
        body = client.get("/api/schedule").get_json()
        assert body["times"] == ["07:30"]
        assert body["published"] == []
        assert body["in_sync"] is False
        assert webapp.CRON_BEGIN in body["preview"]

    def test_get_reports_in_sync_when_crontab_matches(self, client, routes_file, monkeypatch):
        routes_file.write_text(json.dumps([
            {"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15",
             "run_times": ["07:30"]},
        ]))
        monkeypatch.setattr(webapp, "read_crontab", lambda: (
            webapp.CRON_BEGIN + "\n30 7 * * * cd /x && python /x/flight_monitor.py\n" + webapp.CRON_END + "\n"
        ))
        body = client.get("/api/schedule").get_json()
        assert body["in_sync"] is True

    def test_post_refuses_times_outside_active_hours(self, client, routes_file, monkeypatch):
        monkeypatch.setenv("ACTIVE_START", "7")
        monkeypatch.setenv("ACTIVE_END", "22")
        routes_file.write_text(json.dumps([
            {"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15",
             "run_times": ["05:00"]},
        ]))
        resp = client.post("/api/schedule")
        assert resp.status_code == 400
        assert "05:00" in resp.get_json()["errors"][0]

    def test_post_publishes_the_derived_schedule(self, client, routes_file, monkeypatch):
        routes_file.write_text(json.dumps([
            {"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15",
             "run_times": ["07:30", "19:30"]},
        ]))
        installed = []
        monkeypatch.setattr(
            webapp, "publish_schedule", lambda times: installed.append(list(times))
        )
        body = client.post("/api/schedule").get_json()
        assert installed == [["07:30", "19:30"]]
        assert body["ok"] is True
        assert body["in_sync"] is True

    def test_post_publishes_via_crontab_command(self, client, routes_file, monkeypatch):
        routes_file.write_text(json.dumps([
            {"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15",
             "run_times": ["07:30", "13:30", "19:30"]},
        ]))
        calls = []

        def fake_run(args, input=None, capture_output=None, text=None):
            calls.append((args, input))
            if args == ["crontab", "-l"]:
                return mock.Mock(returncode=0, stdout="")
            return mock.Mock(returncode=0, stderr="")

        monkeypatch.setattr(webapp.subprocess, "run", fake_run)
        resp = client.post("/api/schedule")
        assert resp.status_code == 200
        assert resp.get_json()["times"] == ["07:30", "13:30", "19:30"]

        install_call = next(c for c in calls if c[0] == ["crontab", "-"])
        assert webapp.CRON_BEGIN in install_call[1]
        assert "30 7 * * *" in install_call[1]

    def test_post_returns_500_on_crontab_failure(self, client, routes_file, monkeypatch):
        routes_file.write_text(json.dumps([
            {"origin": "YVR", "destination": "FRA", "departure_date": "2027-03-15",
             "run_times": ["07:30"]},
        ]))

        def fake_run(args, input=None, capture_output=None, text=None):
            if args == ["crontab", "-l"]:
                return mock.Mock(returncode=0, stdout="")
            return mock.Mock(returncode=1, stderr="permission denied")

        monkeypatch.setattr(webapp.subprocess, "run", fake_run)
        resp = client.post("/api/schedule")
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
        monkeypatch.setattr(webapp, "read_crontab", lambda: existing)  # opt out of the autouse stub
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
        monkeypatch.setattr(webapp, "read_crontab", lambda: existing)  # opt out of the autouse stub
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
