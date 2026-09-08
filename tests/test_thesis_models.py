from pathlib import Path

import pytest

from app.thesis.models import validate_json_mapping, validate_json_value


@pytest.mark.parametrize(
    "payload",
    [
        {"x": object()},
        {"x": Path("/tmp")},
        {"x": {"nested": {1: "bad key"}}},
        {"x": {"nested": {"thing": set()}}},
    ],
)
def test_rejects_untrusted_objects(payload: object) -> None:
    with pytest.raises(ValueError):
        validate_json_value(payload)


@pytest.mark.parametrize(
    "payload",
    [float("nan"), float("inf"), float("-inf"), {"x": float("nan")}, [float("inf")]],
)
def test_rejects_non_finite_floats(payload: object) -> None:
    with pytest.raises(ValueError):
        validate_json_value(payload)


def test_accepts_finite_scalars() -> None:
    assert validate_json_value(3.14) == 3.14
    assert validate_json_value(0.0) == 0.0
    assert validate_json_value(-1.5) == -1.5
    assert validate_json_value(1) == 1
    assert validate_json_value("a") == "a"
    assert validate_json_value(None) is None
    assert validate_json_value(True) is True


def test_normalizes_tuples_to_lists() -> None:
    out = validate_json_value((1, 2))
    assert out == [1, 2]
    assert isinstance(out, list)
    nested = validate_json_value({"x": (1, {"y": ()})})
    assert nested == {"x": [1, {"y": []}]}
    assert isinstance(nested, dict)
    assert isinstance(nested["x"], list)


def test_round_trip_and_deep_copy() -> None:
    src = {"a": 1, "b": [1, 2.5, "x", None, True], "c": {"d": False}}
    out = validate_json_value(src)
    assert out == src
    assert out is not src
    assert isinstance(out, dict)
    assert out["b"] is not src["b"]
    src["b"].append(99)
    src["c"]["d"] = True
    assert out == {"a": 1, "b": [1, 2.5, "x", None, True], "c": {"d": False}}


def test_where_propagates() -> None:
    with pytest.raises(ValueError) as e:
        validate_json_value({"x": object()}, "test: leverage")
    assert "test: leverage" in str(e.value)


def test_mapping_narrowing() -> None:
    assert validate_json_mapping({}, "w") == {}
    with pytest.raises(ValueError):
        validate_json_mapping([1, 2], "w")
    with pytest.raises(ValueError):
        validate_json_mapping("s", "w")
    with pytest.raises(ValueError):
        validate_json_mapping({"x": set()}, "w")
