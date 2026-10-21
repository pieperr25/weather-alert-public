import os
import json
import io
import unittest
from contextlib import redirect_stderr
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zoneinfo import ZoneInfo

from weather_alert import (
    AlertDecision,
    AlertEvaluation,
    Coordinates,
    ForecastHour,
    HTTP_MAX_RESPONSE_BYTES,
    OPEN_METEO_USER_AGENT,
    PointMetadata,
    SevereOutlook,
    SmtpConfig,
    WeatherAlertError,
    build_email_body,
    build_email_html,
    evaluate_due_alert,
    extract_tomorrow_window,
    fetch_json,
    fetch_open_meteo_hourly,
    get_due_hourly_forecast,
    load_coordinates,
    main,
    parse_meteo_france_vigilance,
    parse_spc_day2_outlook,
    parse_spc_day2_hazard,
    resolve_location,
    send_alert,
    should_run_at_local_hour,
    thresholds_trigger,
    validate_coordinate_freshness,
    wind_threshold_trigger,
)


ZONE = ZoneInfo("America/Chicago")
BASE = datetime(2026, 8, 9, 6, tzinfo=ZONE)


def hour(
    clock_hour,
    pop,
    description="Partly Cloudy",
    wind_speed=None,
    wind_gust=None,
    wind_direction=None,
):
    return ForecastHour(
        BASE.replace(hour=clock_hour),
        pop,
        description,
        wind_speed,
        wind_gust,
        wind_direction,
    )


def nws_period(
    clock_hour,
    pop,
    target_date=9,
    wind_speed="5 mph",
    wind_direction="SW",
    wind_gust=None,
):
    local = datetime(2026, 8, target_date, clock_hour, tzinfo=ZONE)
    return {
        "startTime": local.isoformat(),
        "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": pop},
        "shortForecast": "Scattered Thunderstorms",
        "windSpeed": wind_speed,
        "windDirection": wind_direction,
        **({"windGustMph": wind_gust} if wind_gust is not None else {}),
    }


class ThresholdTests(unittest.TestCase):
    def test_absolute_override(self):
        self.assertTrue(thresholds_trigger([hour(6, 50)]))

    def test_two_consecutive_moderate_hours(self):
        self.assertTrue(thresholds_trigger([hour(12, 30), hour(13, 49.9)]))

    def test_single_edge_hour_is_ignored(self):
        self.assertFalse(thresholds_trigger([hour(6, 45)]))
        self.assertFalse(thresholds_trigger([hour(19, 45)]))

    def test_single_interior_moderate_hour_does_not_trigger(self):
        self.assertFalse(thresholds_trigger([hour(14, 45)]))

    def test_separated_moderate_hours_do_not_trigger(self):
        self.assertFalse(thresholds_trigger([hour(10, 35), hour(12, 40)]))

    def test_below_threshold_does_not_trigger(self):
        self.assertFalse(thresholds_trigger([hour(10, 29.9), hour(11, 29.9)]))


class WindThresholdTests(unittest.TestCase):
    def test_threshold_is_inclusive_at_fifteen_mph(self):
        self.assertFalse(wind_threshold_trigger([hour(10, 0, wind_speed=14.9)]))
        self.assertTrue(wind_threshold_trigger([hour(10, 0, wind_speed=15.0)]))

    def test_wind_outside_local_window_does_not_trigger(self):
        self.assertFalse(wind_threshold_trigger([hour(5, 0, wind_speed=30)]))
        self.assertFalse(wind_threshold_trigger([hour(20, 0, wind_speed=30)]))

    def test_gust_does_not_independently_trigger(self):
        self.assertFalse(
            wind_threshold_trigger([hour(12, 0, wind_speed=12, wind_gust=35)])
        )

    def test_window_extracts_nws_wind_and_optional_gust(self):
        periods = [
            nws_period(clock_hour, 10, wind_speed="15 mph", wind_gust=24)
            for clock_hour in range(6, 20)
        ]
        _, hours = extract_tomorrow_window(
            periods,
            ZONE,
            datetime(2026, 8, 8, 23, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(hours[0].wind_speed_mph, 15)
        self.assertEqual(hours[0].wind_gust_mph, 24)
        self.assertEqual(hours[0].wind_direction, "Southwest")

    def test_native_wind_units_are_normalized_to_mph(self):
        periods = [
            nws_period(clock_hour, 10, wind_speed="24.1402 km/h")
            for clock_hour in range(6, 20)
        ]
        _, hours = extract_tomorrow_window(
            periods,
            ZONE,
            datetime(2026, 8, 8, 23, 30, tzinfo=timezone.utc),
        )
        self.assertAlmostEqual(hours[0].wind_speed_mph, 15.0, places=3)
        self.assertTrue(wind_threshold_trigger(hours))

    def test_missing_wind_does_not_suppress_rain(self):
        periods = [nws_period(clock_hour, 10) for clock_hour in range(6, 20)]
        for period in periods:
            period.pop("windSpeed")
            period.pop("windDirection")
        periods[4]["probabilityOfPrecipitation"]["value"] = 50
        _, hours = extract_tomorrow_window(
            periods,
            ZONE,
            datetime(2026, 8, 8, 23, 30, tzinfo=timezone.utc),
        )
        self.assertTrue(thresholds_trigger(hours))
        self.assertFalse(wind_threshold_trigger(hours))


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.periods = [nws_period(clock_hour, 10) for clock_hour in range(6, 20)]
        self.now = datetime(2026, 8, 8, 23, 30, tzinfo=timezone.utc)

    def test_extracts_inclusive_complete_local_window(self):
        target_date, result = extract_tomorrow_window(self.periods, ZONE, self.now)
        self.assertEqual(target_date.isoformat(), "2026-08-09")
        self.assertEqual([item.start.hour for item in result], list(range(6, 20)))

    def test_dynamic_local_hour_gate(self):
        instant = datetime(2026, 8, 8, 23, 30, tzinfo=timezone.utc)
        self.assertTrue(should_run_at_local_hour(instant, ZONE, 18))
        self.assertFalse(
            should_run_at_local_hour(instant, ZoneInfo("America/Denver"), 18)
        )

    def test_local_hour_gate_remains_open_after_configured_hour(self):
        instant = datetime(2026, 8, 9, 3, 30, tzinfo=timezone.utc)
        self.assertTrue(should_run_at_local_hour(instant, ZONE, 18))

    def test_incomplete_window_is_rejected(self):
        with self.assertRaises(WeatherAlertError):
            extract_tomorrow_window(self.periods[:-1], ZONE, self.now)

    def test_null_pop_is_rejected(self):
        self.periods[3]["probabilityOfPrecipitation"]["value"] = None
        with self.assertRaises(WeatherAlertError):
            extract_tomorrow_window(self.periods, ZONE, self.now)

    def test_uses_forecast_location_date_not_host_date(self):
        honolulu = ZoneInfo("Pacific/Honolulu")
        now = datetime(2026, 8, 9, 5, 30, tzinfo=timezone.utc)  # Still Aug 8 locally.
        periods = []
        for clock_hour in range(6, 20):
            local = datetime(2026, 8, 9, clock_hour, tzinfo=honolulu)
            periods.append(
                {
                    "startTime": local.isoformat(),
                    "probabilityOfPrecipitation": {"value": 10},
                    "shortForecast": "Sunny",
                }
            )
        target_date, _ = extract_tomorrow_window(periods, honolulu, now)
        self.assertEqual(target_date.isoformat(), "2026-08-09")


class InternationalProviderTests(unittest.TestCase):
    @staticmethod
    def open_meteo_payload():
        times = [f"2026-08-09T{hour:02d}:00" for hour in range(24)]
        return {
            "timezone": "Europe/Paris",
            "hourly": {
                "time": times,
                "precipitation_probability": [45] * len(times),
                "weather_code": [80] * len(times),
                "wind_speed_10m": [15] * len(times),
                "wind_gusts_10m": [24] * len(times),
                "wind_direction_10m": [225] * len(times),
            },
        }

    @patch("weather_alert.fetch_json")
    def test_open_meteo_is_normalized_to_existing_threshold_format(self, fetch):
        fetch.return_value = self.open_meteo_payload()

        zone, periods = fetch_open_meteo_hourly(Coordinates(48.8566, 2.3522))
        target_date, hours = extract_tomorrow_window(
            periods,
            zone,
            datetime(2026, 8, 8, 16, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(str(zone), "Europe/Paris")
        self.assertEqual(target_date.isoformat(), "2026-08-09")
        self.assertEqual(hours[0].pop, 45)
        self.assertEqual(hours[0].description, "Light Rain Showers")
        self.assertEqual(hours[0].wind_speed_mph, 15)
        self.assertEqual(hours[0].wind_gust_mph, 24)
        self.assertEqual(hours[0].wind_direction, "Southwest")
        self.assertTrue(thresholds_trigger(hours))
        self.assertIn("timezone=auto", fetch.call_args.args[0])
        self.assertEqual(fetch.call_args.args[1], OPEN_METEO_USER_AGENT)

    @patch("weather_alert.fetch_json")
    def test_open_meteo_rejects_excessive_hourly_periods(self, fetch):
        payload = self.open_meteo_payload()
        for key in (
            "time",
            "precipitation_probability",
            "weather_code",
            "wind_speed_10m",
            "wind_gusts_10m",
            "wind_direction_10m",
        ):
            payload["hourly"][key] = payload["hourly"][key] * 5
        fetch.return_value = payload

        with self.assertRaises(WeatherAlertError):
            fetch_open_meteo_hourly(Coordinates(48.8566, 2.3522))

    @patch("weather_alert.fetch_open_meteo_hourly")
    @patch("weather_alert.get_point_metadata")
    def test_non_nws_location_uses_worldwide_fallback(self, get_point, open_meteo):
        get_point.side_effect = WeatherAlertError("outside NWS coverage")
        open_meteo.return_value = (ZONE, [nws_period(hour, 10) for hour in range(6, 20)])

        result = get_due_hourly_forecast(
            Coordinates(48.8566, 2.3522),
            "weather-alert-test",
            datetime(2026, 8, 8, 23, 30, tzinfo=timezone.utc),
            18,
        )

        self.assertIsNotNone(result)
        open_meteo.assert_called_once()


class HttpSecurityTests(unittest.TestCase):
    @patch("weather_alert.urlopen")
    def test_rejects_response_larger_than_one_megabyte(self, urlopen):
        response = urlopen.return_value.__enter__.return_value
        response.headers.get_content_charset.return_value = "utf-8"
        response.read.return_value = b"x" * (HTTP_MAX_RESPONSE_BYTES + 1)

        with self.assertRaises(WeatherAlertError):
            fetch_json("https://api.example.test/forecast", "test-agent", attempts=1)

        response.read.assert_called_once_with(HTTP_MAX_RESPONSE_BYTES + 1)


class CoordinateTests(unittest.TestCase):
    def test_loads_shortcuts_coordinates_with_iso8601_timezone(self):
        # Shortcuts sends a JSON dictionary serialized into the GitHub
        # variable's text value, with Format Date set to ISO 8601.
        for timestamp in (
            "2026-08-08T18:15:00-05:00",
            "2026-08-09T01:15:00+02:00",
            "2026-08-08T23:15:00Z",
        ):
            with self.subTest(timestamp=timestamp), TemporaryDirectory() as directory:
                request_body = {
                    "name": "COORDS_JSON",
                    "value": json.dumps({
                        "latitude": 40.123,
                        "longitude": -100.456,
                        "updated_at": timestamp,
                    }),
                }
                path = Path(directory, "coords.json")
                path.write_text(request_body["value"], encoding="utf-8")
                coordinates = load_coordinates(path)
                self.assertEqual(coordinates.latitude, 40.123)
                self.assertEqual(coordinates.longitude, -100.456)
                self.assertEqual(
                    coordinates.updated_at,
                    datetime(2026, 8, 8, 23, 15, tzinfo=timezone.utc),
                )
                validate_coordinate_freshness(
                    coordinates,
                    datetime(2026, 8, 8, 23, 30, tzinfo=timezone.utc),
                    max_age_hours=3,
                )

    def test_loads_phone_timestamp_and_accepts_fresh_coordinates(self):
        with TemporaryDirectory() as directory:
            path = Path(directory, "coords.json")
            path.write_text(
                '{"latitude": 41.878, "longitude": -87.630, '
                '"updated_at": "2026-08-08T23:20:00Z"}',
                encoding="utf-8",
            )
            coordinates = load_coordinates(path)

        now = datetime(2026, 8, 8, 23, 30, tzinfo=timezone.utc)
        validate_coordinate_freshness(coordinates, now, 3)

    def test_rejects_stale_phone_coordinates(self):
        coordinates = Coordinates(
            41.878,
            -87.630,
            datetime.now(timezone.utc) - timedelta(hours=4),
        )
        with self.assertRaises(WeatherAlertError):
            validate_coordinate_freshness(
                coordinates, datetime.now(timezone.utc), max_age_hours=3
            )


def spc_payload(label, issue="2026-08-08T17:30:00+00:00"):
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "LABEL": label,
                    "ISSUE_ISO": issue,
                    "VALID_ISO": "2026-08-09T12:00:00+00:00",
                    "EXPIRE_ISO": "2026-08-10T12:00:00+00:00",
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [-89.0, 40.0],
                            [-86.0, 40.0],
                            [-86.0, 43.0],
                            [-89.0, 43.0],
                            [-89.0, 40.0],
                        ]
                    ],
                },
            }
        ],
    }


class SpcOutlookTests(unittest.TestCase):
    now = datetime(2026, 8, 8, 23, 30, tzinfo=timezone.utc)
    coordinates = Coordinates(41.878, -87.630)

    def test_marginal_through_high_trigger(self):
        for label in ("MRGL", "SLGT", "ENH", "MDT", "HIGH"):
            with self.subTest(label=label):
                outlook = parse_spc_day2_outlook(
                    spc_payload(label), self.coordinates, BASE.date(), self.now
                )
                self.assertIsNotNone(outlook)
                self.assertTrue(outlook.triggers)

    def test_general_thunder_does_not_trigger(self):
        outlook = parse_spc_day2_outlook(
            spc_payload("TSTM"), self.coordinates, BASE.date(), self.now
        )
        self.assertEqual(outlook.level, "General Thunder")
        self.assertFalse(outlook.triggers)

    def test_outside_all_polygons_does_not_trigger(self):
        outlook = parse_spc_day2_outlook(
            spc_payload("HIGH"), Coordinates(30, -100), BASE.date(), self.now
        )
        self.assertIsNone(outlook)

    def test_old_product_is_rejected(self):
        with self.assertRaises(WeatherAlertError):
            parse_spc_day2_outlook(
                spc_payload("MRGL", issue="2026-08-07T17:00:00+00:00"),
                self.coordinates,
                BASE.date(),
                self.now,
            )

    def test_hazard_risk_thresholds_and_hatching(self):
        self.assertEqual(
            parse_spc_day2_hazard(
                spc_payload("0.05"), "Wind", self.coordinates, BASE.date(), self.now
            ),
            "Wind",
        )
        self.assertEqual(
            parse_spc_day2_hazard(
                spc_payload("0.10"), "Hail", self.coordinates, BASE.date(), self.now
            ),
            "Hail (10%)",
        )
        self.assertEqual(
            parse_spc_day2_hazard(
                spc_payload("0.05"), "Tornado", self.coordinates, BASE.date(), self.now
            ),
            "Tornado (5%)",
        )
        hatched = spc_payload("0.02")
        hatched["features"][0]["properties"]["SIGN"] = "Significant"
        self.assertEqual(
            parse_spc_day2_hazard(
                hatched, "Tornado", self.coordinates, BASE.date(), self.now
            ),
            "Tornado (2% hatched)",
        )


def france_payload(hazard_id="3", color=2):
    return {
        "update_time": "2026-08-08T14:00:00+00:00",
        "end_validity_time": "2026-08-09T22:00:00+00:00",
        "subdomains_phenomenons_max_color": [
            {
                "domain_id": "75",
                "phenomenons_max_color": [
                    {
                        "phenomenon_id": hazard_id,
                        "phenomenon_max_color_id": color,
                    }
                ],
            }
        ],
    }


class FranceVigilanceTests(unittest.TestCase):
    now = datetime(2026, 8, 8, 16, 30, tzinfo=timezone.utc)
    target = date(2026, 8, 9)

    def test_yellow_selected_hazards_trigger(self):
        for hazard_id in ("1", "2", "3", "5"):
            with self.subTest(hazard_id=hazard_id):
                outlook = parse_meteo_france_vigilance(
                    france_payload(hazard_id, 2), "75", self.target, self.now
                )
                self.assertTrue(outlook.triggers)
                self.assertEqual(outlook.source, "Météo-France Vigilance (tomorrow)")

    def test_yellow_other_hazard_does_not_trigger_but_orange_does(self):
        yellow = parse_meteo_france_vigilance(
            france_payload("6", 2), "75", self.target, self.now
        )
        orange = parse_meteo_france_vigilance(
            france_payload("6", 3), "75", self.target, self.now
        )
        self.assertFalse(yellow.triggers)
        self.assertTrue(orange.triggers)


class EmailTests(unittest.TestCase):
    def test_body_omits_alert_metadata_and_keeps_wind_direction_only(self):
        hours = [
            hour(
                clock_hour,
                50 if clock_hour == 10 else 10,
                "Scattered Thunderstorms",
                wind_speed=15 + (clock_hour - 10) * 0.6,
                wind_gust=19 + clock_hour - 10,
                wind_direction="Southwest",
            )
            for clock_hour in range(10, 16)
        ]
        body = build_email_body(
            BASE.date(),
            Coordinates(41.878113, -87.629799),
            hours,
            AlertDecision(rain_trigger=True, wind_trigger=True),
        )
        self.assertTrue(body.startswith("10:00 - 50% - Thunderstorms"))
        self.assertIn("Wind Direction: Southwest", body)
        self.assertNotIn("Weather alert for", body)
        self.assertNotIn("Coordinates:", body)
        self.assertNotIn("Why this email was sent:", body)
        self.assertNotIn("Forecast window:", body)
        self.assertNotIn("mph", body)
        self.assertNotIn("Maximum gust:", body)
        self.assertTrue(body.endswith("Location:\nLocation unavailable\n"))

    def test_html_forecast_uses_icons_rounding_and_bold_high_rain(self):
        hours = [
            hour(10, 72, "Slight Chance Rain Showers"),
            hour(11, 78, "Mostly Sunny"),
            hour(12, 15, "Snow"),
            hour(13, 10, "Partly Cloudy"),
        ]
        html_body = build_email_html(
            BASE.date(), Coordinates(41.878113, -87.629799), hours
        )

        self.assertIn("🌧️", html_body)
        self.assertIn("☀️", html_body)
        self.assertIn("🌨️", html_body)
        self.assertIn("⛅", html_body)
        self.assertIn("70%", html_body)
        self.assertIn("80%", html_body)
        self.assertIn("Rain Showers", html_body)
        self.assertIn("Sunny", html_body)
        self.assertNotIn("Slight", html_body)
        self.assertNotIn("Mostly", html_body)
        self.assertIn('<tr style="font-weight: 700;">', html_body)
        self.assertIn("<strong>Location:</strong><br>Location unavailable", html_body)

    def test_severe_outlook_includes_only_level_and_specific_hazards(self):
        outlook = SevereOutlook(
            "SPC Day 2 Convective Outlook",
            "Marginal",
            BASE,
            BASE + timedelta(hours=6),
            BASE + timedelta(hours=30),
            ("Wind",),
            True,
        )
        body = build_email_body(
            BASE.date(),
            Coordinates(41.878113, -87.629799),
            [hour(10, 50, wind_speed=15, wind_direction="Southwest")],
            AlertDecision(rain_trigger=True, wind_trigger=True, severe_outlook=outlook),
        )

        self.assertIn("Level: Marginal", body)
        self.assertIn("Hazards: Wind", body)
        self.assertNotIn("Source:", body)
        self.assertNotIn("Issued:", body)
        self.assertNotIn("Valid:", body)

    def test_location_section_uses_supplied_name(self):
        body = build_email_body(
            BASE.date(),
            Coordinates(35.0, -100.0),
            [hour(10, 50)],
            location="Exampleville",
        )
        html_body = build_email_html(
            BASE.date(), Coordinates(35.0, -100.0), [hour(10, 50)], location="Test County"
        )

        self.assertTrue(body.endswith("Location:\nExampleville\n"))
        self.assertIn("<strong>Location:</strong><br>Test County", html_body)

    @patch("weather_alert.smtplib.SMTP")
    def test_starttls_smtp_delivery(self, smtp_class):
        smtp = smtp_class.return_value
        smtp.__enter__.return_value = smtp
        config = SmtpConfig(
            host="smtp.example.com",
            port=587,
            username="user",
            password="secret",
            sender="from@example.com",
            recipients=("to@example.com",),
            use_ssl=False,
            starttls=True,
            timeout_seconds=20,
        )

        send_alert(config, BASE.date(), "Test body\n", "<p>Test HTML body</p>")

        smtp_class.assert_called_once_with("smtp.example.com", 587, timeout=20)
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("user", "secret")
        smtp.send_message.assert_called_once()
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["Subject"], "Weather Alert - Sunday")
        self.assertEqual(
            message.get_body(preferencelist=("html",)).get_content(),
            "<p>Test HTML body</p>\n",
        )

    @patch("weather_alert.time.sleep")
    @patch("weather_alert.smtplib.SMTP")
    def test_transient_smtp_connection_error_retries(self, smtp_class, sleep):
        smtp = smtp_class.return_value
        smtp.__enter__.return_value = smtp
        smtp.starttls.side_effect = [TimeoutError("timed out"), None]
        config = SmtpConfig(
            host="smtp.example.com",
            port=587,
            username="user",
            password="secret",
            sender="from@example.com",
            recipients=("to@example.com",),
            use_ssl=False,
            starttls=True,
            timeout_seconds=20,
        )

        send_alert(config, BASE.date(), "Test body\n")

        self.assertEqual(smtp_class.call_count, 2)
        self.assertEqual(smtp.send_message.call_count, 1)
        self.assertTrue(smtp.send_message.call_args.args[0]["Message-ID"])
        sleep.assert_called_once_with(2)

    @patch("weather_alert.time.sleep")
    @patch("weather_alert.smtplib.SMTP")
    def test_timeout_during_message_transfer_does_not_duplicate(self, smtp_class, sleep):
        smtp = smtp_class.return_value
        smtp.__enter__.return_value = smtp
        smtp.send_message.side_effect = TimeoutError("timed out")
        config = SmtpConfig(
            host="smtp.example.com",
            port=587,
            username="user",
            password="secret",
            sender="from@example.com",
            recipients=("to@example.com",),
            use_ssl=False,
            starttls=True,
            timeout_seconds=60,
        )

        with self.assertRaisesRegex(WeatherAlertError, "result uncertain"):
            send_alert(config, BASE.date(), "Test body\n")

        smtp.send_message.assert_called_once()
        sleep.assert_not_called()


class LocationTests(unittest.TestCase):
    @patch("weather_alert.fetch_json")
    def test_city_name_comes_from_geocoder(self, fetch):
        fetch.return_value = {"address": {"city": "Example City"}}
        self.assertEqual(
            resolve_location(Coordinates(40.0, -100.0), "test-agent"),
            "Example City",
        )
        fetch.assert_called_once()

    @patch("weather_alert.fetch_json", side_effect=WeatherAlertError("offline"))
    def test_unavailable_geocoder_uses_neutral_fallback(self, fetch):
        errors = []
        self.assertEqual(
            resolve_location(Coordinates(40.0, -100.0), "test-agent", errors),
            "Location unavailable",
        )
        self.assertEqual(len(errors), 1)

    @patch("weather_alert.fetch_json")
    def test_reverse_geocode_prefers_town_then_falls_back_to_county(self, fetch):
        coordinates = Coordinates(35.0, -100.0)
        fetch.return_value = {"address": {"town": "Exampleville", "county": "Test County"}}
        self.assertEqual(resolve_location(coordinates, "test-agent"), "Exampleville")
        fetch.return_value = {"address": {"county": "Test County"}}
        self.assertEqual(resolve_location(coordinates, "test-agent"), "Test County")


class ReliabilityTests(unittest.TestCase):
    now = datetime(2026, 8, 8, 23, 30, tzinfo=timezone.utc)
    coordinates = Coordinates(41.878, -87.630)

    @patch("weather_alert.load_coordinates", side_effect=WeatherAlertError("bad coordinates"))
    def test_hosted_failure_reports_underlying_error(self, _load_coords):
        stderr = io.StringIO()
        with patch.dict(os.environ, {"WEATHER_ALERT_REPORT_ERRORS": "true"}), redirect_stderr(stderr):
            self.assertEqual(main(), 1)
        self.assertIn("WeatherAlertError: bad coordinates", stderr.getvalue())

    @patch("weather_alert._fetch_nws_periods_with_optional_gusts")
    @patch("weather_alert.get_point_metadata")
    def test_completed_local_date_suppresses_later_runs(
        self, get_point, fetch_periods
    ):
        get_point.return_value = PointMetadata(ZONE, "hourly", "grid")

        evaluation = evaluate_due_alert(
            self.coordinates,
            "test-agent",
            self.now + timedelta(hours=4),
            configured_hour=18,
            last_evaluated_local_date=date(2026, 8, 8),
        )

        self.assertIsNone(evaluation)
        fetch_periods.assert_not_called()

    @patch("weather_alert._fetch_nws_periods_with_optional_gusts")
    @patch("weather_alert.get_point_metadata")
    def test_stale_coordinates_do_not_fail_outside_configured_hour(
        self, get_point, fetch_periods
    ):
        get_point.return_value = PointMetadata(ZONE, "hourly", "grid")
        coordinates = Coordinates(
            41.878,
            -87.630,
            self.now - timedelta(hours=4),
        )

        evaluation = evaluate_due_alert(
            coordinates,
            "test-agent",
            self.now - timedelta(hours=1),
            configured_hour=18,
            max_coordinate_age_hours=3,
        )

        self.assertIsNone(evaluation)
        fetch_periods.assert_not_called()

    @patch("weather_alert._fetch_nws_periods_with_optional_gusts")
    @patch("weather_alert.get_point_metadata")
    def test_stale_coordinates_fail_during_configured_hour(
        self, get_point, fetch_periods
    ):
        get_point.return_value = PointMetadata(ZONE, "hourly", "grid")
        coordinates = Coordinates(
            41.878,
            -87.630,
            self.now - timedelta(hours=4),
        )

        with self.assertRaisesRegex(WeatherAlertError, "coordinates are stale"):
            evaluate_due_alert(
                coordinates,
                "test-agent",
                self.now,
                configured_hour=18,
                max_coordinate_age_hours=3,
            )

        fetch_periods.assert_not_called()

    @patch("weather_alert.fetch_spc_day2_outlook", return_value=None)
    @patch("weather_alert._fetch_nws_periods_with_optional_gusts")
    @patch("weather_alert.get_point_metadata")
    def test_stale_coordinates_use_last_known_location_when_limit_is_disabled(
        self, get_point, fetch_periods, _fetch_spc
    ):
        get_point.return_value = PointMetadata(ZONE, "hourly", "grid")
        fetch_periods.return_value = [
            nws_period(clock_hour, 10) for clock_hour in range(6, 20)
        ]
        stale_coordinates = Coordinates(
            41.878,
            -87.630,
            self.now - timedelta(days=7),
        )

        evaluation = evaluate_due_alert(
            stale_coordinates,
            "test-agent",
            self.now,
            configured_hour=18,
            max_coordinate_age_hours=None,
        )

        self.assertIsNotNone(evaluation)
        self.assertFalse(evaluation.decision.should_send)
        fetch_periods.assert_called_once()

    @patch("weather_alert.fetch_spc_day2_outlook")
    @patch("weather_alert._fetch_nws_periods_with_optional_gusts")
    @patch("weather_alert.get_point_metadata")
    def test_optional_spc_failure_does_not_suppress_rain(
        self, get_point, fetch_periods, fetch_spc
    ):
        get_point.return_value = PointMetadata(ZONE, "hourly", "grid")
        periods = [nws_period(clock_hour, 10) for clock_hour in range(6, 20)]
        periods[5]["probabilityOfPrecipitation"]["value"] = 50
        fetch_periods.return_value = periods
        fetch_spc.side_effect = WeatherAlertError("SPC unavailable")

        evaluation = evaluate_due_alert(
            self.coordinates, "test-agent", self.now, configured_hour=18
        )

        self.assertTrue(evaluation.decision.rain_trigger)
        self.assertTrue(evaluation.decision.should_send)
        self.assertIn("SPC severe-outlook API failed", evaluation.operational_errors[0])

    @patch("weather_alert.fetch_spc_day2_outlook")
    @patch("weather_alert.fetch_open_meteo_hourly")
    @patch("weather_alert._fetch_nws_periods_with_optional_gusts")
    @patch("weather_alert.get_point_metadata")
    def test_forecast_failure_does_not_suppress_valid_spc_trigger(
        self, get_point, fetch_periods, open_meteo, fetch_spc
    ):
        get_point.return_value = PointMetadata(ZONE, "hourly", "grid")
        fetch_periods.side_effect = WeatherAlertError("forecast unavailable")
        open_meteo.side_effect = WeatherAlertError("fallback unavailable")
        fetch_spc.return_value = SevereOutlook(
            "SPC Day 2 Convective Outlook",
            "Marginal",
            datetime(2026, 8, 8, 17, 30, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, tzinfo=timezone.utc),
            datetime(2026, 8, 10, 12, tzinfo=timezone.utc),
            ("Severe thunderstorms",),
            True,
        )

        evaluation = evaluate_due_alert(
            self.coordinates, "test-agent", self.now, configured_hour=18
        )

        self.assertEqual(evaluation.hours, ())
        self.assertTrue(evaluation.decision.severe_outlook_trigger)
        self.assertIn("regular forecast APIs failed", evaluation.operational_errors[0])

    @patch("weather_alert.fetch_meteo_france_vigilance")
    @patch("weather_alert.lookup_france_department", return_value="75")
    @patch("weather_alert.fetch_open_meteo_hourly")
    @patch("weather_alert.get_point_metadata")
    def test_france_uses_next_day_meteo_france_source(
        self, get_point, open_meteo, lookup, fetch_vigilance
    ):
        get_point.side_effect = WeatherAlertError("outside NWS")
        paris = ZoneInfo("Europe/Paris")
        periods = []
        for clock_hour in range(6, 20):
            local = datetime(2026, 8, 9, clock_hour, tzinfo=paris)
            periods.append(
                {
                    "startTime": local.isoformat(),
                    "probabilityOfPrecipitation": {"value": 0},
                    "shortForecast": "Clear",
                }
            )
        open_meteo.return_value = (paris, periods)
        fetch_vigilance.return_value = parse_meteo_france_vigilance(
            france_payload("3", 2),
            "75",
            date(2026, 8, 9),
            datetime(2026, 8, 8, 16, 30, tzinfo=timezone.utc),
        )

        evaluation = evaluate_due_alert(
            Coordinates(48.8566, 2.3522),
            "test-agent",
            datetime(2026, 8, 8, 16, 30, tzinfo=timezone.utc),
            configured_hour=18,
        )

        fetch_vigilance.assert_called_once()
        self.assertTrue(evaluation.decision.severe_outlook_trigger)

    @patch("weather_alert.send_alert")
    @patch("weather_alert.resolve_location", return_value="Exampleville")
    @patch("weather_alert.load_smtp_config")
    @patch("weather_alert.evaluate_due_alert")
    @patch("weather_alert.load_coordinates")
    def test_combined_conditions_send_only_one_email(
        self, load_coords, evaluate, load_smtp, resolve, send
    ):
        load_coords.return_value = self.coordinates
        outlook = SevereOutlook(
            "SPC Day 2 Convective Outlook",
            "Slight",
            self.now,
            self.now + timedelta(hours=12),
            self.now + timedelta(hours=36),
            ("Severe thunderstorms",),
            True,
        )
        evaluate.return_value = AlertEvaluation(
            BASE.date(),
            (hour(10, 50, wind_speed=16, wind_gust=24),),
            AlertDecision(True, True, outlook),
        )

        self.assertEqual(main(), 0)
        send.assert_called_once()

    @patch("weather_alert.send_alert")
    @patch("weather_alert.resolve_location", return_value="Exampleville")
    @patch("weather_alert.load_smtp_config")
    @patch("weather_alert.evaluate_due_alert")
    @patch("weather_alert.load_coordinates")
    def test_recovered_error_sends_weather_email_then_fails_for_notifier(
        self, load_coords, evaluate, load_smtp, resolve, send
    ):
        load_coords.return_value = self.coordinates
        evaluate.return_value = AlertEvaluation(
            BASE.date(),
            (hour(10, 50),),
            AlertDecision(True, False),
            ("secondary weather API failed",),
        )

        self.assertEqual(main(), 1)
        send.assert_called_once()

    @patch("weather_alert.send_alert")
    @patch("weather_alert.load_smtp_config")
    @patch("weather_alert.evaluate_due_alert")
    @patch("weather_alert.load_coordinates")
    def test_successful_no_alert_evaluation_writes_daily_marker(
        self, load_coords, evaluate, load_smtp, send
    ):
        load_coords.return_value = self.coordinates
        evaluate.return_value = AlertEvaluation(
            date(2026, 8, 9),
            (hour(10, 10),),
            AlertDecision(False, False),
        )

        with TemporaryDirectory() as directory:
            marker = Path(directory, "evaluation-date")
            with patch.dict(
                os.environ,
                {"WEATHER_ALERT_EVALUATION_DATE_FILE": str(marker)},
                clear=True,
            ):
                self.assertEqual(main(), 0)
            self.assertEqual(marker.read_text(encoding="utf-8"), "2026-08-08\n")

        load_smtp.assert_not_called()
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
