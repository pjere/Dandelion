"""Nuclear capacity trajectories from POLICY, reactor by reactor.

The projection evolves each zone's fleet with the `cap_nuclear_gw` rows of the `dispatch_tyndp` sheet.
Those rows were hand-seeded round numbers, and **DE_LU had none at all** — so `_scale_stack`, which
leaves unlisted techs untouched, carried 7.4 GW of German nuclear into every projected year, seventeen
years after Germany shut its last reactor (measured: installed 9516 MW in 2019 → 4056 in 2022 → absent
from the 2024 and 2025 ENTSO-E reports).

This module derives the trajectory instead of asserting it, from two ingredients per reactor —
commissioning year and capacity — and one rule per country:

* **committed phase-out (DE, ES)** — reactors retire on the dates the phase-out plan fixes, regardless
  of age. Germany's `Atomgesetz` schedule ended 2023-04-15; Spain's 2019 protocol runs 2027→2035.
* **lifetime extension (BE, CH, NL)** — reactors run to `LIFETIME_YEARS` (60), the operating life the
  long-term-operation programmes target. FR is instead handled by `glide_closures` — a partial oldest-first
  phase-out at 60 y, the rest extended, gliding to ~52 GW in 2050 (RTE-N03-consistent). Units that closed
  EARLY for their own reasons carry an explicit `closed` year (Belgium's Doel 3 / Tihange 2, France's
  Fessenheim), which always wins: a historical fact outranks a rule.

The output pre-fills the workbook, so every number stays user-editable — the rule is a *default*, not a
constraint. `scripts/gen_nuclear_trajectory.py` writes it; the projection reads the sheet as before and
needs no code change.
"""
from __future__ import annotations

LIFETIME_YEARS = 60             # operating life for the neighbour extension countries (BE/CH/NL), and the
#                                 fallback for any FR reactor `glide_closures` does not schedule. FRANCE is
#                                 handled by the GLIDE (see `glide_closures`), NOT this flat rule: a strict 60
#                                 forced the fleet to ~18 GW by 2050 (a phase-out no French scenario endorses,
#                                 manufacturing loss-of-load against the PPE3-electrified demand), while a flat
#                                 80 for all held it at ~74 GW — above even RTE's highest scenario. The glide
#                                 partially phases out the oldest reactors at 60 y and extends the rest, landing
#                                 ~52 GW in 2050 (RTE-N03-consistent, extension in lieu of SMRs).

#: Countries whose fleet retires on a POLICY schedule rather than by age.
PHASE_OUT_ZONES = ("DE_LU", "ES")

#: (zone, unit, MW, commissioning year, explicit closure year or None).
#: Capacities are nameplate net; the ENTSO-E installed-capacity series cross-checks the totals
#: (BE 3929 MW in 2025 = Doel 4 + Tihange 3 + Doel 2; CH 2983 = the four Swiss units; ES 7117).
_REACTORS: tuple[tuple[str, str, float, int, int | None], ...] = (
    # --- Belgium: phase-out law repealed 2025; the surviving pair runs on the 60-year rule -----------
    ("BE", "Doel 1", 445.0, 1975, 2025),
    ("BE", "Doel 2", 445.0, 1975, 2025),
    ("BE", "Doel 3", 1006.0, 1982, 2022),          # closed early, phase-out law
    ("BE", "Doel 4", 1039.0, 1985, None),
    ("BE", "Tihange 1", 962.0, 1975, 2025),
    ("BE", "Tihange 2", 1008.0, 1983, 2023),       # closed early, phase-out law
    ("BE", "Tihange 3", 1038.0, 1985, None),
    # --- Switzerland: no statutory end date, operate while safe ⇒ 60-year rule ----------------------
    ("CH", "Beznau 1", 365.0, 1969, None),
    ("CH", "Beznau 2", 365.0, 1971, None),
    ("CH", "Goesgen", 1010.0, 1979, None),
    ("CH", "Leibstadt", 1220.0, 1984, None),
    # --- Spain: committed phase-out, 2019 protocol calendar ----------------------------------------
    ("ES", "Almaraz 1", 1049.0, 1983, 2027),
    ("ES", "Almaraz 2", 1044.0, 1984, 2028),
    ("ES", "Asco 1", 1033.0, 1984, 2030),
    ("ES", "Cofrentes", 1092.0, 1985, 2030),
    ("ES", "Asco 2", 1027.0, 1986, 2032),
    ("ES", "Vandellos 2", 1087.0, 1988, 2035),
    ("ES", "Trillo", 1066.0, 1988, 2035),
    # --- Germany: committed phase-out, completed 2023-04-15. The whole 2019 fleet is listed, not just
    #     the final three: the projection computes its capacity factor as target/REFERENCE-YEAR, so a
    #     table that starts after the reference year yields 0/0 and the tech silently escapes scaling —
    #     which is exactly how 7.4 GW of German nuclear survived into every projected year.
    ("DE_LU", "Philippsburg 2", 1402.0, 1985, 2019),
    ("DE_LU", "Brokdorf", 1410.0, 1986, 2021),
    ("DE_LU", "Grohnde", 1360.0, 1985, 2021),
    ("DE_LU", "Gundremmingen C", 1288.0, 1985, 2021),
    ("DE_LU", "Emsland", 1406.0, 1988, 2023),
    ("DE_LU", "Isar 2", 1485.0, 1988, 2023),
    ("DE_LU", "Neckarwestheim 2", 1400.0, 1989, 2023),
    # --- Netherlands: Borssele, lifetime extended ⇒ 60-year rule -----------------------------------
    ("NL", "Borssele", 485.0, 1973, None),
)


#: FR units the fleet registry leaves without a commissioning year (`avail_fleet_registry` has NaN for
#: six rows). Filled from the public commissioning record — and Fessenheim carries its real closure
#: (Feb/Jun 2020), which the FR stack still ignores: 1.75 GW of French nuclear that no longer exists,
#: the same class of defect as the German 7.4 GW this module was written to remove.
FR_MISSING: dict[str, tuple[int, int | None]] = {
    "ST ALBAN 1": (1985, None), "ST ALBAN 2": (1986, None),
    "ST LAURENT 1": (1981, None), "ST LAURENT 2": (1983, None),
    "FESSENHEIM 1": (1977, 2020), "FESSENHEIM 2": (1977, 2020),
}


#: FR reactors closed by POLITICAL DECISION, with the year they stopped producing. `io.fr_fleet`
#: otherwise infers liveness from observed generation — implicit, and it lags reality by however long
#: the data takes to go quiet. A legislated shutdown is a fact and belongs in the fleet definition.
FR_CLOSURES: dict[str, int] = {"FESSENHEIM 1": 2020, "FESSENHEIM 2": 2020}

#: EPR2 programme — EDF's officially retained schedule, three pairs in the announced build order
#: (Penly, then Gravelines, then Bugey), 1670 MW net per unit. Penly 1's 2038 target is EDF's own
#: revision of the original 2035; the later pairs follow the stated ~2-year pair rhythm and are the
#: least firm part of the schedule. These are TARGETS, pre-filled into `dispatch_nuclear_newbuild` so
#: every date stays editable — the programme has already slipped once and will be revised again.
EPR2: tuple[tuple[str, str, float, int], ...] = (
    ("FR", "EPR2 Penly 1", 1670.0, 2038),
    ("FR", "EPR2 Penly 2", 1670.0, 2040),
    ("FR", "EPR2 Gravelines 1", 1670.0, 2041),
    ("FR", "EPR2 Gravelines 2", 1670.0, 2043),
    ("FR", "EPR2 Bugey 1", 1670.0, 2044),
    ("FR", "EPR2 Bugey 2", 1670.0, 2046),
)


def closure_year(zone: str, commissioning: int, closed: int | None) -> int:
    """The year a reactor stops producing: an explicit closure always wins (historical fact or a
    committed phase-out date), otherwise the `LIFETIME_YEARS` operating life."""
    if closed is not None:
        return int(closed)
    return int(commissioning) + LIFETIME_YEARS


def capacity_mw(zone: str, year: int,
                fr_units: list[tuple[float, int, int | None]] | None = None) -> float:
    """Installed nuclear capacity of `zone` in `year` under the default policy rule.

    `fr_units` = [(capacity_mw, commissioning_year, closed_or_None)] for France, modelled
    reactor-by-reactor from the fleet registry rather than duplicated here (59 units); its rule is the
    60-year lifetime, with explicit closures (Fessenheim) winning as everywhere else.
    """
    if zone == "FR":
        if not fr_units:
            return 0.0
        return sum(mw for mw, com, closed in fr_units if closure_year("FR", com, closed) > year)
    return sum(mw for z, _n, mw, com, closed in _REACTORS
               if z == zone and closure_year(z, com, closed) > year)


def newbuild_mw(zone: str, year: int, newbuild: list[tuple[str, float, int]] | None) -> float:
    """Capacity from units not yet built: online once commissioned, and not subject to the lifetime
    rule inside any horizon we project (a 2038 unit reaches 60 years in 2098)."""
    return sum(mw for z, mw, com in (newbuild or []) if z == zone and int(com) <= year)


def glide_closures(fr_units: list[tuple[float, int, int | None]],
                   newbuild: list[tuple[str, float, int]] | None,
                   target_2050: float = 52.0, hold_until: int = 2037,
                   min_life: int = 60, horizon: int = 2050) -> list[tuple[float, int, int | None]]:
    """Assign each schedulable FR reactor a closure year on a GLIDE PATH, returning `fr_units` with the
    `closed` slot filled.

    The TOTAL fleet (historic + `newbuild` EPR2) holds ~full until `hold_until`, then declines SMOOTHLY to
    `target_2050` GW by `horizon`. Reactors are retired OLDEST-FIRST and never before `min_life` years — a
    partial phase-out: the oldest units close at 60 y while the younger fleet is extended beyond 60 y (so the
    ~52 GW 2050 level is reached by extension in lieu of RTE-N03's SMRs). Reactors an explicit closure already
    fixes (Fessenheim) keep it and are excluded from the schedule; reactors never selected are EXTENDED past
    the horizon (`horizon + 50`) — NOT left to the 60-year fallback, which would retire a young unit reaching
    60 y before 2050 and undershoot the target.
    """
    fixed = [(mw, com, cl) for mw, com, cl in fr_units if cl is not None]
    sched = sorted(((mw, com) for mw, com, cl in fr_units if cl is None), key=lambda t: t[1])  # oldest first
    full_gw = sum(mw for mw, _ in sched) / 1e3

    def epr2_gw(y: int) -> float:
        return sum(mw for z, mw, com in (newbuild or []) if z == "FR" and int(com) <= y) / 1e3

    def target_total(y: int) -> float:                         # target installed nuclear (GW), incl. EPR2
        if y <= hold_until:
            return full_gw + epr2_gw(y)
        f = min((y - hold_until) / (horizon - hold_until), 1.0)
        return full_gw + (target_2050 - full_gw) * f

    closed: dict[int, int] = {}                                # sched index → closure year
    remaining = full_gw
    for y in range(hold_until + 1, horizon + 1):
        while remaining + epr2_gw(y) > target_total(y) + 1e-6:
            i = next((k for k, (mw, com) in enumerate(sched)
                      if k not in closed and y - com >= min_life), None)
            if i is None:
                break                                          # nothing has reached min_life yet — hold
            closed[i] = y
            remaining -= sched[i][0] / 1e3
    return fixed + [(mw, com, closed.get(k, horizon + 50)) for k, (mw, com) in enumerate(sched)]


def trajectory(zones: tuple[str, ...], years: tuple[int, ...],
               fr_units: list[tuple[float, int, int | None]] | None = None,
               newbuild: list[tuple[str, float, int]] | None = None) -> list[dict]:
    """→ `dispatch_tyndp` rows [{zone, variable, year, value}] carrying `cap_nuclear_gw` in GW.

    The value is the EXISTING fleet under the policy rule plus any commissioned new build, so the
    projection keeps reading one total and needs no code change."""
    return [{"zone": z, "variable": "cap_nuclear_gw", "year": int(y),
             "value": round((capacity_mw(z, int(y), fr_units)
                             + newbuild_mw(z, int(y), newbuild)) / 1e3, 3)}
            for z in zones for y in years]
