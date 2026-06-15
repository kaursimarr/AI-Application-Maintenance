import pytest
from input.calculator import calculate_emi

def test_normal_emi():
    assert calculate_emi(100000, 12, 12) == 8884.88

def test_zero_interest():
    # This test will fail on the buggy code above
    assert calculate_emi(100000, 0, 12) == 8333.33