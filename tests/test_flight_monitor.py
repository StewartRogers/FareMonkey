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


class TestRouteRunsThisHour:
    def test_no_run_hours_always_runs(self):
        assert fm.route_runs_this_hour({"origin": "A"}, 7) is True

    def test_run_hours_matching(self):
        assert fm.route_runs_this_hour({"run_hours": [7, 13, 19]}, 13) is True

    def test_run_hours_not_matching(self):
        assert fm.route_runs_this_hour({"run_hours": [13]}, 7) is False


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
        state = {"api_calls": {fm.month_key(): 235}}
        with mock.patch.object(fm, "MONTHLY_CALL_CAP", 240):
            assert fm.can_make_calls(state, 5) is True
            assert fm.can_make_calls(state, 6) is False

    def test_can_make_calls_empty_state(self):
        with mock.patch.object(fm, "MONTHLY_CALL_CAP", 240):
            assert fm.can_make_calls({}, 240) is True
            assert fm.can_make_calls({}, 241) is False


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
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"plan_searches_left": 150}
        with mock.patch("requests.get", return_value=mock_resp):
            result = fm.sync_account_quota()
        assert result == 150
        assert fm._searches_left == 150

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
        msg = fm.format_telegram(self.ROUTE, self.OFFER, "✈️", -5.2)
        assert "CAD 4,373 (↓5.2%)" in msg

    def test_price_rise(self):
        msg = fm.format_telegram(self.ROUTE, self.OFFER, "⚠️", 3.8)
        assert "(↑3.8%)" in msg

    def test_baseline_no_arrow(self):
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


# ---------------------------------------------------------------------------
# Archive / trim
# ---------------------------------------------------------------------------

class TestArchiveResponse:
    def test_writes_jsonl(self, responses_file):
        with mock.patch.object(fm, "ARCHIVE_RESPONSES", True):
            fm.archive_response(
                {"origin": "YVR", "destination": "CUN"},
                {"api_key": "SECRET", "departure_id": "YVR"},
                {"best_flights": []},
            )
        lines = responses_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert "api_key" not in record["query"]
        assert record["route"] == "YVR-CUN"

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
        assert payload["text"] == "hello *bold*"

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
