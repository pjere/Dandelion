"""res_model.transfer — station→site wind bridge (D1) and GHI processing (D3)."""
from .ghi import ghi_from_cloud, national_ghi, station_ghi
from .wind import WindTransfer, apply_wind_transfer, fit_wind_transfer

__all__ = ["ghi_from_cloud", "national_ghi", "station_ghi",
           "WindTransfer", "fit_wind_transfer", "apply_wind_transfer"]
