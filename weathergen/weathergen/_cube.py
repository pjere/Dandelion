"""Helpers to flatten the (time, station, variable) cube to a (time, location) matrix
and back, with a stable column ordering shared between fit and simulate.
"""
from __future__ import annotations

import numpy as np
import xarray as xr


def to_matrix(da: xr.DataArray) -> tuple[np.ndarray, list[tuple[str, str]]]:
    """(time, station, variable) -> (time, S*V) ndarray + ordered (station, variable) keys."""
    stations = [str(s) for s in da["station"].values]
    variables = [str(v) for v in da["variable"].values]
    keys = [(s, v) for s in stations for v in variables]  # station-major
    mat = da.transpose("time", "station", "variable").values.reshape(len(da.time), -1)
    return mat, keys


def from_matrix(
    mat: np.ndarray, keys: list[tuple[str, str]], time: np.ndarray
) -> xr.DataArray:
    """Inverse of :func:`to_matrix`."""
    stations = list(dict.fromkeys(k[0] for k in keys))
    variables = list(dict.fromkeys(k[1] for k in keys))
    S, V = len(stations), len(variables)
    arr = mat.reshape(len(time), S, V)
    return xr.DataArray(
        arr, dims=("time", "station", "variable"),
        coords={"time": time, "station": stations, "variable": variables}, name="obs",
    )
