"""Tests for flight_monitor.py — pure-logic functions only (no live API calls)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

# Ensure the project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import flight_monitor as fm


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def state_file(tmp_path):
    """Yield a temporary state.json path, patching STATE_FILE for the test."""
    path = tmp_path / "state.json"
    with mock.patch.object(fm, "STATE_FILE", path):
        yield path


@pytest.fixture()
def responses_file(tmp_path):
    """Yield a temporary responses.jsonl path."""
    path = tmp_path / "responses.jsonl"
    with mock.patch.object(fm, "RESPONSES_FILE", path):
        yield path


@pytest.fixture()
def log_file(tmp_path):
    """Yield a temporary flight_monitor.log path."""
    path = tmp_path / "flight_monitor.log"
    with mock.patch.object(fm, "LOG_FILE", path):
        yield path


# ---------------------------------------------------------------------------
# Helpers: load / save JSON
# ---------------------------------------------------------------------------

class TestLoadSaveJson:
    def test_load_missing_file(self, tmp_path):
        assert fm.load_json(tmp_path / "nope.json") == {}

    def test_round_trip(self, tmp_path):
        path = tmp_path / "data.json"
        data = {"prices": {"A-B 2026-01-01": {"price": 100}}}
        fm.save_json(path, data)
        assert fm.load_json(path) == data

    def test_load_corrupt_file(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json", encoding="utf-8")
        with pytest.raises(SystemExit):
            fm.load_json(path)

    def test_save_atomic(self, tmp_path):
        path = tmp_path / "state.json"
        fm.save_json(path, {"a": 1})
        assert path.exists()
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == []


# ---------------------------------------------------------------------------
# Helpers: load_routes
# ---------------------------------------------------------------------------

class TestLoadRoutes:
    def test_valid_routes(self, tmp_path):
        path = tmp_path / "routes.json"
        routes = [{"origin": "YVR", "destination": "CUN", "departure_date": "2026-12-23"}]
        path.write_text(json.dumps(routes), encoding="utf-8")
        with mock.patch.object(fm, "ROUTES_FILE", path):
            assert fm.load_routes() == routes

    def test_empty_array_exits(self, tmp_path):
        path = tmp_path / "routes.json"
        path.write_text("[]", encoding="utf-8")
        with mock.patch.object(fm, "ROUTES_FILE", path):
            with pytest.raises(SystemExit):
                fm.load_routes()

    def test_missing_file_exits(self, tmp_path):
        with mock.patch.object(fm, "ROUTES_FILE", tmp_path / "nope.json"):
            with pytest.raises(SystemExit):
                fm.load_routes()

    def test_missing_required_field_exits(self, tmp_path):
        path = tmp_path / "routes.json"
        routes = [{"origin": "YVR", "departure_date": "2026-12-23"}]
        path.write_text(json.dumps(routes), encoding="utf-8")
        with mock.patch.object(fm, "ROUTES_FILE", path):
            with pytest.raises(SystemExit):
                fm.load_routes()

    def test_valid_legs_route(self, tmp_path):
        path = tmp_path / "routes.json"
        routes = [{"legs": [
            {"origin": "JFK", "destination": "HEL", "date": "2026-09-15"},
            {"origin": "HEL", "destination": "BER", "date": "2026-09-20"},
            {"origin": "BER", "destination": "JFK", "date": "2026-09-25"},
        ]}]
        path.write_text(json.dumps(routes), encoding="utf-8")
        with mock.patch.object(fm, "ROUTES_FILE", path):
            assert fm.load_routes() == routes

    def test_legs_route_missing_leg_field_exits(self, tmp_path):
        path = tmp_path / "routes.json"
        routes = [{"legs": [
            {"origin": "JFK", "destination": "HEL"},
            {"origin": "HEL", "destination": "BER", "date": "2026-09-20"},
        ]}]
        path.write_text(json.dumps(routes), encoding="utf-8")
        with mock.patch.object(fm, "ROUTES_FILE", path):
            with pytest.raises(SystemExit):
                fm.load_routes()

    def test_legs_route_needs_at_least_two_legs(self, tmp_path):
        path = tmp_path / "routes.json"
        routes = [{"legs": [{"origin": "JFK", "destination": "HEL", "date": "2026-09-15"}]}]
        path.write_text(json.dumps(routes), encoding="utf-8")
        with mock.patch.object(fm, "ROUTES_FILE", path):
            with pytest.raises(SystemExit):
                fm.load_routes()

    def test_legs_combined_with_simple_fields_exits(self, tmp_path):
        path = tmp_path / "routes.json"
        routes = [{
            "origin": "JFK", "destination": "HEL", "departure_date": "2026-09-15",
            "legs": [
                {"origin": "JFK", "destination": "HEL", "date": "2026-09-15"},
                {"origin": "HEL", "destination": "BER", "date": "2026-09-20"},
            ],
        }]
        path.write_text(json.dumps(routes), encoding="utf-8")
        with mock.patch.object(fm, "ROUTES_FILE", path):
            with pytest.raises(SystemExit):
                fm.load_routes()


# ---------------------------------------------------------------------------
# route_label
# ---------------------------------------------------------------------------

class TestRouteLabel:
    def test_simple_route(self):
        route = {"origin": "YVR", "destination": "CUN", "departure_date": "2026-12-23"}
        assert fm.route_label(route) == "YVR-CUN 2026-12-23"

    def test_legs_route(self):
        route = {"legs": [
            {"origin": "JFK", "destination": "HEL", "date": "2026-09-15"},
            {"origin": "HEL", "destination": "BER", "date": "2026-09-20"},
            {"origin": "BER", "destination": "JFK", "date": "2026-09-25"},
        ]}
        assert fm.route_label(route) == "JFK-HEL-BER-JFK 2026-09-15"

    def test_empty_legs_does_not_raise(self):
        assert fm.route_label({"legs": []}) == "? ?"

    def test_missing_fields_degrade_gracefully(self):
        assert fm.route_label({}) == "?-? ?"


# ---------------------------------------------------------------------------
# Helpers: time / scheduling
# ---------------------------------------------------------------------------

class TestActiveHours:
    def test_within_window(self):
        dt = datetime(2026, 6, 28, 12, 0)
        with mock.patch.object(fm, "current_local_time", return_value=dt), \
             mock.patch.object(fm, "ACTIVE_START", 7), \
             mock.patch.object(fm, "ACTIVE_END", 22):
            assert fm.is_within_active_hours() is True

    def test_before_window(self):
        dt = datetime(2026, 6, 28, 5, 0)
        with mock.patch.object(fm, "current_local_time", return_value=dt), \
             mock.patch.object(fm, "ACTIVE_START", 7), \
             mock.patch.object(fm, "ACTIVE_END", 22):
            assert fm.is_within_active_hours() is False

    def test_after_window(self):
        dt = datetime(2026, 6, 28, 23, 0)
        with mock.patch.object(fm, "current_local_time", return_value=dt), \
             mock.patch.object(fm, "ACTIVE_START", 7), \
             mock.patch.object(fm, "ACTIVE_END", 22):
            assert fm.is_within_active_hours() is False


class TestParseRunTime:
    @pytest.mark.parametrize("value,expected", [
        ("00:00", 0), ("7:30", 450), ("07:30", 450), ("23:59", 1439), (" 13:05 ", 785),
    ])
    def test_valid(self, value, expected):
        assert fm.parse_run_time(value) == expected

    @pytest.mark.parametrize("value", ["24:00", "7:60", "7-30", "0730", "", None, 730, "7:30:00"])
    def test_invalid(self, value):
        assert fm.parse_run_time(value) is None


class TestRouteRunsAt:
    def at(self, hour, minute):
        return datetime(2026, 6, 28, hour, minute)

    def test_no_schedule_runs_on_every_firing(self):
        assert fm.route_runs_at({"origin": "A"}, self.at(7, 30)) is True

    def test_run_times_matching(self):
        route = {"run_times": ["07:30", "19:30"]}
        assert fm.route_runs_at(route, self.at(19, 30)) is True

    def test_run_times_not_matching(self):
        assert fm.route_runs_at({"run_times": ["13:30"]}, self.at(7, 30)) is False

    def test_late_start_within_tolerance_still_counts(self):
        # Interpreter startup and the account-quota sync happen before routes are
        # filtered, so the firing must not have to land on the exact minute.
        with mock.patch.object(fm, "RUN_TIME_TOLERANCE_MIN", 10):
            assert fm.route_runs_at({"run_times": ["07:30"]}, self.at(7, 36)) is True
            assert fm.route_runs_at({"run_times": ["07:30"]}, self.at(7, 45)) is False

    def test_tolerance_wraps_around_midnight(self):
        with mock.patch.object(fm, "RUN_TIME_TOLERANCE_MIN", 10):
            assert fm.route_runs_at({"run_times": ["00:00"]}, self.at(23, 55)) is True

    def test_malformed_run_time_is_ignored_not_fatal(self):
        assert fm.route_runs_at({"run_times": ["7-30"]}, self.at(7, 30)) is False

    def test_legacy_run_hours_still_honoured(self):
        assert fm.route_runs_at({"run_hours": [7, 13, 19]}, self.at(13, 5)) is True
        assert fm.route_runs_at({"run_hours": [13]}, self.at(7, 30)) is False

    def test_run_times_takes_precedence_over_run_hours(self):
        route = {"run_times": ["19:30"], "run_hours": [7]}
        assert fm.route_runs_at(route, self.at(7, 30)) is False
        assert fm.route_runs_at(route, self.at(19, 30)) is True


# ---------------------------------------------------------------------------
# API call tracking
# ---------------------------------------------------------------------------

class TestCallTracking:
    def test_get_count_empty(self):
        assert fm.get_call_count({}) == 0

    def test_increment_and_get(self):
        state = {}
        fm.increment_call_count(state, 3)
        assert fm.get_call_count(state) == 3
        fm.increment_call_count(state, 2)
        assert fm.get_call_count(state) == 5

    def test_can_make_calls_within_cap(self):
        fm._this_month_usage = 235
        fm._calls_made_this_run = 0
        with mock.patch.object(fm, "MONTHLY_CALL_CAP", 240):
            assert fm.can_make_calls(5) is True
            assert fm.can_make_calls(6) is False

    def test_can_make_calls_counts_calls_made_this_run(self):
        fm._this_month_usage = 235
        fm._calls_made_this_run = 3
        with mock.patch.object(fm, "MONTHLY_CALL_CAP", 240):
            assert fm.can_make_calls(2) is True
            assert fm.can_make_calls(3) is False

    def test_can_make_calls_fails_closed_when_usage_unknown(self):
        fm._this_month_usage = None
        fm._calls_made_this_run = 0
        with mock.patch.object(fm, "MONTHLY_CALL_CAP", 240):
            assert fm.can_make_calls(1) is False

    def test_current_usage(self):
        fm._this_month_usage = 40
        fm._calls_made_this_run = 4
        assert fm.current_usage() == 44

    def test_current_usage_unknown_when_sync_failed(self):
        fm._this_month_usage = None
        assert fm.current_usage() is None

    def test_record_call_increments_this_run_tally(self):
        fm._calls_made_this_run = 0
        fm.record_call()
        fm.record_call()
        assert fm._calls_made_this_run == 2


# ---------------------------------------------------------------------------
# Account quota tracking
# ---------------------------------------------------------------------------

class TestSearchesLeft:
    def test_decrement(self):
        fm._searches_left = 100
        fm.decrement_searches_left()
        assert fm._searches_left == 99

    def test_decrement_when_none(self):
        fm._searches_left = None
        fm.decrement_searches_left()
        assert fm._searches_left is None

    def test_log_with_value(self):
        fm._searches_left = 42
        assert fm.log_searches_left() == " [42 left on plan]"

    def test_log_when_none(self):
        fm._searches_left = None
        assert fm.log_searches_left() == ""

    def test_sync_success(self):
        fm._searches_left = None
        fm._this_month_usage = None
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"plan_searches_left": 150, "this_month_usage": 49}
        with mock.patch("requests.get", return_value=mock_resp):
            result = fm.sync_account_quota()
        assert result == 150
        assert fm._searches_left == 150
        assert fm._this_month_usage == 49

    def test_sync_fallback_field(self):
        fm._searches_left = None
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"total_searches_left": 99}
        with mock.patch("requests.get", return_value=mock_resp):
            result = fm.sync_account_quota()
        assert result == 99

    def test_sync_http_error(self):
        fm._searches_left = None
        mock_resp = mock.Mock()
        mock_resp.status_code = 500
        with mock.patch("requests.get", return_value=mock_resp):
            result = fm.sync_account_quota()
        assert result is None
        assert fm._searches_left is None

    def test_sync_network_error(self):
        fm._searches_left = None
        import requests as req
        with mock.patch("requests.get", side_effect=req.ConnectionError("down")):
            result = fm.sync_account_quota()
        assert result is None


# ---------------------------------------------------------------------------
# US layover filter
# ---------------------------------------------------------------------------

class TestHasUsLayover:
    def test_nonstop(self):
        assert fm._has_us_layover({"flights": [{}]}) is False

    def test_no_layovers_key(self):
        assert fm._has_us_layover({}) is False

    def test_us_layover_detected(self):
        flight = {"layovers": [{"id": "LAX", "duration": 120}]}
        assert fm._has_us_layover(flight) is True

    def test_non_us_layover_passes(self):
        flight = {"layovers": [{"id": "GDL", "duration": 90}]}
        assert fm._has_us_layover(flight) is False

    def test_multiple_layovers_one_us(self):
        flight = {"layovers": [{"id": "GDL"}, {"id": "DFW"}]}
        assert fm._has_us_layover(flight) is True

    def test_case_insensitive(self):
        flight = {"layovers": [{"id": "lax"}]}
        assert fm._has_us_layover(flight) is True

    def test_missing_id_field(self):
        flight = {"layovers": [{"name": "Some Airport"}]}
        assert fm._has_us_layover(flight) is False


# ---------------------------------------------------------------------------
# _extract_details
# ---------------------------------------------------------------------------

class TestExtractDetails:
    def test_nonstop_flight(self):
        flight = {
            "flights": [{
                "airline": "WestJet",
                "flight_number": "WS 3030",
                "departure_airport": {"id": "YVR", "time": "2026-12-23 07:00"},
                "arrival_airport": {"id": "CUN", "time": "2026-12-23 15:10"},
            }],
            "total_duration": 370,
        }
        d = fm._extract_details(flight)
        assert d["airlines"] == ["WestJet"]
        assert d["flight_numbers"] == ["WS 3030"]
        assert d["stops"] == 0
        assert d["layover_airports"] == []
        assert d["departure_time"] == "2026-12-23 07:00"
        assert d["arrival_time"] == "2026-12-23 15:10"
        assert d["total_duration"] == 370

    def test_connecting_flight(self):
        flight = {
            "flights": [
                {
                    "airline": "Delta",
                    "flight_number": "DL 3184",
                    "departure_airport": {"id": "YVR", "time": "2026-12-23 19:30"},
                    "arrival_airport": {"id": "LAX", "time": "2026-12-23 21:32"},
                },
                {
                    "airline": "Delta",
                    "flight_number": "DL 623",
                    "departure_airport": {"id": "LAX", "time": "2026-12-23 23:25"},
                    "arrival_airport": {"id": "CUN", "time": "2026-12-24 07:05"},
                },
            ],
            "layovers": [{"id": "LAX", "duration": 113}],
            "total_duration": 575,
        }
        d = fm._extract_details(flight)
        assert d["airlines"] == ["Delta"]
        assert d["flight_numbers"] == ["DL 3184", "DL 623"]
        assert d["stops"] == 1
        assert d["layover_airports"] == ["LAX"]
        assert d["departure_time"] == "2026-12-23 19:30"
        assert d["arrival_time"] == "2026-12-24 07:05"

    def test_multi_airline(self):
        flight = {
            "flights": [
                {"airline": "Air Canada", "flight_number": "AC 100",
                 "departure_airport": {"id": "YVR", "time": "2026-01-01 08:00"},
                 "arrival_airport": {"id": "YYZ", "time": "2026-01-01 15:00"}},
                {"airline": "WestJet", "flight_number": "WS 200",
                 "departure_airport": {"id": "YYZ", "time": "2026-01-01 17:00"},
                 "arrival_airport": {"id": "CUN", "time": "2026-01-01 21:00"}},
            ],
            "layovers": [{"id": "YYZ"}],
            "total_duration": 780,
        }
        d = fm._extract_details(flight)
        assert d["airlines"] == ["Air Canada", "WestJet"]

    def test_empty_segments(self):
        d = fm._extract_details({})
        assert d["airlines"] == []
        assert d["stops"] == 0
        assert d["departure_time"] is None


# ---------------------------------------------------------------------------
# _summarize
# ---------------------------------------------------------------------------

class TestSummarize:
    def test_basic(self):
        flight = {
            "price": 4373,
            "flights": [{"airline": "Flair Airlines"}],
            "total_duration": 355,
        }
        s = fm._summarize(flight)
        assert s == {"price": 4373.0, "airlines": ["Flair Airlines"],
                      "stops": 0, "total_duration": 355}

    def test_with_layovers(self):
        flight = {
            "price": 4889,
            "flights": [{"airline": "Delta"}, {"airline": "Delta"}],
            "layovers": [{"id": "LAX"}],
            "total_duration": 575,
        }
        s = fm._summarize(flight)
        assert s["stops"] == 1
        assert s["airlines"] == ["Delta"]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

class TestFormatPrice:
    def test_whole_number(self):
        assert fm._format_price(4373.0) == "4,373"

    def test_with_decimals(self):
        assert fm._format_price(1234.56) == "1,234.56"

    def test_zero(self):
        assert fm._format_price(0.0) == "0"

    def test_large(self):
        assert fm._format_price(12345.0) == "12,345"


class TestOvernight:
    def test_same_day(self):
        assert fm._overnight("2026-12-23 08:00", "2026-12-23 16:00") == ""

    def test_next_day(self):
        assert fm._overnight("2026-12-23 23:30", "2026-12-24 07:25") == " (+1)"

    def test_two_days(self):
        assert fm._overnight("2026-12-23 23:30", "2026-12-25 07:25") == " (+2)"

    def test_none_inputs(self):
        assert fm._overnight(None, "2026-12-24 07:25") == ""
        assert fm._overnight("2026-12-23 23:30", None) == ""
        assert fm._overnight(None, None) == ""

    def test_bad_format(self):
        assert fm._overnight("bad", "2026-12-24 07:25") == ""


class TestFormatDate:
    def test_normal(self):
        assert fm._format_date("2026-12-30") == "Dec 30"

    def test_bad_input(self):
        assert fm._format_date("not-a-date") == "not-a-date"


class TestFormatOffer:
    def test_basic(self):
        offer = {"airlines": ["WestJet"], "stops": 0,
                 "total_duration": 330, "price_level": "low"}
        result = fm.format_offer(offer)
        assert "WestJet" in result
        assert "nonstop" in result
        assert "5h 30m" in result
        assert "low vs typical" in result

    def test_with_stops(self):
        offer = {"airlines": ["Delta"], "stops": 2, "total_duration": 600}
        result = fm.format_offer(offer)
        assert "2 stops" in result

    def test_one_stop(self):
        offer = {"airlines": ["Delta"], "stops": 1}
        result = fm.format_offer(offer)
        assert "1 stop" in result
        assert "stops" not in result

    def test_no_price_level(self):
        offer = {"airlines": ["AC"], "stops": 0}
        result = fm.format_offer(offer)
        assert "typical" not in result


# ---------------------------------------------------------------------------
# format_telegram
# ---------------------------------------------------------------------------

class TestFormatTelegram:
    ROUTE = {
        "origin": "YVR", "destination": "CUN",
        "departure_date": "2026-12-23", "return_date": "2026-12-30",
        "adults": 4,
    }
    OFFER = {
        "price": 4373.0, "airlines": ["Flair Airlines"], "stops": 0,
        "layover_airports": [], "total_duration": 355,
        "departure_time": "2026-12-23 23:30",
        "arrival_time": "2026-12-24 07:25",
        "price_level": "high",
    }

    def test_header_format(self):
        msg = fm.format_telegram(self.ROUTE, self.OFFER, "✈️", -5.2)
        lines = msg.split("\n")
        assert lines[0] == "✈️ YVR → CUN (4 pax)"

    def test_price_drop(self):
        # Pin CURRENCY: it is read from the environment at import, so a local
        # .env would otherwise decide whether this assertion holds.
        with mock.patch.object(fm, "CURRENCY", "CAD"):
            msg = fm.format_telegram(self.ROUTE, self.OFFER, "✈️", -5.2)
        assert "CAD 4,373 (↓5.2%)" in msg

    def test_price_rise(self):
        msg = fm.format_telegram(self.ROUTE, self.OFFER, "⚠️", 3.8)
        assert "(↑3.8%)" in msg

    def test_baseline_no_arrow(self):
        with mock.patch.object(fm, "CURRENCY", "CAD"):
            msg = fm.format_telegram(self.ROUTE, self.OFFER, "🐒", None)
        assert "↓" not in msg
        assert "↑" not in msg
        assert "CAD 4,373" in msg

    def test_no_change_no_arrow(self):
        msg = fm.format_telegram(self.ROUTE, self.OFFER, "➡️", 0)
        assert "↓" not in msg
        assert "↑" not in msg

    def test_price_level_on_summary_line(self):
        msg = fm.format_telegram(self.ROUTE, self.OFFER, "✈️", -1.0)
        lines = msg.split("\n")
        assert "high vs typical" in lines[1]

    def test_flight_details_on_summary_line(self):
        msg = fm.format_telegram(self.ROUTE, self.OFFER, "✈️", -1.0)
        lines = msg.split("\n")
        assert "Flair Airlines" in lines[1]
        assert "nonstop" in lines[1]
        assert "5h 55m" in lines[1]

    def test_outbound_with_overnight(self):
        msg = fm.format_telegram(self.ROUTE, self.OFFER, "✈️", -1.0)
        assert "Outbound: Dec 23 | 23:30 → 07:25 (+1)" in msg

    def test_outbound_same_day(self):
        offer = {**self.OFFER,
                 "departure_time": "2026-12-23 08:00",
                 "arrival_time": "2026-12-23 16:30"}
        msg = fm.format_telegram(self.ROUTE, offer, "✈️", -1.0)
        assert "Outbound: Dec 23 | 08:00 → 16:30" in msg
        assert "(+1)" not in msg

    def test_inbound_shown_for_round_trip(self):
        msg = fm.format_telegram(self.ROUTE, self.OFFER, "✈️", -1.0)
        assert "Inbound: Dec 30 | flight times not available" in msg

    def test_no_inbound_for_one_way(self):
        route_ow = {"origin": "YVR", "destination": "SJD",
                     "departure_date": "2027-03-16", "adults": 2}
        msg = fm.format_telegram(route_ow, self.OFFER, "🔹", -1.0)
        assert "Inbound" not in msg

    def test_connection_layover(self):
        offer = {**self.OFFER, "stops": 1, "layover_airports": ["LAX"]}
        msg = fm.format_telegram(self.ROUTE, offer, "✈️", -1.0)
        assert "1 stop LAX" in msg

    def test_multi_stop_layovers(self):
        offer = {**self.OFFER, "stops": 2, "layover_airports": ["LAX", "DFW"]}
        msg = fm.format_telegram(self.ROUTE, offer, "✈️", -1.0)
        assert "2 stop LAX→DFW" in msg

    def test_adults_default_1(self):
        route = {"origin": "A", "destination": "B", "departure_date": "2026-01-01"}
        msg = fm.format_telegram(route, self.OFFER, "🐒", None)
        assert "(1 pax)" in msg

    def test_blank_line_separates_itinerary(self):
        msg = fm.format_telegram(self.ROUTE, self.OFFER, "✈️", -1.0)
        lines = msg.split("\n")
        assert lines[2] == ""

    def test_missing_times(self):
        offer = {**self.OFFER, "departure_time": None, "arrival_time": None}
        msg = fm.format_telegram(self.ROUTE, offer, "✈️", -1.0)
        assert "Outbound" not in msg
        assert "Inbound: Dec 30" in msg

    LEGS_ROUTE = {
        "legs": [
            {"origin": "JFK", "destination": "HEL", "date": "2026-09-15"},
            {"origin": "HEL", "destination": "BER", "date": "2026-09-20"},
            {"origin": "BER", "destination": "JFK", "date": "2026-09-25"},
        ],
        "adults": 1,
    }

    def test_legs_header_lists_full_chain(self):
        msg = fm.format_telegram(self.LEGS_ROUTE, self.OFFER, "🐒", None)
        lines = msg.split("\n")
        assert lines[0] == "🐒 JFK → HEL → BER → JFK (1 pax)"

    LEGS_OFFER = {
        "price": 6000.0,
        "airlines": ["Finnair", "Lufthansa"],
        "stops": 0,
        "total_duration": 900,
        "legs": [
            {"origin": "JFK", "destination": "HEL", "date": "2026-09-15", "price": 1000.0,
             "departure_time": "2026-09-15 08:00", "arrival_time": "2026-09-15 20:00",
             "total_duration": 625},
            {"origin": "HEL", "destination": "BER", "date": "2026-09-20", "price": 2000.0,
             "departure_time": "2026-09-20 09:00", "arrival_time": "2026-09-20 11:00",
             "total_duration": 120},
            {"origin": "BER", "destination": "JFK", "date": "2026-09-25", "price": 3000.0,
             "departure_time": None, "arrival_time": None, "total_duration": None},
        ],
    }

    def test_legs_lists_each_leg(self):
        # Pin CURRENCY: it is read from the environment at import, so a local
        # .env would otherwise decide whether this assertion holds.
        with mock.patch.object(fm, "CURRENCY", "USD"):
            msg = fm.format_telegram(self.LEGS_ROUTE, self.LEGS_OFFER, "🐒", None)
        assert "Leg 1: JFK → HEL | Sep 15 | 08:00 → 20:00 | 10h 25m | USD 1,000" in msg
        assert "Leg 2: HEL → BER | Sep 20 | 09:00 → 11:00 | 2h 00m | USD 2,000" in msg
        assert "Leg 3: BER → JFK | Sep 25 | flight times not available | USD 3,000" in msg

    def test_legs_no_outbound_inbound_wording(self):
        msg = fm.format_telegram(self.LEGS_ROUTE, self.OFFER, "🐒", None)
        assert "Outbound" not in msg
        assert "Inbound" not in msg

    def test_teens_shown_in_header(self):
        route = {**self.ROUTE, "adults": 2, "teens": 2}
        msg = fm.format_telegram(route, self.OFFER, "✈️", -1.0)
        assert "(2 adults + 2 teens)" in msg.split("\n")[0]

    def test_singular_adult_and_teen(self):
        route = {**self.ROUTE, "adults": 1, "teens": 1}
        msg = fm.format_telegram(route, self.OFFER, "✈️", -1.0)
        assert "(1 adult + 1 teen)" in msg.split("\n")[0]

    def test_no_teens_still_shows_pax(self):
        # self.ROUTE has no "teens" key at all — must not crash or change format.
        msg = fm.format_telegram(self.ROUTE, self.OFFER, "✈️", -1.0)
        assert "(4 pax)" in msg.split("\n")[0]


# ---------------------------------------------------------------------------
# Archive / trim
# ---------------------------------------------------------------------------

class TestArchiveResponse:
    def test_writes_jsonl(self, responses_file):
        with mock.patch.object(fm, "ARCHIVE_RESPONSES", True):
            fm.archive_response(
                {"origin": "YVR", "destination": "CUN", "departure_date": "2026-12-23"},
                {"api_key": "SECRET", "departure_id": "YVR"},
                {"best_flights": []},
            )
        lines = responses_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert "api_key" not in record["query"]
        assert record["route"] == "YVR-CUN 2026-12-23"

    def test_writes_jsonl_legs_route(self, responses_file):
        route = {"legs": [
            {"origin": "JFK", "destination": "HEL", "date": "2026-09-15"},
            {"origin": "HEL", "destination": "BER", "date": "2026-09-20"},
        ]}
        with mock.patch.object(fm, "ARCHIVE_RESPONSES", True):
            fm.archive_response(route, {"api_key": "SECRET"}, {"best_flights": []})
        lines = responses_file.read_text(encoding="utf-8").strip().split("\n")
        record = json.loads(lines[0])
        assert record["route"] == "JFK-HEL-BER 2026-09-15"

    def test_disabled(self, responses_file):
        with mock.patch.object(fm, "ARCHIVE_RESPONSES", False):
            fm.archive_response(
                {"origin": "A", "destination": "B"}, {}, {},
            )
        assert not responses_file.exists()


class TestIsOlderThan:
    def test_older(self):
        cutoff = datetime(2026, 6, 20, tzinfo=None)
        assert fm._is_older_than("2026-06-19T10:00:00", cutoff) is True

    def test_newer(self):
        cutoff = datetime(2026, 6, 20, tzinfo=None)
        assert fm._is_older_than("2026-06-21T10:00:00", cutoff) is False

    def test_unparseable_kept(self):
        cutoff = datetime(2026, 6, 20)
        assert fm._is_older_than("not-a-date", cutoff) is False

    def test_none_kept(self):
        cutoff = datetime(2026, 6, 20)
        assert fm._is_older_than(None, cutoff) is False


class TestTrimHistory:
    def test_removes_old_entries(self):
        state = {"prices": {"A-B 2026-01-01": {
            "history": [
                {"price": 100, "timestamp": "2026-06-01T00:00:00"},
                {"price": 200, "timestamp": "2026-06-25T00:00:00"},
            ]
        }}}
        cutoff = datetime(2026, 6, 20)
        removed = fm.trim_history(state, cutoff)
        assert removed == 1
        assert len(state["prices"]["A-B 2026-01-01"]["history"]) == 1


class TestTrimResponses:
    def test_removes_old_lines(self, responses_file):
        old = json.dumps({"timestamp": "2026-06-01T00:00:00", "data": "old"})
        new = json.dumps({"timestamp": "2026-06-25T00:00:00", "data": "new"})
        responses_file.write_text(old + "\n" + new + "\n", encoding="utf-8")
        cutoff = datetime(2026, 6, 20)
        removed = fm.trim_responses(cutoff)
        assert removed == 1
        kept = responses_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(kept) == 1
        assert json.loads(kept[0])["data"] == "new"

    def test_missing_file(self, responses_file):
        assert fm.trim_responses(datetime(2026, 6, 20)) == 0


# ---------------------------------------------------------------------------
# log() / LOG_FILE
# ---------------------------------------------------------------------------

class TestLogFile:
    def test_log_appends_to_file(self, log_file):
        fm.log("hello")
        assert "hello" in log_file.read_text(encoding="utf-8")

    def test_log_blank_appends_blank_line(self, log_file):
        fm.log()
        assert log_file.read_text(encoding="utf-8") == "\n"

    def test_log_survives_unwritable_file(self, tmp_path):
        with mock.patch.object(fm, "LOG_FILE", tmp_path / "nope" / "flight_monitor.log"):
            fm.log("should not raise")  # parent dir doesn't exist


class TestTrimLogs:
    def test_removes_old_lines(self, log_file):
        log_file.write_text(
            "2026-06-01 00:00:00  old line\n"
            "2026-06-25 00:00:00  new line\n",
            encoding="utf-8",
        )
        removed = fm.trim_logs(datetime(2026, 6, 20))
        assert removed == 1
        kept = log_file.read_text(encoding="utf-8").strip().split("\n")
        assert kept == ["2026-06-25 00:00:00  new line"]

    def test_keeps_blank_and_unparseable_lines(self, log_file):
        log_file.write_text("\nnot a timestamp\n2026-06-01 00:00:00  old\n", encoding="utf-8")
        removed = fm.trim_logs(datetime(2026, 6, 20))
        assert removed == 1
        kept = log_file.read_text(encoding="utf-8")
        assert "not a timestamp" in kept
        assert "old" not in kept

    def test_missing_file(self, log_file):
        assert fm.trim_logs(datetime(2026, 6, 20)) == 0


# ---------------------------------------------------------------------------
# _days_arg
# ---------------------------------------------------------------------------

class TestDaysArg:
    def test_default(self):
        assert fm._days_arg([], 3) == 3

    def test_provided(self):
        assert fm._days_arg(["--days", "5"], 3) == 5

    def test_missing_value(self):
        with pytest.raises(SystemExit):
            fm._days_arg(["--days"], 3)

    def test_non_integer(self):
        with pytest.raises(SystemExit):
            fm._days_arg(["--days", "abc"], 3)

    def test_zero(self):
        with pytest.raises(SystemExit):
            fm._days_arg(["--days", "0"], 3)

    def test_negative(self):
        with pytest.raises(SystemExit):
            fm._days_arg(["--days", "-1"], 3)


# ---------------------------------------------------------------------------
# search_cheapest (mocked HTTP)
# ---------------------------------------------------------------------------

class TestSearchCheapest:
    ROUTE = {
        "origin": "YVR", "destination": "CUN",
        "departure_date": "2026-12-23", "return_date": "2026-12-30",
        "adults": 4, "non_stop": False, "travel_class": "ECONOMY",
    }

    def _make_response(self, best=None, other=None, insights=None, error=None):
        data = {}
        if best is not None:
            data["best_flights"] = best
        if other is not None:
            data["other_flights"] = other
        if insights is not None:
            data["price_insights"] = insights
        if error is not None:
            data["error"] = error
        resp = mock.Mock()
        resp.status_code = 200
        resp.json.return_value = data
        resp.text = json.dumps(data)
        return resp

    def _flight(self, price, airline="TestAir", layovers=None):
        return {
            "price": price,
            "flights": [{
                "airline": airline,
                "flight_number": f"{airline[:2].upper()} 100",
                "departure_airport": {"id": "YVR", "time": "2026-12-23 08:00"},
                "arrival_airport": {"id": "CUN", "time": "2026-12-23 16:00"},
            }],
            "layovers": layovers or [],
            "total_duration": 480,
            "departure_token": "tok123",
        }

    def test_returns_cheapest(self):
        resp = self._make_response(
            best=[self._flight(5000), self._flight(4000)],
        )
        with mock.patch("requests.get", return_value=resp), \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False), \
             mock.patch.object(fm, "EXCLUDE_US_CONNECTIONS", False):
            offer = fm.search_cheapest(self.ROUTE)
        assert offer is not None
        assert offer["price"] == 4000.0

    def test_combines_best_and_other(self):
        resp = self._make_response(
            best=[self._flight(5000)],
            other=[self._flight(3000)],
        )
        with mock.patch("requests.get", return_value=resp), \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False), \
             mock.patch.object(fm, "EXCLUDE_US_CONNECTIONS", False):
            offer = fm.search_cheapest(self.ROUTE)
        assert offer["price"] == 3000.0

    def test_us_filter_excludes_layovers(self):
        us_flight = self._flight(3000, layovers=[{"id": "LAX", "duration": 120}])
        non_us_flight = self._flight(5000, layovers=[{"id": "GDL", "duration": 90}])
        resp = self._make_response(best=[us_flight, non_us_flight])
        with mock.patch("requests.get", return_value=resp), \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False), \
             mock.patch.object(fm, "EXCLUDE_US_CONNECTIONS", True):
            offer = fm.search_cheapest(self.ROUTE)
        assert offer["price"] == 5000.0

    def test_us_filter_all_excluded_returns_none(self):
        us_flight = self._flight(3000, layovers=[{"id": "LAX"}])
        resp = self._make_response(best=[us_flight])
        with mock.patch("requests.get", return_value=resp), \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False), \
             mock.patch.object(fm, "EXCLUDE_US_CONNECTIONS", True):
            offer = fm.search_cheapest(self.ROUTE)
        assert offer is None

    def test_multi_stop_excluded(self):
        one_stop = self._flight(5000, layovers=[{"id": "GDL"}])
        two_stop = self._flight(3000, layovers=[{"id": "GDL"}, {"id": "MEX"}])
        resp = self._make_response(best=[one_stop, two_stop])
        with mock.patch("requests.get", return_value=resp), \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False), \
             mock.patch.object(fm, "EXCLUDE_US_CONNECTIONS", False):
            offer = fm.search_cheapest(self.ROUTE)
        assert offer["price"] == 5000.0

    def test_all_multi_stop_returns_none(self):
        two_stop = self._flight(3000, layovers=[{"id": "GDL"}, {"id": "MEX"}])
        resp = self._make_response(best=[two_stop])
        with mock.patch("requests.get", return_value=resp), \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False), \
             mock.patch.object(fm, "EXCLUDE_US_CONNECTIONS", False):
            offer = fm.search_cheapest(self.ROUTE)
        assert offer is None

    def test_api_error_returns_none(self):
        resp = mock.Mock()
        resp.status_code = 500
        resp.text = "Internal Server Error"
        with mock.patch("requests.get", return_value=resp), \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False):
            offer = fm.search_cheapest(self.ROUTE)
        assert offer is None

    def test_network_error_returns_none(self):
        import requests as req
        with mock.patch("requests.get", side_effect=req.ConnectionError), \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False):
            offer = fm.search_cheapest(self.ROUTE)
        assert offer is None

    def test_empty_candidates_returns_none(self):
        resp = self._make_response(best=[], other=[])
        with mock.patch("requests.get", return_value=resp), \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False), \
             mock.patch.object(fm, "EXCLUDE_US_CONNECTIONS", False):
            offer = fm.search_cheapest(self.ROUTE)
        assert offer is None

    def test_captures_price_insights(self):
        resp = self._make_response(
            best=[self._flight(4000)],
            insights={"price_level": "high", "typical_price_range": [1700, 2750]},
        )
        with mock.patch("requests.get", return_value=resp), \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False), \
             mock.patch.object(fm, "EXCLUDE_US_CONNECTIONS", False):
            offer = fm.search_cheapest(self.ROUTE)
        assert offer["price_level"] == "high"
        assert offer["typical_price_range"] == [1700, 2750]

    def test_alternatives_capped_at_3(self):
        flights = [self._flight(p) for p in [100, 200, 300, 400, 500]]
        resp = self._make_response(best=flights)
        with mock.patch("requests.get", return_value=resp), \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False), \
             mock.patch.object(fm, "EXCLUDE_US_CONNECTIONS", False):
            offer = fm.search_cheapest(self.ROUTE)
        assert len(offer["alternatives"]) == 3

    def test_nonstop_price_captured(self):
        nonstop = self._flight(5000)
        connecting = self._flight(4000, layovers=[{"id": "GDL"}])
        resp = self._make_response(best=[nonstop, connecting])
        with mock.patch("requests.get", return_value=resp), \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False), \
             mock.patch.object(fm, "EXCLUDE_US_CONNECTIONS", False):
            offer = fm.search_cheapest(self.ROUTE)
        assert offer["price"] == 4000.0
        assert offer["nonstop_price"] == 5000.0

    def test_decrements_searches_left(self):
        fm._searches_left = 50
        resp = self._make_response(best=[self._flight(4000)])
        with mock.patch("requests.get", return_value=resp), \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False), \
             mock.patch.object(fm, "EXCLUDE_US_CONNECTIONS", False):
            fm.search_cheapest(self.ROUTE)
        assert fm._searches_left == 49

    def test_records_real_call_made(self):
        fm._calls_made_this_run = 0
        resp = self._make_response(best=[self._flight(4000)])
        with mock.patch("requests.get", return_value=resp), \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False), \
             mock.patch.object(fm, "EXCLUDE_US_CONNECTIONS", False):
            fm.search_cheapest(self.ROUTE)
        assert fm._calls_made_this_run == 1

    def test_api_error_json_body(self):
        resp = self._make_response(error="Your plan has run out of searches.")
        with mock.patch("requests.get", return_value=resp), \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False):
            offer = fm.search_cheapest(self.ROUTE)
        assert offer is None

    def test_one_way_route(self):
        route = {"origin": "YVR", "destination": "SJD",
                 "departure_date": "2027-03-16", "adults": 2}
        resp = self._make_response(best=[self._flight(2000)])
        with mock.patch("requests.get", return_value=resp) as mock_get, \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False), \
             mock.patch.object(fm, "EXCLUDE_US_CONNECTIONS", False):
            offer = fm.search_cheapest(route)
        call_params = mock_get.call_args[1]["params"]
        assert call_params["type"] == 2
        assert "return_date" not in call_params
        assert offer["price"] == 2000.0

    LEGS_ROUTE = {
        "legs": [
            {"origin": "JFK", "destination": "HEL", "date": "2026-09-15"},
            {"origin": "HEL", "destination": "BER", "date": "2026-09-20"},
            {"origin": "BER", "destination": "JFK", "date": "2026-09-25"},
        ],
        "adults": 2, "non_stop": False, "travel_class": "BUSINESS",
    }

    def test_legs_route_makes_one_one_way_search_per_leg(self):
        # SerpAPI's multi-city (type=3) call only prices the first leg — see
        # _search_multi_leg's docstring — so each leg is its own one-way
        # (type=2) search instead of a single multi_city_json request.
        responses = [
            self._make_response(best=[self._flight(1000)]),
            self._make_response(best=[self._flight(2000)]),
            self._make_response(best=[self._flight(3000)]),
        ]
        with mock.patch("requests.get", side_effect=responses) as mock_get, \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False), \
             mock.patch.object(fm, "EXCLUDE_US_CONNECTIONS", False):
            offer = fm.search_cheapest(self.LEGS_ROUTE)
        assert mock_get.call_count == 3
        calls = [c.kwargs["params"] for c in mock_get.call_args_list]
        expected_legs = self.LEGS_ROUTE["legs"]
        for call_params, leg in zip(calls, expected_legs):
            assert call_params["type"] == 2
            assert call_params["departure_id"] == leg["origin"]
            assert call_params["arrival_id"] == leg["destination"]
            assert call_params["outbound_date"] == leg["date"]
            assert "multi_city_json" not in call_params
            assert "return_date" not in call_params
            # Shared route settings apply to every leg.
            assert call_params["adults"] == 2
            assert call_params["travel_class"] == fm.TRAVEL_CLASS_MAP["BUSINESS"]
            assert call_params["stops"] == 0  # non_stop: False → any
        assert offer["price"] == 6000.0  # 1000 + 2000 + 3000

    def test_legs_route_offer_includes_per_leg_breakdown(self):
        responses = [
            self._make_response(best=[self._flight(1000, airline="Finnair")]),
            self._make_response(best=[self._flight(2000, airline="Lufthansa")]),
            self._make_response(best=[self._flight(3000, airline="Air Canada")]),
        ]
        with mock.patch("requests.get", side_effect=responses), \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False), \
             mock.patch.object(fm, "EXCLUDE_US_CONNECTIONS", False):
            offer = fm.search_cheapest(self.LEGS_ROUTE)
        assert [leg["price"] for leg in offer["legs"]] == [1000.0, 2000.0, 3000.0]
        assert offer["legs"][0]["origin"] == "JFK"
        assert offer["legs"][0]["destination"] == "HEL"
        assert offer["legs"][0]["date"] == "2026-09-15"
        assert offer["airlines"] == ["Finnair", "Lufthansa", "Air Canada"]

    def test_legs_route_sums_stops_and_duration_across_legs(self):
        responses = [
            self._make_response(best=[self._flight(1000, layovers=[{"id": "GDL"}])]),
            self._make_response(best=[self._flight(2000)]),
            self._make_response(best=[self._flight(3000, layovers=[{"id": "MEX"}])]),
        ]
        with mock.patch("requests.get", side_effect=responses), \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False), \
             mock.patch.object(fm, "EXCLUDE_US_CONNECTIONS", False):
            offer = fm.search_cheapest(self.LEGS_ROUTE)
        assert offer["stops"] == 2  # 1 + 0 + 1
        assert offer["total_duration"] == 480 * 3  # each _flight() fixture is 480 min

    def test_legs_route_per_leg_stop_cap_still_applies(self):
        # Multi-leg routes no longer skip the 2+-stop cap — each leg is
        # searched via the normal one-way path, so a leg with only 2+-stop
        # candidates has no valid offer, and the whole trip can't be priced.
        too_many_stops = self._flight(3000, layovers=[{"id": "GDL"}, {"id": "MEX"}])
        responses = [
            self._make_response(best=[self._flight(1000)]),
            self._make_response(best=[too_many_stops]),
        ]
        with mock.patch("requests.get", side_effect=responses), \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False), \
             mock.patch.object(fm, "EXCLUDE_US_CONNECTIONS", False):
            offer = fm.search_cheapest(self.LEGS_ROUTE)
        assert offer is None

    def test_legs_route_none_if_any_leg_has_no_offers(self):
        responses = [
            self._make_response(best=[self._flight(1000)]),
            self._make_response(best=[]),  # leg 2: no offers at all
        ]
        with mock.patch("requests.get", side_effect=responses), \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False), \
             mock.patch.object(fm, "EXCLUDE_US_CONNECTIONS", False):
            offer = fm.search_cheapest(self.LEGS_ROUTE)
        assert offer is None

    def test_legs_route_teens_and_max_duration_apply_per_leg(self):
        route = {**self.LEGS_ROUTE, "adults": 2, "teens": 2, "max_duration_hours": 20}
        responses = [self._make_response(best=[self._flight(1000)]) for _ in range(3)]
        with mock.patch("requests.get", side_effect=responses) as mock_get, \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False), \
             mock.patch.object(fm, "EXCLUDE_US_CONNECTIONS", False):
            fm.search_cheapest(route)
        for call in mock_get.call_args_list:
            assert call.kwargs["params"]["adults"] == 4
            assert call.kwargs["params"]["max_duration"] == 1200


    def test_teens_folded_into_adults_param(self):
        route = {**self.ROUTE, "adults": 2, "teens": 2}
        resp = self._make_response(best=[self._flight(4000)])
        with mock.patch("requests.get", return_value=resp) as mock_get, \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False), \
             mock.patch.object(fm, "EXCLUDE_US_CONNECTIONS", False):
            fm.search_cheapest(route)
        assert mock_get.call_args[1]["params"]["adults"] == 4

    def test_no_teens_leaves_adults_unchanged(self):
        resp = self._make_response(best=[self._flight(4000)])
        with mock.patch("requests.get", return_value=resp) as mock_get, \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False), \
             mock.patch.object(fm, "EXCLUDE_US_CONNECTIONS", False):
            fm.search_cheapest(self.ROUTE)
        assert mock_get.call_args[1]["params"]["adults"] == self.ROUTE["adults"]

    def test_max_duration_hours_converted_to_minutes(self):
        route = {**self.ROUTE, "max_duration_hours": 24}
        resp = self._make_response(best=[self._flight(4000)])
        with mock.patch("requests.get", return_value=resp) as mock_get, \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False), \
             mock.patch.object(fm, "EXCLUDE_US_CONNECTIONS", False):
            fm.search_cheapest(route)
        assert mock_get.call_args[1]["params"]["max_duration"] == 1440

    def test_max_duration_hours_rounds_fractional(self):
        route = {**self.ROUTE, "max_duration_hours": 1.5}
        resp = self._make_response(best=[self._flight(4000)])
        with mock.patch("requests.get", return_value=resp) as mock_get, \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False), \
             mock.patch.object(fm, "EXCLUDE_US_CONNECTIONS", False):
            fm.search_cheapest(route)
        assert mock_get.call_args[1]["params"]["max_duration"] == 90

    def test_no_max_duration_param_when_absent(self):
        resp = self._make_response(best=[self._flight(4000)])
        with mock.patch("requests.get", return_value=resp) as mock_get, \
             mock.patch.object(fm, "ARCHIVE_RESPONSES", False), \
             mock.patch.object(fm, "EXCLUDE_US_CONNECTIONS", False):
            fm.search_cheapest(self.ROUTE)
        assert "max_duration" not in mock_get.call_args[1]["params"]


class TestRouteSearchCost:
    def test_simple_route_costs_one(self):
        assert fm.route_search_cost({"origin": "A", "destination": "B", "departure_date": "2026-01-01"}) == 1

    def test_legs_route_costs_leg_count(self):
        route = {"legs": [
            {"origin": "A", "destination": "B", "date": "2026-01-01"},
            {"origin": "B", "destination": "C", "date": "2026-01-05"},
            {"origin": "C", "destination": "A", "date": "2026-01-10"},
        ]}
        assert fm.route_search_cost(route) == 3


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

class TestSendTelegram:
    def test_disabled_when_no_token(self):
        with mock.patch.object(fm, "TELEGRAM_BOT_TOKEN", ""), \
             mock.patch("requests.post") as mock_post:
            fm.send_telegram("test")
        mock_post.assert_not_called()

    def test_sends_with_markdown(self):
        mock_resp = mock.Mock()
        mock_resp.json.return_value = {"ok": True}
        with mock.patch.object(fm, "TELEGRAM_BOT_TOKEN", "tok"), \
             mock.patch.object(fm, "TELEGRAM_CHAT_ID", "123"), \
             mock.patch("requests.post", return_value=mock_resp) as mock_post:
            fm.send_telegram("hello *bold*")
        payload = mock_post.call_args[1]["json"]
        assert payload["parse_mode"] == "Markdown"
        assert payload["text"] == f"📍 {fm.HOSTNAME}\nhello *bold*"

    def test_retries_without_markdown_on_parse_error(self):
        fail_resp = mock.Mock()
        fail_resp.json.return_value = {"ok": False, "description": "parse error"}
        ok_resp = mock.Mock()
        ok_resp.json.return_value = {"ok": True}
        with mock.patch.object(fm, "TELEGRAM_BOT_TOKEN", "tok"), \
             mock.patch.object(fm, "TELEGRAM_CHAT_ID", "123"), \
             mock.patch("requests.post", side_effect=[fail_resp, ok_resp]) as mock_post:
            fm.send_telegram("bad *markdown")
        assert mock_post.call_count == 2
        second_payload = mock_post.call_args_list[1][1]["json"]
        assert "parse_mode" not in second_payload


# ---------------------------------------------------------------------------
# _maybe_alert_quota
# ---------------------------------------------------------------------------

class TestMaybeAlertQuota:
    def setup_method(self):
        fm._QUOTA_ALERTED = False

    def test_429_triggers_alert(self):
        with mock.patch.object(fm, "send_telegram") as mock_tg:
            result = fm._maybe_alert_quota("YVR-CUN", 429, "Too many requests")
        assert result is True
        mock_tg.assert_called_once()

    def test_quota_message_triggers_alert(self):
        with mock.patch.object(fm, "send_telegram") as mock_tg:
            result = fm._maybe_alert_quota("YVR-CUN", 200, "You ran out of searches")
        assert result is True

    def test_normal_error_no_alert(self):
        with mock.patch.object(fm, "send_telegram") as mock_tg:
            result = fm._maybe_alert_quota("YVR-CUN", 500, "Internal error")
        assert result is False
        mock_tg.assert_not_called()

    def test_only_alerts_once_per_process(self):
        with mock.patch.object(fm, "send_telegram") as mock_tg:
            fm._maybe_alert_quota("A", 429, "")
            fm._maybe_alert_quota("B", 429, "")
        assert mock_tg.call_count == 1


# ---------------------------------------------------------------------------
# run_scan (mocked search_cheapest)
# ---------------------------------------------------------------------------

class TestRunScan:
    LEGS_ROUTE = {
        "legs": [
            {"origin": "JFK", "destination": "HEL", "date": "2026-09-15"},
            {"origin": "HEL", "destination": "BER", "date": "2026-09-20"},
            {"origin": "BER", "destination": "JFK", "date": "2026-09-25"},
        ],
        "adults": 1,
    }
    SIMPLE_ROUTE = {
        "origin": "YVR", "destination": "CUN",
        "departure_date": "2026-12-23", "return_date": "2026-12-30",
    }

    def _run(self, route, days, state_file):
        probes = []

        def fake_search(r):
            probes.append(r)
            return {"price": 1000.0}

        with mock.patch.object(fm, "load_routes", return_value=[route]), \
             mock.patch.object(fm, "sync_account_quota"), \
             mock.patch.object(fm, "can_make_calls", return_value=True), \
             mock.patch.object(fm, "search_cheapest", side_effect=fake_search), \
             mock.patch.object(fm, "send_telegram"):
            fm.run_scan(days)
        return probes, fm.load_json(state_file)

    def test_legs_route_shifts_every_leg_by_same_offset(self, state_file):
        probes, state = self._run(self.LEGS_ROUTE, 1, state_file)

        assert len(probes) == 3  # offsets -1, 0, +1
        shifted_back = next(p for p in probes if p["legs"][0]["date"] == "2026-09-14")
        assert [leg["date"] for leg in shifted_back["legs"]] == [
            "2026-09-14", "2026-09-19", "2026-09-24",
        ]
        shifted_fwd = next(p for p in probes if p["legs"][0]["date"] == "2026-09-16")
        assert [leg["date"] for leg in shifted_fwd["legs"]] == [
            "2026-09-16", "2026-09-21", "2026-09-26",
        ]

        label = fm.route_label(self.LEGS_ROUTE)
        assert state["flex_scans"][label]["base_date"] == "2026-09-15"

    def test_simple_route_shifts_departure_and_return_together(self, state_file):
        probes, state = self._run(self.SIMPLE_ROUTE, 1, state_file)

        assert len(probes) == 3
        shifted_back = next(p for p in probes if p["departure_date"] == "2026-12-22")
        assert shifted_back["return_date"] == "2026-12-29"

        label = fm.route_label(self.SIMPLE_ROUTE)
        assert state["flex_scans"][label]["base_date"] == "2026-12-23"

    def test_legs_route_call_count_reflects_leg_count(self, state_file):
        # 3 legs × 3 offsets (-1/0/+1) = 9 real searches, not 3 — run_scan must
        # use route_search_cost(), not a flat 1 per date, to track this.
        _, state = self._run(self.LEGS_ROUTE, 1, state_file)
        assert fm.get_call_count(state) == 9

    def test_simple_route_call_count_is_one_per_offset(self, state_file):
        _, state = self._run(self.SIMPLE_ROUTE, 1, state_file)
        assert fm.get_call_count(state) == 3
