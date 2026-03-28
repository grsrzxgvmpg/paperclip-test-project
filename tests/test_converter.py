import pytest
from converter import (
    celsius_to_fahrenheit,
    fahrenheit_to_celsius,
    km_to_miles,
    miles_to_km,
    kg_to_lbs,
    lbs_to_kg,
    liter_to_gallon,
    gallon_to_liter,
    mph_to_kph,
    kph_to_mph,
)


def test_liter_to_gallon():
    assert liter_to_gallon(3.78541) == pytest.approx(1.0)


def test_gallon_to_liter():
    assert gallon_to_liter(1.0) == pytest.approx(3.78541)


def test_boiling_point():
    assert celsius_to_fahrenheit(100) == pytest.approx(212.0)


def test_freezing_point():
    assert celsius_to_fahrenheit(0) == pytest.approx(32.0)


def test_f_to_c_round_trip():
    assert fahrenheit_to_celsius(celsius_to_fahrenheit(37.5)) == pytest.approx(37.5)


def test_km_to_miles():
    assert km_to_miles(1.0) == pytest.approx(0.621371)


def test_miles_to_km():
    assert miles_to_km(1.0) == pytest.approx(1.60934, rel=1e-3)


def test_kg_to_lbs():
    assert kg_to_lbs(1.0) == pytest.approx(2.20462)


def test_lbs_to_kg():
    assert lbs_to_kg(1.0) == pytest.approx(0.453592, rel=1e-3)


def test_mph_to_kph():
    assert mph_to_kph(1.0) == pytest.approx(1.60934, rel=1e-3)


def test_kph_to_mph():
    assert kph_to_mph(1.60934) == pytest.approx(1.0, rel=1e-3)
