#!/usr/bin/env python3
"""Send one next-day weather alert when rain, wind, or outlook rules trigger.

This program is intended for unattended cron execution. Set
WEATHER_ALERT_REPORT_ERRORS=true to report failures to stderr (for hosted
Actions), or WEATHER_ALERT_LOG_FILE to record them in a file.
"""

from __future__ import annotations

import html
import json
import math
import os
import re
import smtplib
import socket
import ssl
import time
import traceback
from codecs import decode as codec_decode
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


NWS_POINTS_URL = "https://api.weather.gov/points/{latitude},{longitude}"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
SPC_DAY2_URL = "https://www.spc.noaa.gov/products/outlook/day2otlk_cat.nolyr.geojson"
SPC_DAY2_HAZARD_URLS = {
    "Tornado": "https://www.spc.noaa.gov/products/outlook/day2otlk_torn.nolyr.geojson",
    "Hail": "https://www.spc.noaa.gov/products/outlook/day2otlk_hail.nolyr.geojson",
    "Wind": "https://www.spc.noaa.gov/products/outlook/day2otlk_wind.nolyr.geojson",
}
METEO_FRANCE_SITE_URL = "https://vigilance.meteofrance.fr/fr"
METEO_FRANCE_VIGILANCE_URL = (
    "https://rwg.meteofrance.com/wsft/v3/warning/currentphenomenons"
)
FRANCE_REVERSE_GEOCODE_URL = "https://api-adresse.data.gouv.fr/reverse/"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
WINDOW_START_HOUR = 6
WINDOW_END_HOUR = 19
WIND_TRIGGER_MPH = 15.0
HTTP_TIMEOUT_SECONDS = 15.0
HTTP_ATTEMPTS = 3
HTTP_MAX_RESPONSE_BYTES = 1_048_576
OPEN_METEO_MAX_HOURLY_PERIODS = 96
OPEN_METEO_USER_AGENT = "daily-weather-alert/1.0"
SEVERE_PRODUCT_MAX_AGE = timedelta(hours=24)


class WeatherAlertError(Exception):
    """Expected operational or data error."""


@dataclass(frozen=True)
class Coordinates:
    latitude: float
    longitude: float
    updated_at: datetime | None = None

    @property
    def display(self) -> str:
        return f"{self.latitude:.6f}, {self.longitude:.6f}"


@dataclass(frozen=True)
class ForecastHour:
    start: datetime
    pop: float
    description: str
    wind_speed_mph: float | None = None
    wind_gust_mph: float | None = None
    wind_direction: str | None = None


@dataclass(frozen=True)
class SevereOutlook:
    source: str
    level: str
    issued_at: datetime
    valid_from: datetime
    valid_to: datetime
    hazards: tuple[str, ...] = ()
    triggers: bool = False


@dataclass(frozen=True)
class AlertDecision:
    rain_trigger: bool
    wind_trigger: bool
    severe_outlook: SevereOutlook | None = None

    @property
    def severe_outlook_trigger(self) -> bool:
        return bool(self.severe_outlook and self.severe_outlook.triggers)

    @property
    def should_send(self) -> bool:
        return self.rain_trigger or self.wind_trigger or self.severe_outlook_trigger


@dataclass(frozen=True)
class AlertEvaluation:
    target_date: date
    hours: tuple[ForecastHour, ...]
    decision: AlertDecision
    operational_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    username: str | None
    password: str | None
    sender: str
    recipients: tuple[str, ...]
    use_ssl: bool
    starttls: bool
    timeout_seconds: float


@dataclass(frozen=True)
class PointMetadata:
    location_timezone: ZoneInfo
    forecast_url: str
    forecast_grid_data_url: str = ""


WMO_WEATHER_DESCRIPTIONS = {
    0: "Clear Sky",
    1: "Mainly Clear",
    2: "Partly Cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Rime Fog",
    51: "Light Drizzle",
    53: "Drizzle",
    55: "Heavy Drizzle",
    56: "Light Freezing Drizzle",
    57: "Heavy Freezing Drizzle",
    61: "Light Rain",
    63: "Rain",
    65: "Heavy Rain",
    66: "Light Freezing Rain",
    67: "Heavy Freezing Rain",
    71: "Light Snow",
    73: "Snow",
    75: "Heavy Snow",
    77: "Snow Grains",
    80: "Light Rain Showers",
    81: "Rain Showers",
    82: "Heavy Rain Showers",
    85: "Light Snow Showers",
    86: "Heavy Snow Showers",
    95: "Thunderstorms",
    96: "Thunderstorms with Light Hail",
    99: "Thunderstorms with Heavy Hail",
}


def _required_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WeatherAlertError(f"{context} is not a JSON object")
    return value


def _finite_number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WeatherAlertError(f"{context} is not numeric")
    number = float(value)
    if not math.isfinite(number):
        raise WeatherAlertError(f"{context} is not finite")
    return number


def _parse_aware_datetime(value: Any, context: str) -> datetime:
    if isinstance(value, bool):
        raise WeatherAlertError(f"{context} is not a valid timestamp")
    if isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise WeatherAlertError(f"{context} is not a valid Unix timestamp") from exc
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise WeatherAlertError(f"{context} is not a valid ISO-8601 timestamp") from exc
    else:
        raise WeatherAlertError(f"{context} is not a timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WeatherAlertError(f"{context} has no UTC offset")
    return parsed


def load_coordinates(path: Path, attempts: int = 3) -> Coordinates:
    """Read coordinates, retrying briefly in case a webhook is replacing them."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with path.open("r", encoding="utf-8") as handle:
                document = _required_mapping(json.load(handle), "coords.json")
            latitude = _finite_number(document.get("latitude"), "latitude")
            longitude = _finite_number(document.get("longitude"), "longitude")
            if not -90.0 <= latitude <= 90.0:
                raise WeatherAlertError("latitude is outside -90..90")
            if not -180.0 <= longitude <= 180.0:
                raise WeatherAlertError("longitude is outside -180..180")
            updated_at_value = document.get("updated_at")
            updated_at = (
                _parse_aware_datetime(updated_at_value, "updated_at")
                if updated_at_value is not None
                else None
            )
            return Coordinates(latitude, longitude, updated_at)
        except (OSError, json.JSONDecodeError, WeatherAlertError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.2 * (attempt + 1))
    raise WeatherAlertError(f"could not read valid coordinates from {path}") from last_error


def _coordinate_for_url(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def fetch_http(
    url: str,
    user_agent: str,
    *,
    accept: str = "application/geo+json",
    headers: Mapping[str, str] | None = None,
    attempts: int = HTTP_ATTEMPTS,
    timeout_seconds: float = HTTP_TIMEOUT_SECONDS,
) -> tuple[bytes, Mapping[str, str]]:
    """Fetch a bounded response, retrying timeouts, rate limits, and server errors."""
    request_headers = {
        "Accept": accept,
        "User-Agent": user_agent,
    }
    if headers:
        request_headers.update(headers)
    request = Request(
        url,
        headers=request_headers,
    )
    last_error: Exception | None = None

    for attempt in range(attempts):
        retry_delay = float(2**attempt)
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                encoding = response.headers.get_content_charset() or "utf-8"
                raw_payload = response.read(HTTP_MAX_RESPONSE_BYTES + 1)
                if len(raw_payload) > HTTP_MAX_RESPONSE_BYTES:
                    raise WeatherAlertError("weather response exceeds size limit")
                response_headers = {
                    key.lower(): value for key, value in response.headers.items()
                }
                response_headers["content-encoding"] = encoding
            return raw_payload, response_headers
        except HTTPError as exc:
            last_error = exc
            if exc.code != 429 and not 500 <= exc.code <= 599:
                break
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            if retry_after and retry_after.isdigit():
                retry_delay = min(float(retry_after), 30.0)
        except (URLError, TimeoutError, socket.timeout, json.JSONDecodeError, OSError) as exc:
            last_error = exc

        if attempt + 1 < attempts:
            time.sleep(retry_delay)

    raise WeatherAlertError(
        f"weather API request failed after {attempts} attempt(s): {url}"
    ) from last_error


def fetch_json(
    url: str,
    user_agent: str,
    *,
    headers: Mapping[str, str] | None = None,
    attempts: int = HTTP_ATTEMPTS,
    timeout_seconds: float = HTTP_TIMEOUT_SECONDS,
) -> Mapping[str, Any]:
    """Fetch a JSON object from a bounded HTTP response."""
    raw_payload, response_headers = fetch_http(
        url,
        user_agent,
        headers=headers,
        attempts=attempts,
        timeout_seconds=timeout_seconds,
    )
    try:
        payload = json.loads(
            raw_payload.decode(response_headers.get("content-encoding", "utf-8"))
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WeatherAlertError(f"response from {url} is not valid JSON") from exc
    return _required_mapping(payload, f"response from {url}")


def get_point_metadata(coordinates: Coordinates, user_agent: str) -> PointMetadata:
    points_url = NWS_POINTS_URL.format(
        latitude=quote(_coordinate_for_url(coordinates.latitude), safe="-"),
        longitude=quote(_coordinate_for_url(coordinates.longitude), safe="-"),
    )
    point_payload = fetch_json(points_url, user_agent)
    point_properties = _required_mapping(point_payload.get("properties"), "point properties")

    forecast_url = point_properties.get("forecastHourly")
    forecast_grid_data_url = point_properties.get("forecastGridData")
    timezone_name = point_properties.get("timeZone")
    if not isinstance(forecast_url, str) or not forecast_url.startswith("https://api.weather.gov/"):
        raise WeatherAlertError("point response has no valid NWS hourly forecast URL")
    if not isinstance(forecast_grid_data_url, str) or not forecast_grid_data_url.startswith(
        "https://api.weather.gov/"
    ):
        raise WeatherAlertError("point response has no valid NWS grid forecast URL")
    if not isinstance(timezone_name, str) or not timezone_name:
        raise WeatherAlertError("point response has no time zone")
    try:
        location_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise WeatherAlertError(f"unknown NWS time zone: {timezone_name}") from exc

    return PointMetadata(location_timezone, forecast_url, forecast_grid_data_url)


def fetch_hourly_periods(
    forecast_url: str, user_agent: str
) -> Sequence[Mapping[str, Any]]:
    forecast_payload = fetch_json(forecast_url, user_agent)
    forecast_properties = _required_mapping(
        forecast_payload.get("properties"), "hourly forecast properties"
    )
    periods = forecast_properties.get("periods")
    if not isinstance(periods, list) or not periods:
        raise WeatherAlertError("hourly forecast contains no periods")
    for period in periods:
        _required_mapping(period, "hourly forecast period")
    return periods


def _speed_to_mph(value: float, unit: str) -> float:
    normalized = unit.strip().lower()
    if normalized in {"mph", "mi_h-1", "wmounit:mi_h-1"}:
        return value
    if normalized in {"km/h", "kmh", "km_h-1", "wmounit:km_h-1"}:
        return value * 0.621371192237334
    if normalized in {"m/s", "m_s-1", "wmounit:m_s-1"}:
        return value * 2.2369362920544
    if normalized in {"kn", "kt", "knot", "knots", "wmounit:kt"}:
        return value * 1.1507794480235
    raise WeatherAlertError(f"unsupported wind speed unit: {unit}")


def _parse_iso_duration(value: str) -> timedelta:
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?",
        value,
    )
    if not match:
        raise WeatherAlertError(f"unsupported NWS validTime duration: {value}")
    duration = timedelta(
        days=int(match.group("days") or 0),
        hours=int(match.group("hours") or 0),
        minutes=int(match.group("minutes") or 0),
    )
    if duration <= timedelta(0):
        raise WeatherAlertError("NWS validTime duration is not positive")
    return duration


def _parse_nws_grid_values(
    grid_property: Any, context: str
) -> list[tuple[datetime, datetime, float]]:
    item = _required_mapping(grid_property, context)
    unit = item.get("uom")
    values = item.get("values")
    if not isinstance(unit, str) or not isinstance(values, list):
        raise WeatherAlertError(f"{context} has no unit or values")
    result: list[tuple[datetime, datetime, float]] = []
    for index, raw_item in enumerate(values):
        value_item = _required_mapping(raw_item, f"{context}.values[{index}]")
        valid_time = value_item.get("validTime")
        if not isinstance(valid_time, str) or "/" not in valid_time:
            raise WeatherAlertError(f"{context}.values[{index}] has invalid validTime")
        if value_item.get("value") is None:
            continue
        start_text, duration_text = valid_time.split("/", 1)
        start = _parse_aware_datetime(start_text, f"{context}.values[{index}].validTime")
        speed = _finite_number(value_item.get("value"), f"{context}.values[{index}].value")
        if speed < 0:
            raise WeatherAlertError(f"{context}.values[{index}] is negative")
        result.append((start, start + _parse_iso_duration(duration_text), _speed_to_mph(speed, unit)))
    return result


def fetch_nws_wind_gusts(
    forecast_grid_data_url: str, user_agent: str
) -> list[tuple[datetime, datetime, float]]:
    payload = fetch_json(forecast_grid_data_url, user_agent)
    properties = _required_mapping(payload.get("properties"), "NWS grid properties")
    return _parse_nws_grid_values(properties.get("windGust"), "NWS windGust")


def add_nws_wind_gusts(
    periods: Sequence[Mapping[str, Any]],
    gusts: Sequence[tuple[datetime, datetime, float]],
) -> Sequence[Mapping[str, Any]]:
    """Return copied hourly periods with the applicable normalized gust attached."""
    normalized: list[Mapping[str, Any]] = []
    for period in periods:
        start = _parse_nws_datetime(period.get("startTime"))
        matching = [value for begin, end, value in gusts if begin <= start < end]
        copied = dict(period)
        if matching:
            copied["windGustMph"] = max(matching)
        normalized.append(copied)
    return normalized


def get_hourly_forecast(
    coordinates: Coordinates, user_agent: str
) -> tuple[ZoneInfo, Sequence[Mapping[str, Any]]]:
    """Use NWS when available, then fall back to Open-Meteo worldwide."""
    try:
        metadata = get_point_metadata(coordinates, user_agent)
        periods = fetch_hourly_periods(metadata.forecast_url, user_agent)
        if metadata.forecast_grid_data_url:
            try:
                periods = add_nws_wind_gusts(
                    periods,
                    fetch_nws_wind_gusts(metadata.forecast_grid_data_url, user_agent),
                )
            except WeatherAlertError:
                pass
        return metadata.location_timezone, periods
    except WeatherAlertError:
        try:
            return fetch_open_meteo_hourly(coordinates)
        except WeatherAlertError as fallback_error:
            raise WeatherAlertError(
                "both NWS and Open-Meteo forecast requests failed"
            ) from fallback_error


def _parallel_list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise WeatherAlertError(f"{context} is not a non-empty list")
    return value


def _optional_parallel_list(value: Any, length: int) -> list[Any]:
    if not isinstance(value, list) or len(value) != length:
        return [None] * length
    return value


def fetch_open_meteo_hourly(
    coordinates: Coordinates,
) -> tuple[ZoneInfo, Sequence[Mapping[str, Any]]]:
    """Fetch a worldwide forecast and normalize it to the NWS period shape.

    Open-Meteo's automatic model selection incorporates high-resolution
    Météo-France AROME/ARPEGE data in France and suitable national/global
    models elsewhere.
    """
    query = urlencode(
        {
            "latitude": _coordinate_for_url(coordinates.latitude),
            "longitude": _coordinate_for_url(coordinates.longitude),
            "hourly": (
                "precipitation_probability,weather_code,wind_speed_10m,"
                "wind_gusts_10m,wind_direction_10m"
            ),
            "wind_speed_unit": "mph",
            "timezone": "auto",
            "forecast_days": "3",
        }
    )
    payload = fetch_json(f"{OPEN_METEO_URL}?{query}", OPEN_METEO_USER_AGENT)

    timezone_name = payload.get("timezone")
    if not isinstance(timezone_name, str) or not timezone_name:
        raise WeatherAlertError("Open-Meteo response has no time zone")
    try:
        location_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise WeatherAlertError(
            f"unknown Open-Meteo time zone: {timezone_name}"
        ) from exc

    hourly = _required_mapping(payload.get("hourly"), "Open-Meteo hourly data")
    times = _parallel_list(hourly.get("time"), "Open-Meteo hourly time")
    pops = _parallel_list(
        hourly.get("precipitation_probability"),
        "Open-Meteo hourly precipitation_probability",
    )
    codes = _parallel_list(hourly.get("weather_code"), "Open-Meteo hourly weather_code")
    wind_speeds = _optional_parallel_list(hourly.get("wind_speed_10m"), len(times))
    wind_gusts = _optional_parallel_list(hourly.get("wind_gusts_10m"), len(times))
    wind_directions = _optional_parallel_list(
        hourly.get("wind_direction_10m"), len(times)
    )
    if not len(times) == len(pops) == len(codes):
        raise WeatherAlertError("Open-Meteo hourly arrays have different lengths")
    if len(times) > OPEN_METEO_MAX_HOURLY_PERIODS:
        raise WeatherAlertError("Open-Meteo hourly forecast exceeds period limit")

    periods: list[Mapping[str, Any]] = []
    for index, values in enumerate(
        zip(times, pops, codes, wind_speeds, wind_gusts, wind_directions)
    ):
        time_value, pop_value, code_value, wind_value, gust_value, direction_value = values
        if not isinstance(time_value, str):
            raise WeatherAlertError(f"Open-Meteo time[{index}] is not a string")
        try:
            local_start = datetime.fromisoformat(time_value)
        except ValueError as exc:
            raise WeatherAlertError(
                f"Open-Meteo time[{index}] is not ISO-8601"
            ) from exc
        if local_start.tzinfo is not None:
            local_start = local_start.astimezone(location_timezone)
        else:
            local_start = local_start.replace(tzinfo=location_timezone)

        pop = _finite_number(pop_value, f"Open-Meteo precipitation_probability[{index}]")
        code_number = _finite_number(code_value, f"Open-Meteo weather_code[{index}]")
        if not code_number.is_integer() or int(code_number) not in WMO_WEATHER_DESCRIPTIONS:
            raise WeatherAlertError(f"unknown Open-Meteo weather code: {code_value}")
        try:
            wind_speed = _finite_number(
                wind_value, f"Open-Meteo wind_speed_10m[{index}]"
            )
            if wind_speed < 0:
                raise WeatherAlertError("negative wind speed")
        except WeatherAlertError:
            wind_speed = None
        try:
            wind_gust = _finite_number(
                gust_value, f"Open-Meteo wind_gusts_10m[{index}]"
            )
            if wind_gust < 0:
                raise WeatherAlertError("negative wind gust")
        except WeatherAlertError:
            wind_gust = None
        try:
            wind_direction = _finite_number(
                direction_value, f"Open-Meteo wind_direction_10m[{index}]"
            )
            if not 0 <= wind_direction <= 360:
                raise WeatherAlertError("invalid wind direction")
        except WeatherAlertError:
            wind_direction = None
        periods.append(
            {
                "startTime": local_start.isoformat(),
                "probabilityOfPrecipitation": {"value": pop},
                "shortForecast": WMO_WEATHER_DESCRIPTIONS[int(code_number)],
                **({"windSpeedMph": wind_speed} if wind_speed is not None else {}),
                **({"windGustMph": wind_gust} if wind_gust is not None else {}),
                **(
                    {"windDirectionDegrees": wind_direction}
                    if wind_direction is not None
                    else {}
                ),
            }
        )
    return location_timezone, periods


def get_due_hourly_forecast(
    coordinates: Coordinates,
    user_agent: str,
    now: datetime,
    configured_hour: int | None,
) -> tuple[ZoneInfo, Sequence[Mapping[str, Any]]] | None:
    """Resolve the provider and avoid downloading NWS hourly data until due."""
    try:
        metadata = get_point_metadata(coordinates, user_agent)
    except WeatherAlertError:
        location_timezone, periods = fetch_open_meteo_hourly(coordinates)
    else:
        if not should_run_at_local_hour(now, metadata.location_timezone, configured_hour):
            return None
        try:
            periods = fetch_hourly_periods(metadata.forecast_url, user_agent)
            if metadata.forecast_grid_data_url:
                try:
                    periods = add_nws_wind_gusts(
                        periods,
                        fetch_nws_wind_gusts(
                            metadata.forecast_grid_data_url, user_agent
                        ),
                    )
                except WeatherAlertError:
                    pass
            return metadata.location_timezone, periods
        except WeatherAlertError:
            location_timezone, periods = fetch_open_meteo_hourly(coordinates)

    if not should_run_at_local_hour(now, location_timezone, configured_hour):
        return None
    return location_timezone, periods


def _parse_nws_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise WeatherAlertError("forecast startTime is not a string")
    return _parse_aware_datetime(value, "forecast startTime")


def validate_coordinate_freshness(
    coordinates: Coordinates, now: datetime, max_age_hours: float | None
) -> None:
    if max_age_hours is None:
        return
    if max_age_hours <= 0:
        raise WeatherAlertError("MAX_COORDINATE_AGE_HOURS must be positive")
    if coordinates.updated_at is None:
        raise WeatherAlertError("coordinates have no updated_at timestamp")
    age = now.astimezone(timezone.utc) - coordinates.updated_at.astimezone(timezone.utc)
    if age < -timedelta(minutes=5):
        raise WeatherAlertError("coordinate timestamp is in the future")
    if age > timedelta(hours=max_age_hours):
        raise WeatherAlertError("coordinates are stale")


def should_run_at_local_hour(
    now: datetime, location_timezone: ZoneInfo, configured_hour: int | None
) -> bool:
    """Return whether the local clock has reached today's evaluation window."""
    if configured_hour is None:
        return True
    if not 0 <= configured_hour <= 23:
        raise WeatherAlertError("RUN_ONLY_AT_LOCAL_HOUR must be between 0 and 23")
    return now.astimezone(location_timezone).hour >= configured_hour


def _parse_pop(period: Mapping[str, Any]) -> float:
    pop_object = _required_mapping(
        period.get("probabilityOfPrecipitation"), "probabilityOfPrecipitation"
    )
    pop = _finite_number(pop_object.get("value"), "probabilityOfPrecipitation.value")
    if not 0.0 <= pop <= 100.0:
        raise WeatherAlertError("probability of precipitation is outside 0..100")
    return pop


COMPASS_DIRECTIONS = (
    "North",
    "North-Northeast",
    "Northeast",
    "East-Northeast",
    "East",
    "East-Southeast",
    "Southeast",
    "South-Southeast",
    "South",
    "South-Southwest",
    "Southwest",
    "West-Southwest",
    "West",
    "West-Northwest",
    "Northwest",
    "North-Northwest",
)
COMPASS_ABBREVIATIONS = {
    abbreviation: name
    for abbreviation, name in zip(
        ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"),
        COMPASS_DIRECTIONS,
    )
}


def _direction_from_degrees(value: float) -> str:
    return COMPASS_DIRECTIONS[int((value % 360 + 11.25) // 22.5) % 16]


def _parse_optional_wind_speed(period: Mapping[str, Any]) -> float | None:
    normalized = period.get("windSpeedMph")
    if normalized is not None:
        try:
            value = _finite_number(normalized, "windSpeedMph")
            return value if value >= 0 else None
        except WeatherAlertError:
            return None

    raw = period.get("windSpeed")
    if not isinstance(raw, str):
        return None
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)(?:\s+to\s+(\d+(?:\.\d+)?))?\s*(mph|km/h|m/s|kt|knots?)\s*",
        raw,
        re.IGNORECASE,
    )
    if not match:
        return None
    values = [float(match.group(1))]
    if match.group(2):
        values.append(float(match.group(2)))
    try:
        return _speed_to_mph(max(values), match.group(3))
    except WeatherAlertError:
        return None


def _parse_optional_wind_gust(period: Mapping[str, Any]) -> float | None:
    normalized = period.get("windGustMph")
    if normalized is not None:
        try:
            value = _finite_number(normalized, "windGustMph")
            return value if value >= 0 else None
        except WeatherAlertError:
            return None

    raw = period.get("windGust")
    if not isinstance(raw, Mapping):
        return None
    unit = raw.get("unitCode")
    value = raw.get("maxValue", raw.get("value"))
    if not isinstance(unit, str):
        return None
    try:
        speed = _finite_number(value, "windGust.value")
        return _speed_to_mph(speed, unit) if speed >= 0 else None
    except WeatherAlertError:
        return None


def _parse_optional_wind_direction(period: Mapping[str, Any]) -> str | None:
    degrees = period.get("windDirectionDegrees")
    if degrees is not None:
        try:
            value = _finite_number(degrees, "windDirectionDegrees")
            return _direction_from_degrees(value) if 0 <= value <= 360 else None
        except WeatherAlertError:
            return None
    raw = period.get("windDirection")
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().upper()
    if normalized in {"VRB", "VARIABLE"}:
        return "Variable"
    return COMPASS_ABBREVIATIONS.get(normalized)


def extract_tomorrow_window(
    periods: Sequence[Mapping[str, Any]],
    location_timezone: ZoneInfo,
    now: datetime | None = None,
) -> tuple[date, list[ForecastHour]]:
    """Return all hourly periods from 06:00 through 19:00 local, inclusive."""
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise WeatherAlertError("current time must be timezone-aware")
    target_date = instant.astimezone(location_timezone).date() + timedelta(days=1)
    hours_by_clock_hour: dict[int, ForecastHour] = {}

    for period in periods:
        start = _parse_nws_datetime(period.get("startTime")).astimezone(location_timezone)
        if start.date() != target_date or not WINDOW_START_HOUR <= start.hour <= WINDOW_END_HOUR:
            continue
        if start.minute != 0 or start.second != 0 or start.microsecond != 0:
            raise WeatherAlertError("forecast period is not aligned to an hour")
        if start.hour in hours_by_clock_hour:
            raise WeatherAlertError(f"duplicate forecast period for local hour {start.hour:02d}:00")
        description = period.get("shortForecast")
        if not isinstance(description, str) or not description.strip():
            raise WeatherAlertError("forecast period has no shortForecast description")
        hours_by_clock_hour[start.hour] = ForecastHour(
            start=start,
            pop=_parse_pop(period),
            description=" ".join(description.split()),
            wind_speed_mph=_parse_optional_wind_speed(period),
            wind_gust_mph=_parse_optional_wind_gust(period),
            wind_direction=_parse_optional_wind_direction(period),
        )

    expected_hours = set(range(WINDOW_START_HOUR, WINDOW_END_HOUR + 1))
    if set(hours_by_clock_hour) != expected_hours:
        missing = sorted(expected_hours - set(hours_by_clock_hour))
        missing_text = ", ".join(f"{hour:02d}:00" for hour in missing)
        raise WeatherAlertError(f"hourly forecast window is incomplete; missing {missing_text}")

    return target_date, [hours_by_clock_hour[hour] for hour in sorted(hours_by_clock_hour)]


def thresholds_trigger(hours: Sequence[ForecastHour]) -> bool:
    """Apply the absolute and consecutive-hour precipitation rules."""
    if any(hour.pop >= 50.0 for hour in hours):
        return True

    moderate = [hour for hour in hours if 30.0 <= hour.pop < 50.0]
    if len(moderate) == 1 and moderate[0].start.hour in (
        WINDOW_START_HOUR,
        WINDOW_END_HOUR,
    ):
        return False

    for previous, current in zip(moderate, moderate[1:]):
        if current.start.astimezone(timezone.utc) - previous.start.astimezone(
            timezone.utc
        ) == timedelta(hours=1):
            return True
    return False


def wind_threshold_trigger(hours: Sequence[ForecastHour]) -> bool:
    """Trigger only on sustained wind within the already-extracted local window."""
    return any(
        WINDOW_START_HOUR <= hour.start.hour <= WINDOW_END_HOUR
        and hour.wind_speed_mph is not None
        and hour.wind_speed_mph >= WIND_TRIGGER_MPH
        for hour in hours
    )


SPC_CATEGORY_NAMES = {
    "TSTM": "General Thunder",
    "MRGL": "Marginal",
    "SLGT": "Slight",
    "ENH": "Enhanced",
    "MDT": "Moderate",
    "HIGH": "High",
}
SPC_CATEGORY_RANK = {label: rank for rank, label in enumerate(SPC_CATEGORY_NAMES)}
FRANCE_COLOR_NAMES = {1: "Green", 2: "Yellow", 3: "Orange", 4: "Red"}
FRANCE_HAZARD_NAMES = {
    "1": "Wind",
    "2": "Rain/Flooding",
    "3": "Thunderstorms",
    "4": "Flooding",
    "5": "Snow/Ice",
    "6": "Extreme Heat",
    "7": "Extreme Cold",
    "8": "Avalanches",
    "9": "Coastal Flooding",
}
FRANCE_YELLOW_TRIGGER_HAZARDS = {"1", "2", "3", "5"}


def _point_on_segment(
    point: tuple[float, float], start: tuple[float, float], end: tuple[float, float]
) -> bool:
    px, py = point
    ax, ay = start
    bx, by = end
    cross = (px - ax) * (by - ay) - (py - ay) * (bx - ax)
    if not math.isclose(cross, 0.0, abs_tol=1e-10):
        return False
    return min(ax, bx) - 1e-10 <= px <= max(ax, bx) + 1e-10 and min(
        ay, by
    ) - 1e-10 <= py <= max(ay, by) + 1e-10


def _validated_ring(value: Any, context: str) -> list[tuple[float, float]]:
    if not isinstance(value, list) or len(value) < 4:
        raise WeatherAlertError(f"{context} is not a polygon ring")
    ring: list[tuple[float, float]] = []
    for index, coordinate in enumerate(value):
        if not isinstance(coordinate, list) or len(coordinate) < 2:
            raise WeatherAlertError(f"{context}[{index}] is not a coordinate")
        longitude = _finite_number(coordinate[0], f"{context}[{index}].longitude")
        latitude = _finite_number(coordinate[1], f"{context}[{index}].latitude")
        if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
            raise WeatherAlertError(f"{context}[{index}] is outside coordinate bounds")
        ring.append((longitude, latitude))
    if ring[0] != ring[-1]:
        raise WeatherAlertError(f"{context} is not closed")
    return ring


def _point_in_ring(point: tuple[float, float], ring: Sequence[tuple[float, float]]) -> int:
    """Return 0 outside, 1 inside, or 2 on the boundary."""
    inside = False
    px, py = point
    for start, end in zip(ring, ring[1:]):
        if _point_on_segment(point, start, end):
            return 2
        ax, ay = start
        bx, by = end
        if (ay > py) != (by > py):
            crossing_x = (bx - ax) * (py - ay) / (by - ay) + ax
            if px < crossing_x:
                inside = not inside
    return 1 if inside else 0


def _point_in_polygon(point: tuple[float, float], value: Any, context: str) -> bool:
    if not isinstance(value, list) or not value:
        raise WeatherAlertError(f"{context} has no rings")
    rings = [_validated_ring(ring, f"{context}[{index}]") for index, ring in enumerate(value)]
    outer_result = _point_in_ring(point, rings[0])
    if outer_result == 0:
        return False
    if outer_result == 2:
        return True
    return not any(_point_in_ring(point, hole) for hole in rings[1:])


def _geometry_covers(geometry: Any, coordinates: Coordinates, context: str) -> bool:
    item = _required_mapping(geometry, context)
    geometry_type = item.get("type")
    raw_coordinates = item.get("coordinates")
    point = (coordinates.longitude, coordinates.latitude)
    if geometry_type == "Polygon":
        return _point_in_polygon(point, raw_coordinates, f"{context}.coordinates")
    if geometry_type == "MultiPolygon":
        if not isinstance(raw_coordinates, list) or not raw_coordinates:
            raise WeatherAlertError(f"{context}.coordinates has no polygons")
        coverage = [
            _point_in_polygon(point, polygon, f"{context}.coordinates[{index}]")
            for index, polygon in enumerate(raw_coordinates)
        ]
        return any(coverage)
    raise WeatherAlertError(f"{context} has unsupported geometry type")


def _validate_severe_times(
    issued_at: datetime,
    valid_from: datetime,
    valid_to: datetime,
    target_date: date,
    now: datetime,
) -> None:
    if issued_at > now + timedelta(minutes=5):
        raise WeatherAlertError("severe outlook issuance is in the future")
    if now - issued_at > SEVERE_PRODUCT_MAX_AGE:
        raise WeatherAlertError("severe outlook is stale")
    if valid_from >= valid_to or now >= valid_to:
        raise WeatherAlertError("severe outlook validity period is expired or malformed")
    if valid_from.date() != target_date:
        raise WeatherAlertError("severe outlook is not the target day's product")


def parse_spc_day2_outlook(
    payload: Mapping[str, Any],
    coordinates: Coordinates,
    target_date: date,
    now: datetime,
) -> SevereOutlook | None:
    """Validate an SPC categorical GeoJSON product and find the highest covering risk."""
    if payload.get("type") != "FeatureCollection":
        raise WeatherAlertError("SPC outlook is not a FeatureCollection")
    features = payload.get("features")
    if not isinstance(features, list) or not features:
        raise WeatherAlertError("SPC outlook contains no features")

    product_times: tuple[datetime, datetime, datetime] | None = None
    highest_label: str | None = None
    for index, raw_feature in enumerate(features):
        feature = _required_mapping(raw_feature, f"SPC feature[{index}]")
        properties = _required_mapping(
            feature.get("properties"), f"SPC feature[{index}].properties"
        )
        label = properties.get("LABEL")
        if label not in SPC_CATEGORY_NAMES:
            raise WeatherAlertError(f"SPC feature[{index}] has unknown category")
        issued_at = _parse_aware_datetime(
            properties.get("ISSUE_ISO"), f"SPC feature[{index}].ISSUE_ISO"
        )
        valid_from = _parse_aware_datetime(
            properties.get("VALID_ISO"), f"SPC feature[{index}].VALID_ISO"
        )
        valid_to = _parse_aware_datetime(
            properties.get("EXPIRE_ISO"), f"SPC feature[{index}].EXPIRE_ISO"
        )
        times = (issued_at, valid_from, valid_to)
        if product_times is None:
            product_times = times
            _validate_severe_times(*times, target_date, now)
        elif times != product_times:
            raise WeatherAlertError("SPC features have inconsistent product times")

        if _geometry_covers(feature.get("geometry"), coordinates, f"SPC feature[{index}].geometry"):
            if highest_label is None or SPC_CATEGORY_RANK[label] > SPC_CATEGORY_RANK[
                highest_label
            ]:
                highest_label = label

    if product_times is None or highest_label is None:
        return None
    issued_at, valid_from, valid_to = product_times
    return SevereOutlook(
        source="SPC Day 2 Convective Outlook",
        level=SPC_CATEGORY_NAMES[highest_label],
        issued_at=issued_at,
        valid_from=valid_from,
        valid_to=valid_to,
        triggers=SPC_CATEGORY_RANK[highest_label] >= SPC_CATEGORY_RANK["MRGL"],
    )


def _spc_probability_feature_is_hatched(properties: Mapping[str, Any]) -> bool:
    """Return whether an SPC probability feature denotes significant/hatched risk."""
    for key, value in properties.items():
        if value is None:
            continue
        if re.search(r"(?:significant|hatched)", str(value), re.IGNORECASE):
            return True
        if re.search(r"(?:sig|hatched)", key, re.IGNORECASE):
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value > 0
            return str(value).strip().lower() not in {"", "0", "false", "no", "none"}
    return False


def _spc_probability(properties: Mapping[str, Any], context: str) -> float:
    value = properties.get("LABEL")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        probability = float(value)
    elif isinstance(value, str):
        match = re.search(r"\d+(?:\.\d+)?", value)
        if not match:
            raise WeatherAlertError(f"{context}.LABEL is not a probability")
        probability = float(match.group())
        if "%" in value:
            probability /= 100
    else:
        raise WeatherAlertError(f"{context}.LABEL is not a probability")
    if not 0 <= probability <= 1:
        raise WeatherAlertError(f"{context}.LABEL is outside 0..1")
    return probability


def parse_spc_day2_hazard(
    payload: Mapping[str, Any],
    hazard: str,
    coordinates: Coordinates,
    target_date: date,
    now: datetime,
) -> str | None:
    """Return the local SPC hazard, including its risk only when notable."""
    if payload.get("type") != "FeatureCollection":
        raise WeatherAlertError(f"SPC {hazard.lower()} outlook is not a FeatureCollection")
    features = payload.get("features")
    if not isinstance(features, list):
        raise WeatherAlertError(f"SPC {hazard.lower()} outlook has no features")

    highest_probability: float | None = None
    hatched = False
    for index, raw_feature in enumerate(features):
        feature = _required_mapping(raw_feature, f"SPC {hazard} feature[{index}]")
        properties = _required_mapping(
            feature.get("properties"), f"SPC {hazard} feature[{index}].properties"
        )
        issued_at = _parse_aware_datetime(
            properties.get("ISSUE_ISO"), f"SPC {hazard} feature[{index}].ISSUE_ISO"
        )
        valid_from = _parse_aware_datetime(
            properties.get("VALID_ISO"), f"SPC {hazard} feature[{index}].VALID_ISO"
        )
        valid_to = _parse_aware_datetime(
            properties.get("EXPIRE_ISO"), f"SPC {hazard} feature[{index}].EXPIRE_ISO"
        )
        _validate_severe_times(issued_at, valid_from, valid_to, target_date, now)
        if not _geometry_covers(
            feature.get("geometry"), coordinates, f"SPC {hazard} feature[{index}].geometry"
        ):
            continue
        probability = _spc_probability(properties, f"SPC {hazard} feature[{index}]")
        if highest_probability is None or probability > highest_probability:
            highest_probability = probability
        hatched = hatched or _spc_probability_feature_is_hatched(properties)

    if highest_probability is None:
        return None
    threshold = 0.05 if hazard == "Tornado" else 0.10
    if highest_probability >= threshold or hatched:
        percent = _format_percentage(highest_probability * 100)
        qualifier = f"{percent}%" + (" hatched" if hatched else "")
        return f"{hazard} ({qualifier})"
    return hazard


def fetch_spc_day2_outlook(
    coordinates: Coordinates, target_date: date, now: datetime, user_agent: str
) -> SevereOutlook | None:
    outlook = parse_spc_day2_outlook(
        fetch_json(SPC_DAY2_URL, user_agent), coordinates, target_date, now
    )
    if outlook is None:
        return None
    try:
        hazards = tuple(
            result
            for hazard, url in SPC_DAY2_HAZARD_URLS.items()
            if (
                result := parse_spc_day2_hazard(
                    fetch_json(url, user_agent), hazard, coordinates, target_date, now
                )
            )
        )
    except WeatherAlertError:
        # These layers improve the email detail but must not suppress a valid
        # categorical alert when a companion SPC product is unavailable.
        hazards = ()
    return replace(outlook, hazards=hazards)


def lookup_france_department(
    coordinates: Coordinates, user_agent: str
) -> str | None:
    query = urlencode(
        {
            "lat": _coordinate_for_url(coordinates.latitude),
            "lon": _coordinate_for_url(coordinates.longitude),
            "limit": "1",
        }
    )
    payload = fetch_json(f"{FRANCE_REVERSE_GEOCODE_URL}?{query}", user_agent)
    if payload.get("type") != "FeatureCollection":
        raise WeatherAlertError("French reverse-geocode response is malformed")
    features = payload.get("features")
    if not isinstance(features, list):
        raise WeatherAlertError("French reverse-geocode response has no features")
    if not features:
        return None
    feature = _required_mapping(features[0], "French reverse-geocode feature")
    properties = _required_mapping(
        feature.get("properties"), "French reverse-geocode properties"
    )
    context = properties.get("context")
    if not isinstance(context, str):
        raise WeatherAlertError("French reverse-geocode response has no department")
    department = context.split(",", 1)[0].strip().upper()
    if not re.fullmatch(r"(?:\d{2,3}|2A|2B)", department):
        raise WeatherAlertError("French reverse-geocode department code is invalid")
    return department


def fetch_meteo_france_vigilance_payload(user_agent: str) -> Mapping[str, Any]:
    _, response_headers = fetch_http(
        METEO_FRANCE_SITE_URL,
        user_agent,
        accept="text/html,application/xhtml+xml",
    )
    cookie_header = response_headers.get("set-cookie")
    if not cookie_header:
        raise WeatherAlertError("Météo-France did not provide a public session")
    cookies = SimpleCookie()
    cookies.load(cookie_header)
    session = cookies.get("mfsession")
    if session is None or not session.value:
        raise WeatherAlertError("Météo-France public session is missing")
    token = codec_decode(session.value, "rot_13")
    query = urlencode(
        {
            "domain": "FRA",
            "warning_type": "vigilance",
            "formatDate": "iso",
            "echeance": "J1",
            "depth": "1",
        }
    )
    return fetch_json(
        f"{METEO_FRANCE_VIGILANCE_URL}?{query}",
        user_agent,
        headers={"Authorization": f"Bearer {token}"},
    )


def parse_meteo_france_vigilance(
    payload: Mapping[str, Any],
    department: str,
    target_date: date,
    now: datetime,
) -> SevereOutlook:
    issued_at = _parse_aware_datetime(payload.get("update_time"), "Vigilance update_time")
    valid_to = _parse_aware_datetime(
        payload.get("end_validity_time"), "Vigilance end_validity_time"
    )
    paris = ZoneInfo("Europe/Paris")
    valid_from = datetime.combine(target_date, datetime.min.time(), tzinfo=paris)
    if issued_at > now + timedelta(minutes=5):
        raise WeatherAlertError("Vigilance issuance is in the future")
    if now - issued_at > SEVERE_PRODUCT_MAX_AGE:
        raise WeatherAlertError("Vigilance product is stale")
    if issued_at.astimezone(paris).date() != target_date - timedelta(days=1):
        raise WeatherAlertError("Vigilance product was not issued for the target day")
    expected_end = datetime.combine(
        target_date + timedelta(days=1), datetime.min.time(), tzinfo=paris
    )
    if valid_to.astimezone(paris) != expected_end or now >= valid_to:
        raise WeatherAlertError("Vigilance validity does not cover the target day")

    subdomains = payload.get("subdomains_phenomenons_max_color")
    if not isinstance(subdomains, list):
        raise WeatherAlertError("Vigilance product has no departmental data")
    department_data: Mapping[str, Any] | None = None
    for index, raw_item in enumerate(subdomains):
        item = _required_mapping(raw_item, f"Vigilance subdomain[{index}]")
        if item.get("domain_id") == department:
            department_data = item
            break
    if department_data is None:
        raise WeatherAlertError(f"Vigilance product has no data for department {department}")

    raw_hazards = department_data.get("phenomenons_max_color")
    if not isinstance(raw_hazards, list) or not raw_hazards:
        raise WeatherAlertError("Vigilance department has no hazard levels")
    colored_hazards: list[tuple[str, int]] = []
    triggers = False
    for index, raw_hazard in enumerate(raw_hazards):
        hazard = _required_mapping(raw_hazard, f"Vigilance hazard[{index}]")
        hazard_id = hazard.get("phenomenon_id")
        color_value = hazard.get("phenomenon_max_color_id")
        if hazard_id not in FRANCE_HAZARD_NAMES:
            raise WeatherAlertError("Vigilance product has an unknown hazard")
        color_number = _finite_number(color_value, "Vigilance hazard color")
        if not color_number.is_integer() or int(color_number) not in FRANCE_COLOR_NAMES:
            raise WeatherAlertError("Vigilance product has an unknown color")
        color = int(color_number)
        if color >= 2:
            colored_hazards.append((hazard_id, color))
        if color >= 3 or (color >= 2 and hazard_id in FRANCE_YELLOW_TRIGGER_HAZARDS):
            triggers = True

    highest_color = max((color for _, color in colored_hazards), default=1)
    hazards = tuple(
        f"{FRANCE_COLOR_NAMES[color]} {FRANCE_HAZARD_NAMES[hazard_id]}"
        for hazard_id, color in sorted(colored_hazards)
    )
    return SevereOutlook(
        source="Météo-France Vigilance (tomorrow)",
        level=FRANCE_COLOR_NAMES[highest_color],
        issued_at=issued_at,
        valid_from=valid_from,
        valid_to=valid_to,
        hazards=hazards,
        triggers=triggers,
    )


def fetch_meteo_france_vigilance(
    department: str, target_date: date, now: datetime, user_agent: str
) -> SevereOutlook:
    return parse_meteo_france_vigilance(
        fetch_meteo_france_vigilance_payload(user_agent),
        department,
        target_date,
        now,
    )


def _format_percentage(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _format_speed(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def resolve_location(
    coordinates: Coordinates,
    user_agent: str,
    operational_errors: list[str] | None = None,
) -> str:
    """Name the alert location without allowing reverse geocoding to block email delivery."""
    query = urlencode(
        {
            "format": "jsonv2",
            "addressdetails": "1",
            "lat": _coordinate_for_url(coordinates.latitude),
            "lon": _coordinate_for_url(coordinates.longitude),
            "zoom": "10",
        }
    )
    try:
        payload = fetch_json(f"{NOMINATIM_REVERSE_URL}?{query}", user_agent)
    except WeatherAlertError as exc:
        if operational_errors is not None:
            operational_errors.append(f"reverse geocoding failed: {exc}")
        return "Location unavailable"
    address = payload.get("address")
    if not isinstance(address, Mapping):
        if operational_errors is not None:
            operational_errors.append(
                "reverse geocoding failed: response contains no address"
            )
        return "Location unavailable"
    for key in ("city", "town", "village", "hamlet"):
        location = address.get(key)
        if isinstance(location, str) and location.strip():
            return location.strip()
    county = address.get("county")
    if isinstance(county, str) and county.strip():
        return county.strip()
    return "Location unavailable"


def _rounded_pop(value: float) -> int:
    """Round a precipitation probability to the nearest five percent."""
    return max(0, min(100, int(math.floor(value / 5 + 0.5) * 5)))


def _forecast_description(description: str) -> str:
    """Remove forecast-uncertainty and intensity qualifiers from display text."""
    simplified = re.sub(
        r"\b(?:a|an|and|areas?|chance|few|heavy|isolated|likely|light|mainly|"
        r"moderate|mostly|numerous|occasional|of|partly|patchy|possible|scattered|slight)\b",
        " ",
        description,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", simplified).strip(" -/,;") or description


def _weather_icon(description: str) -> str:
    normalized = description.lower()
    if "thunder" in normalized or "t-storm" in normalized:
        return "⛈️"
    if any(term in normalized for term in ("snow", "sleet", "ice", "blizzard")):
        return "🌨️"
    if any(term in normalized for term in ("rain", "shower", "drizzle")):
        return "🌧️"
    if "fog" in normalized:
        return "🌫️"
    if "partly" in normalized:
        return "⛅"
    if "cloud" in normalized or "overcast" in normalized:
        return "☁️"
    if "sun" in normalized or "clear" in normalized:
        return "☀️"
    return "🌤️"


def _hourly_forecast_lines(hours: Sequence[ForecastHour]) -> list[tuple[str, str, int, str, bool]]:
    return [
        (
            f"{hour.start:%H:%M}",
            _weather_icon(hour.description),
            _rounded_pop(hour.pop),
            _forecast_description(hour.description),
            hour.pop > 75,
        )
        for hour in hours
    ]


def _wind_period_lines(hours: Sequence[ForecastHour], triggered: bool) -> list[str]:
    available = [hour for hour in hours if hour.wind_speed_mph is not None]
    if not available:
        return ["Wind Direction: Unavailable"]

    relevant = (
        [hour for hour in available if hour.wind_speed_mph >= WIND_TRIGGER_MPH]
        if triggered
        else available
    )
    directions = list(dict.fromkeys(hour.wind_direction for hour in relevant if hour.wind_direction))
    return ["Wind Direction: " + (", ".join(directions) if directions else "Unavailable")]


def build_email_body(
    target_date: date,
    coordinates: Coordinates,
    hours: Sequence[ForecastHour],
    decision: AlertDecision | None = None,
    location: str = "Location unavailable",
) -> str:
    alert_decision = decision or AlertDecision(
        rain_trigger=thresholds_trigger(hours),
        wind_trigger=wind_threshold_trigger(hours),
    )
    lines: list[str] = []
    lines.extend(
        f"{time} - {pop}% - {description}"
        for time, _icon, pop, description, _bold in _hourly_forecast_lines(hours)
    )
    if not hours:
        lines.append("Hourly forecast unavailable.")
    lines.extend(["", "Wind:"])
    lines.extend(_wind_period_lines(hours, alert_decision.wind_trigger))

    lines.extend(["", "Severe outlook:"])
    if alert_decision.severe_outlook:
        outlook = alert_decision.severe_outlook
        lines.append(f"Level: {outlook.level}")
        if outlook.hazards:
            lines.append(f"Hazards: {', '.join(outlook.hazards)}")
    else:
        lines.append(
            "No valid official next-day outlook was available or covered this location."
        )
    lines.extend(["", "Location:", location])
    return "\n".join(lines) + "\n"


def build_email_html(
    target_date: date,
    coordinates: Coordinates,
    hours: Sequence[ForecastHour],
    decision: AlertDecision | None = None,
    location: str = "Location unavailable",
) -> str:
    """Build the HTML version of the alert while preserving the plain-text fallback."""
    alert_decision = decision or AlertDecision(
        rain_trigger=thresholds_trigger(hours),
        wind_trigger=wind_threshold_trigger(hours),
    )
    forecast_rows = "".join(
        ("<tr style=\"font-weight: 700;\">" if bold else "<tr>")
        + f"<td style=\"padding: 5px 10px 5px 0;\">{time}</td>"
        + f"<td style=\"padding: 5px 10px 5px 0;\">{icon}</td>"
        + f"<td style=\"padding: 5px 10px 5px 0;\">{pop}%</td>"
        + f"<td style=\"padding: 5px 0;\">{html.escape(description)}</td></tr>"
        for time, icon, pop, description, bold in _hourly_forecast_lines(hours)
    )
    if not forecast_rows:
        forecast_rows = "<tr><td>Hourly forecast unavailable.</td></tr>"

    wind = html.escape(" ".join(_wind_period_lines(hours, alert_decision.wind_trigger)))
    severe_lines: list[str]
    if alert_decision.severe_outlook:
        outlook = alert_decision.severe_outlook
        severe_lines = [f"Level: {outlook.level}"]
        if outlook.hazards:
            severe_lines.append(f"Hazards: {', '.join(outlook.hazards)}")
    else:
        severe_lines = ["No valid official next-day outlook was available or covered this location."]
    severe = "<br>".join(html.escape(line) for line in severe_lines)
    return (
        "<!doctype html><html><body style=\"font-family: Arial, sans-serif; color: #202124;\">"
        "<table role=\"presentation\" style=\"border-collapse: collapse;\">"
        f"{forecast_rows}</table>"
        f"<p><strong>Wind:</strong><br>{wind}</p>"
        f"<p><strong>Severe outlook:</strong><br>{severe}</p>"
        f"<p><strong>Location:</strong><br>{html.escape(location)}</p>"
        "</body></html>"
    )


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise WeatherAlertError(f"{name} must be true or false")


def _optional_env_int(name: str) -> int | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise WeatherAlertError(f"{name} must be an integer") from exc


def _optional_env_float(name: str) -> float | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise WeatherAlertError(f"{name} must be numeric") from exc


def _optional_env_date(name: str) -> date | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise WeatherAlertError(f"{name} must be an ISO-8601 date") from exc


def load_smtp_config() -> SmtpConfig:
    host = os.environ.get("SMTP_HOST", "").strip()
    sender = os.environ.get("SMTP_FROM", "").strip()
    recipients = tuple(
        address.strip()
        for address in os.environ.get("SMTP_TO", "").split(",")
        if address.strip()
    )
    if not host or not sender or not recipients:
        raise WeatherAlertError("SMTP_HOST, SMTP_FROM, and SMTP_TO are required")

    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
        timeout_seconds = float(os.environ.get("SMTP_TIMEOUT_SECONDS", "20"))
    except ValueError as exc:
        raise WeatherAlertError("SMTP_PORT and SMTP_TIMEOUT_SECONDS must be numeric") from exc
    if not 1 <= port <= 65535 or timeout_seconds <= 0:
        raise WeatherAlertError("invalid SMTP port or timeout")

    username = os.environ.get("SMTP_USERNAME") or None
    password = os.environ.get("SMTP_PASSWORD") or None
    if bool(username) != bool(password):
        raise WeatherAlertError("SMTP_USERNAME and SMTP_PASSWORD must be set together")

    use_ssl = _env_bool("SMTP_USE_SSL", port == 465)
    starttls = _env_bool("SMTP_STARTTLS", not use_ssl)
    if use_ssl and starttls:
        raise WeatherAlertError("SMTP_USE_SSL and SMTP_STARTTLS cannot both be enabled")
    return SmtpConfig(
        host=host,
        port=port,
        username=username,
        password=password,
        sender=sender,
        recipients=recipients,
        use_ssl=use_ssl,
        starttls=starttls,
        timeout_seconds=timeout_seconds,
    )


def send_alert(
    config: SmtpConfig, target_date: date, body: str, html_body: str | None = None
) -> None:
    message = EmailMessage()
    message["Subject"] = f"Weather Alert - {target_date:%A}"
    message["From"] = config.sender
    message["To"] = ", ".join(config.recipients)
    message["Message-ID"] = make_msgid()
    message.set_content(body)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    tls_context = ssl.create_default_context()
    for attempt in range(2):
        transfer_started = False
        try:
            if config.use_ssl:
                smtp_connection = smtplib.SMTP_SSL(
                    config.host,
                    config.port,
                    timeout=config.timeout_seconds,
                    context=tls_context,
                )
            else:
                smtp_connection = smtplib.SMTP(
                    config.host,
                    config.port,
                    timeout=config.timeout_seconds,
                )
            with smtp_connection as smtp:
                if config.starttls:
                    smtp.ehlo()
                    smtp.starttls(context=tls_context)
                    smtp.ehlo()
                if config.username and config.password:
                    smtp.login(config.username, config.password)
                transfer_started = True
                smtp.send_message(message, from_addr=config.sender, to_addrs=config.recipients)
            return
        except (OSError, smtplib.SMTPException) as exc:
            # A lost response after DATA can mean Gmail already accepted the
            # message. Repeating it in that state sent two real alerts.
            if transfer_started:
                raise WeatherAlertError(
                    "SMTP result uncertain after message transfer; check the inbox"
                ) from exc
            if attempt == 1:
                raise WeatherAlertError("SMTP delivery failed after 2 attempts") from exc
            time.sleep(2)


def _log_current_exception() -> None:
    """Best-effort file logging that can never leak errors to cron output."""
    log_path = os.environ.get("WEATHER_ALERT_LOG_FILE", "").strip()
    if not log_path:
        return
    try:
        timestamp = datetime.now(timezone.utc).isoformat()
        with Path(log_path).expanduser().open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} ERROR weather alert job failed\n")
            handle.write(traceback.format_exc())
            handle.write("\n")
    except Exception:
        pass


def _fetch_nws_periods_with_optional_gusts(
    metadata: PointMetadata,
    user_agent: str,
    operational_errors: list[str] | None = None,
) -> Sequence[Mapping[str, Any]]:
    periods = fetch_hourly_periods(metadata.forecast_url, user_agent)
    if metadata.forecast_grid_data_url:
        try:
            gusts = fetch_nws_wind_gusts(metadata.forecast_grid_data_url, user_agent)
            periods = add_nws_wind_gusts(periods, gusts)
        except WeatherAlertError as exc:
            if operational_errors is not None:
                operational_errors.append(f"NWS wind-gust API failed: {exc}")
    return periods


def evaluate_due_alert(
    coordinates: Coordinates,
    user_agent: str,
    now: datetime,
    configured_hour: int | None,
    max_coordinate_age_hours: float | None = None,
    last_evaluated_local_date: date | None = None,
) -> AlertEvaluation | None:
    """Evaluate forecast and severe sources independently for the next local day."""
    metadata: PointMetadata | None = None
    periods: Sequence[Mapping[str, Any]] | None = None
    location_timezone: ZoneInfo | None = None
    forecast_error: WeatherAlertError | None = None
    severe_error: WeatherAlertError | None = None
    department: str | None = None
    operational_errors: list[str] = []

    try:
        metadata = get_point_metadata(coordinates, user_agent)
        location_timezone = metadata.location_timezone
    except WeatherAlertError:
        try:
            location_timezone, periods = fetch_open_meteo_hourly(coordinates)
        except WeatherAlertError as exc:
            forecast_error = exc
            operational_errors.append(f"forecast data validation failed: {exc}")
            try:
                department = lookup_france_department(coordinates, user_agent)
            except WeatherAlertError as lookup_error:
                raise WeatherAlertError(
                    "could not resolve a local time zone from forecast or France location data"
                ) from lookup_error
            if department is None:
                raise WeatherAlertError(
                    "could not resolve a local time zone for the coordinates"
                ) from exc
            location_timezone = ZoneInfo("Europe/Paris")

    if location_timezone is None:
        raise WeatherAlertError("location time zone is unavailable")
    if not should_run_at_local_hour(now, location_timezone, configured_hour):
        return None
    local_date = now.astimezone(location_timezone).date()
    if last_evaluated_local_date == local_date:
        return None
    validate_coordinate_freshness(coordinates, now, max_coordinate_age_hours)
    target_date = local_date + timedelta(days=1)

    hours: tuple[ForecastHour, ...] = ()
    if metadata is not None:
        try:
            periods = _fetch_nws_periods_with_optional_gusts(
                metadata, user_agent, operational_errors
            )
        except WeatherAlertError as primary_error:
            try:
                fallback_zone, periods = fetch_open_meteo_hourly(coordinates)
                if fallback_zone != location_timezone:
                    raise WeatherAlertError("forecast providers disagree on local time zone")
                operational_errors.append(
                    f"NWS hourly forecast API failed; Open-Meteo fallback was used: "
                    f"{primary_error}"
                )
            except WeatherAlertError as exc:
                forecast_error = exc
                operational_errors.append(
                    "regular forecast APIs failed: "
                    f"NWS: {primary_error}; Open-Meteo: {exc}"
                )
    if periods is not None:
        try:
            extracted_date, extracted_hours = extract_tomorrow_window(
                periods, location_timezone, now
            )
            if extracted_date != target_date:
                raise WeatherAlertError("forecast target date is inconsistent")
            hours = tuple(extracted_hours)
            forecast_error = None
        except WeatherAlertError as exc:
            forecast_error = exc
            operational_errors.append(f"forecast data validation failed: {exc}")

    severe_outlook: SevereOutlook | None = None
    if metadata is not None:
        try:
            severe_outlook = fetch_spc_day2_outlook(
                coordinates, target_date, now, user_agent
            )
        except WeatherAlertError as exc:
            severe_error = exc
            operational_errors.append(f"SPC severe-outlook API failed: {exc}")
    elif str(location_timezone) == "Europe/Paris":
        if department is None:
            try:
                department = lookup_france_department(coordinates, user_agent)
            except WeatherAlertError as exc:
                severe_error = exc
                operational_errors.append(
                    f"France department lookup API failed: {exc}"
                )
        if department is not None:
            try:
                severe_outlook = fetch_meteo_france_vigilance(
                    department, target_date, now, user_agent
                )
                severe_error = None
            except WeatherAlertError as exc:
                severe_error = exc
                operational_errors.append(
                    f"Météo-France severe-outlook API failed: {exc}"
                )

    decision = AlertDecision(
        rain_trigger=thresholds_trigger(hours),
        wind_trigger=wind_threshold_trigger(hours),
        severe_outlook=severe_outlook,
    )
    if not decision.should_send:
        if forecast_error is not None:
            raise WeatherAlertError("regular forecast evaluation failed") from forecast_error
        if severe_error is not None:
            raise WeatherAlertError("severe outlook evaluation failed") from severe_error
    return AlertEvaluation(target_date, hours, decision, tuple(operational_errors))


def _write_evaluation_marker(target_date: date) -> None:
    """Record a completed evaluation for the workflow's persistent date marker."""
    marker_path = os.environ.get("WEATHER_ALERT_EVALUATION_DATE_FILE", "").strip()
    if not marker_path:
        return
    evaluation_date = target_date - timedelta(days=1)
    Path(marker_path).expanduser().write_text(
        evaluation_date.isoformat() + "\n", encoding="utf-8"
    )


def main() -> int:
    try:
        coords_path = Path(
            os.environ.get("COORDS_FILE", Path(__file__).with_name("coords.json"))
        ).expanduser()
        coordinates = load_coordinates(coords_path)
        user_agent = os.environ.get(
            "NWS_USER_AGENT", "daily-weather-alert/1.0"
        ).strip()
        if not user_agent:
            raise WeatherAlertError("NWS_USER_AGENT cannot be empty")

        now = datetime.now(timezone.utc)
        evaluation = evaluate_due_alert(
            coordinates,
            user_agent,
            now,
            _optional_env_int("RUN_ONLY_AT_LOCAL_HOUR"),
            _optional_env_float("MAX_COORDINATE_AGE_HOURS"),
            _optional_env_date("LAST_EVALUATED_LOCAL_DATE"),
        )
        if evaluation is None:
            return 0
        operational_errors = list(evaluation.operational_errors)
        if evaluation.decision.should_send:
            smtp_config = load_smtp_config()
            location = resolve_location(coordinates, user_agent, operational_errors)
            body = build_email_body(
                evaluation.target_date,
                coordinates,
                evaluation.hours,
                evaluation.decision,
                location,
            )
            html_body = build_email_html(
                evaluation.target_date,
                coordinates,
                evaluation.hours,
                evaluation.decision,
                location,
            )
            send_alert(smtp_config, evaluation.target_date, body, html_body)
        _write_evaluation_marker(evaluation.target_date)
        if operational_errors:
            raise WeatherAlertError("; ".join(operational_errors))
        return 0
    except Exception:
        _log_current_exception()
        if os.environ.get("WEATHER_ALERT_REPORT_ERRORS", "").lower() == "true":
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
