"""Tests for config loading and URL normalization."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from webcal_mcp.config import CalendarConfig, load_config


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(dedent(body))
    return path


def test_loads_multiple_calendars(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """\
            default_ttl_seconds = 600

            [calendars.personal]
            url = "https://example.com/p.ics"
            description = "Personal"

            [calendars.work]
            url = "webcal://example.com/w.ics"
            ttl_seconds = 1800
            """,
        )
    )
    assert set(cfg.calendars) == {"personal", "work"}
    assert cfg.calendars["personal"].ttl_seconds == 600  # inherits default
    assert cfg.calendars["work"].ttl_seconds == 1800
    assert cfg.default_calendar is None  # ambiguous with two calendars


def test_single_calendar_is_default(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """\
            [calendars.only]
            url = "https://example.com/o.ics"
            """,
        )
    )
    assert cfg.default_calendar == "only"


def test_missing_url_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing 'url'"):
        load_config(
            _write(
                tmp_path,
                """\
                [calendars.bad]
                description = "no url"
                """,
            )
        )


def test_eventkit_source(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """\
            [calendars.personal]
            source = "eventkit"
            identifier = "Personal"
            description = "iCloud personal"
            """,
        )
    )
    cal = cfg.calendars["personal"]
    assert cal.source == "eventkit"
    assert cal.identifier == "Personal"
    assert cal.url == ""


def test_eventkit_identifier_defaults_to_name(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """\
            [calendars.Work]
            source = "eventkit"
            """,
        )
    )
    assert cfg.calendars["Work"].identifier == "Work"


def test_unknown_source_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown source"):
        load_config(
            _write(
                tmp_path,
                """\
                [calendars.bad]
                source = "carrier-pigeon"
                """,
            )
        )


def test_ics_source_still_requires_url(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing 'url'"):
        load_config(
            _write(
                tmp_path,
                """\
                [calendars.bad]
                source = "ics"
                """,
            )
        )


def test_no_calendars_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No \\[calendars"):
        load_config(_write(tmp_path, "default_ttl_seconds = 60\n"))


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_nonpositive_timeout_rejected(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError, match="http_timeout_seconds"):
        load_config(
            _write(
                tmp_path,
                f"""\
                http_timeout_seconds = {bad}

                [calendars.only]
                url = "https://example.com/o.ics"
                """,
            )
        )


@pytest.mark.parametrize(
    "url,expected",
    [
        ("webcal://example.com/x.ics", "https://example.com/x.ics"),
        ("webcals://example.com/x.ics", "https://example.com/x.ics"),
        ("https://example.com/x.ics", "https://example.com/x.ics"),
        ("http://example.com/x.ics", "http://example.com/x.ics"),
    ],
)
def test_http_url_normalization(url: str, expected: str) -> None:
    assert CalendarConfig(name="c", url=url).http_url == expected
