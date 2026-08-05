"""Great Britain data from Elexon BMRS (Insights Solution).

GB left ENTSO-E Transparency after Brexit — its data moved to Elexon/BMRS — so the dispatch model
carried GB as a border supply/demand curve on FR/BE rather than a balance of its own (see
`dispatch_model/DECISIONS.md`, "Every remaining gap is GB"). That decision recorded sourcing GB from
BMRS as a later refinement; this package is that refinement.

The API is public and needs no key (verified against the live service):

    generation by fuel   GET /bmrs/api/v1/datasets/FUELINST
    wind + solar by PSR  GET /bmrs/api/v1/datasets/AGWS
    demand outturn       GET /bmrs/api/v1/demand/outturn          (INDO / ITSDO)
    market index price   GET /bmrs/api/v1/balancing/pricing/market-index   (MID; GBP/MWh)

Rows are written into the SAME tables as the ENTSO-E feeds, under `series_key = 'GB'`, and — critically
— with ENTSO-E *PSR names* as `sub_key` ("Fossil Gas", "Nuclear", …). `io.entsoe_hist.PSR2TECH` then maps
them exactly as it does for every other zone, so no downstream reader, stack builder or tech taxonomy
needs to know GB came from a different upstream.
"""
from . import series                                                       # noqa: F401

__all__ = ["series"]
