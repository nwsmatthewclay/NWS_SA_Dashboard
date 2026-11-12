import pytest

from mesoscale.diagnostics import _finite_profile


def test_profile_rejects_mismatched_arrays():
    with pytest.raises(ValueError, match="equal length"):
        _finite_profile([1000, 900, 800, 700, 600],
                        [290, 285, 280, 275, 270],
                        [285, 280, 275, 270],
                        [0, 1000, 2000, 3000, 4000])


def test_profile_rejects_duplicate_pressure_levels():
    with pytest.raises(ValueError, match="strictly decrease"):
        _finite_profile([1000, 900, 900, 700, 600],
                        [290, 285, 280, 275, 270],
                        [285, 280, 275, 270, 265],
                        [0, 1000, 2000, 3000, 4000])


def test_profile_rejects_nonmonotonic_height():
    with pytest.raises(ValueError, match="increase upward"):
        _finite_profile([1000, 900, 800, 700, 600],
                        [290, 285, 280, 275, 270],
                        [285, 280, 275, 270, 265],
                        [0, 1000, 900, 3000, 4000])
