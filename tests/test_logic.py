"""Focused unit tests for the pure validation and selection policy."""
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from controller.main import Controller, validate_split
import pytest


def test_valid_splits():
    validate_split(50, 50)
    validate_split(100, 0)
    validate_split(0, 100)

@pytest.mark.parametrize("a,b", [(101, -1), (-1, 101), (60, 30), (50, 51), (110, 0)])
def test_invalid_splits(a, b):
    with pytest.raises(ValueError):
        validate_split(a, b)


def ready(controller, name, weight, routing="enabled"):
    controller.containers[name].update(runtime="running", health="healthy", routing=routing)
    controller.configured_split[name] = weight


def test_effective_split_uses_desired_ratio_when_both_healthy():
    c = Controller()
    ready(c, "a", 70)
    ready(c, "b", 30)
    assert c.effective_split_unlocked() == {"a": 70, "b": 30}


def test_single_healthy_container_gets_all_effective_traffic_even_if_weight_zero():
    c = Controller()
    ready(c, "a", 0)
    c.containers["b"].update(runtime="running", health="unhealthy", routing="enabled")
    assert c.effective_split_unlocked() == {"a": 100, "b": 0}


def test_draining_container_is_not_eligible():
    c = Controller()
    ready(c, "a", 50, routing="draining")
    ready(c, "b", 50)
    assert c.effective_split_unlocked() == {"a": 0, "b": 100}


def test_no_healthy_backend_returns_zero_effective_split():
    c = Controller()
    c.containers["a"].update(runtime="stopped", health="unknown", routing="disabled")
    c.containers["b"].update(runtime="running", health="unhealthy", routing="enabled")
    assert c.effective_split_unlocked() == {"a": 0, "b": 0}
