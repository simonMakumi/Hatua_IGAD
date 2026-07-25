"""
The anticipatory action library.

Why a fixed library instead of letting the model write actions freehand
----------------------------------------------------------------------
Because a language model asked "what should this pastoralist do about the
drought" will produce something plausible, fluent, and occasionally ruinous.
"Sell your livestock now" is correct advice in month one of a failing season
and catastrophic in month four, when everyone is selling and prices have
collapsed — it converts a recoverable shock into permanent destitution.

So the model does not invent actions. It **selects** from actions that already
exist in published anticipatory action protocols, and the Verifier rejects any
advisory citing an ``action_id`` that is not in this file. The model's job is
judgement about which action fits, not authorship of what people should do
with their livelihoods.

Sources these are drawn from: IGAD/ICPAC anticipatory action frameworks, FAO
Early Warning Early Action livestock protocols, WFP and IFRC/Red Cross Early
Action Protocols for the Horn of Africa, and Kenya NDMA drought phase
classification guidance.

The ``no_regret`` flag is the safety property that matters most. Actions
flagged no-regret remain beneficial even if the forecast is wrong. At LOW and
MODERATE confidence the Action Planner is restricted to no-regret actions only
— because at 45% confidence, telling a family to destock is a coin-flip with
their assets.
"""

from __future__ import annotations

from ..models import ActorType, AnticipatoryAction, HazardType, Severity

# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------

_LIBRARY: list[dict] = [
    # ---------------------- DROUGHT: pastoralist ----------------------
    {
        "action_id": "dr_past_water_survey",
        "hazard": HazardType.DROUGHT,
        "actor": ActorType.PASTORALIST,
        "instruction": "Check which water points on your migration route still "
                       "have water before you move the herd.",
        "rationale": "Herds die on the route, not at the destination. "
                     "Confirming water before departure is the single highest-"
                     "value pre-move action.",
        "deadline_days": 14,
        "no_regret": True,
        "reversible": True,
        "min_severity": Severity.ADVISORY,
    },
    {
        "action_id": "dr_past_early_migration",
        "hazard": HazardType.DROUGHT,
        "actor": ActorType.PASTORALIST,
        "instruction": "Move the herd to dry-season grazing earlier than usual, "
                       "before pasture on the normal route is exhausted.",
        "rationale": "Moving early means arriving while forage and water remain "
                     "and before congestion at the destination.",
        "deadline_days": 30,
        "no_regret": False,
        "reversible": True,
        "min_severity": Severity.WATCH,
    },
    {
        "action_id": "dr_past_destock_weak",
        "hazard": HazardType.DROUGHT,
        "actor": ActorType.PASTORALIST,
        "instruction": "Sell weak and older animals now, while market prices "
                       "are still holding. Keep breeding females.",
        "rationale": "Prices collapse once distress selling becomes general. "
                     "Selling early converts animals that would likely die into "
                     "cash; keeping the breeding core protects recovery.",
        "deadline_days": 45,
        "no_regret": False,
        "reversible": False,
        "min_severity": Severity.WARNING,
    },
    {
        "action_id": "dr_past_supplementary_feed",
        "hazard": HazardType.DROUGHT,
        "actor": ActorType.PASTORALIST,
        "instruction": "Buy or reserve supplementary feed for breeding females "
                       "and young stock now.",
        "rationale": "Feed prices rise sharply once drought is widely "
                     "recognised. Protecting the breeding core preserves the "
                     "herd's ability to rebuild.",
        "deadline_days": 30,
        "no_regret": True,
        "reversible": True,
        "min_severity": Severity.WATCH,
    },
    {
        "action_id": "dr_past_vaccinate",
        "hazard": HazardType.DROUGHT,
        "actor": ActorType.PASTORALIST,
        "instruction": "Vaccinate and deworm the herd before moving. Weakened "
                       "animals concentrating at water points spread disease.",
        "rationale": "Drought mortality is frequently disease at congested "
                     "water points rather than thirst or starvation directly.",
        "deadline_days": 21,
        "no_regret": True,
        "reversible": True,
        "min_severity": Severity.ADVISORY,
    },
    # ---------------------- DROUGHT: farmer ----------------------
    {
        "action_id": "dr_farm_drought_seed",
        "hazard": HazardType.DROUGHT,
        "actor": ActorType.FARMER,
        "instruction": "Switch to short-maturing or drought-tolerant seed "
                       "varieties for the coming planting season.",
        "rationale": "Short-cycle varieties can complete their cycle within a "
                     "shortened rainy season; long-cycle varieties fail entirely.",
        "deadline_days": 30,
        "no_regret": True,
        "reversible": True,
        "min_severity": Severity.ADVISORY,
    },
    {
        "action_id": "dr_farm_delay_planting",
        "hazard": HazardType.DROUGHT,
        "actor": ActorType.FARMER,
        "instruction": "Delay planting until rainfall is established. Do not "
                       "plant on the first showers alone.",
        "rationale": "False starts destroy seed stock. A failed first planting "
                     "often leaves no seed for the real onset.",
        "deadline_days": 21,
        "no_regret": True,
        "reversible": True,
        "min_severity": Severity.WATCH,
    },
    {
        "action_id": "dr_farm_water_harvest",
        "hazard": HazardType.DROUGHT,
        "actor": ActorType.FARMER,
        "instruction": "Repair water harvesting structures and storage tanks "
                       "before the rains, however light they are.",
        "rationale": "Capturing a poor season's rainfall is only possible if "
                     "storage is already functional when it falls.",
        "deadline_days": 30,
        "no_regret": True,
        "reversible": True,
        "min_severity": Severity.ADVISORY,
    },
    # ---------------------- DROUGHT: household ----------------------
    {
        "action_id": "dr_hh_water_storage",
        "hazard": HazardType.DROUGHT,
        "actor": ActorType.HOUSEHOLD,
        "instruction": "Fill and cover all household water storage now. "
                       "Prioritise drinking water for children.",
        "rationale": "Storage filled before shortage costs nothing; water "
                     "bought during shortage costs many times more.",
        "deadline_days": 7,
        "no_regret": True,
        "reversible": True,
        "min_severity": Severity.ADVISORY,
    },
    {
        "action_id": "dr_hh_food_stock",
        "hazard": HazardType.DROUGHT,
        "actor": ActorType.HOUSEHOLD,
        "instruction": "Buy and store staple food now if you can. Prices "
                       "typically rise sharply once shortage is general.",
        "rationale": "Staple prices in drought-affected markets commonly rise "
                     "well above normal within weeks of a failed season.",
        "deadline_days": 21,
        "no_regret": True,
        "reversible": True,
        "min_severity": Severity.WATCH,
    },
    # ---------------------- FLOOD: household ----------------------
    {
        "action_id": "fl_hh_move_higher",
        "hazard": HazardType.FLOOD,
        "actor": ActorType.HOUSEHOLD,
        "instruction": "Move to higher ground now. Take documents, medicine "
                       "and drinking water.",
        "rationale": "Flood deaths concentrate among people who moved after "
                     "water had already risen and routes were cut.",
        "deadline_days": 1,
        "no_regret": False,
        "reversible": True,
        "min_severity": Severity.WARNING,
    },
    {
        "action_id": "fl_hh_protect_documents",
        "hazard": HazardType.FLOOD,
        "actor": ActorType.HOUSEHOLD,
        "instruction": "Put identity documents, land papers and phone in a "
                       "sealed bag somewhere high in the house.",
        "rationale": "Lost documentation blocks access to aid, land claims and "
                     "banking for months or years after the water recedes.",
        "deadline_days": 2,
        "no_regret": True,
        "reversible": True,
        "min_severity": Severity.ADVISORY,
    },
    {
        "action_id": "fl_hh_avoid_crossing",
        "hazard": HazardType.FLOOD,
        "actor": ActorType.HOUSEHOLD,
        "instruction": "Do not cross flowing water on foot or by vehicle, even "
                       "if it looks shallow.",
        "rationale": "Attempted crossings are a leading cause of flood deaths. "
                     "Moving water conceals depth and washed-out roadbed.",
        "deadline_days": 1,
        "no_regret": True,
        "reversible": True,
        "min_severity": Severity.ADVISORY,
    },
    {
        "action_id": "fl_hh_water_treatment",
        "hazard": HazardType.FLOOD,
        "actor": ActorType.HOUSEHOLD,
        "instruction": "Boil or treat all drinking water. Assume wells and "
                       "boreholes are contaminated after flooding.",
        "rationale": "Cholera and other diarrhoeal outbreaks follow flooding "
                     "through contaminated shallow water sources.",
        "deadline_days": 3,
        "no_regret": True,
        "reversible": True,
        "min_severity": Severity.ADVISORY,
    },
    # ---------------------- FLOOD: pastoralist / farmer ----------------------
    {
        "action_id": "fl_past_move_livestock",
        "hazard": HazardType.FLOOD,
        "actor": ActorType.PASTORALIST,
        "instruction": "Move livestock off the floodplain to high ground now.",
        "rationale": "Livestock cannot be moved once routes flood, and drowned "
                     "herds are an unrecoverable asset loss.",
        "deadline_days": 2,
        "no_regret": False,
        "reversible": True,
        "min_severity": Severity.WATCH,
    },
    {
        "action_id": "fl_farm_early_harvest",
        "hazard": HazardType.FLOOD,
        "actor": ActorType.FARMER,
        "instruction": "Harvest any mature or near-mature crop immediately, "
                       "even slightly early.",
        "rationale": "A slightly early harvest recovers most of the value; a "
                     "flooded field recovers none.",
        "deadline_days": 3,
        "no_regret": False,
        "reversible": False,
        "min_severity": Severity.WATCH,
    },
    {
        "action_id": "fl_farm_raise_stores",
        "hazard": HazardType.FLOOD,
        "actor": ActorType.FARMER,
        "instruction": "Raise stored grain and seed off the floor, onto "
                       "platforms or into raised stores.",
        "rationale": "Seed stock lost to flooding removes the ability to plant "
                     "the following season, extending the crisis by a year.",
        "deadline_days": 2,
        "no_regret": True,
        "reversible": True,
        "min_severity": Severity.ADVISORY,
    },
    # ---------------------- HEAVY RAIN ----------------------
    {
        "action_id": "hr_hh_secure_shelter",
        "hazard": HazardType.HEAVY_RAIN,
        "actor": ActorType.HOUSEHOLD,
        "instruction": "Secure roofing and clear drainage around the house "
                       "before the rain arrives.",
        "rationale": "Most storm damage to informal shelter is preventable with "
                     "an hour of work beforehand.",
        "deadline_days": 3,
        "no_regret": True,
        "reversible": True,
        "min_severity": Severity.ADVISORY,
    },
    # ---------------------- HEAT STRESS ----------------------
    {
        "action_id": "ht_past_shade_water",
        "hazard": HazardType.HEAT_STRESS,
        "actor": ActorType.PASTORALIST,
        "instruction": "Water livestock early morning and late evening, and "
                       "keep them in shade during the middle of the day.",
        "rationale": "Heat mortality in livestock concentrates in midday "
                     "movement and grazing.",
        "deadline_days": 2,
        "no_regret": True,
        "reversible": True,
        "min_severity": Severity.ADVISORY,
    },
    {
        "action_id": "ht_hh_protect_vulnerable",
        "hazard": HazardType.HEAT_STRESS,
        "actor": ActorType.HOUSEHOLD,
        "instruction": "Keep children, elderly people and pregnant women out "
                       "of direct sun and increase drinking water.",
        "rationale": "Heat illness concentrates in these groups and is almost "
                     "entirely preventable.",
        "deadline_days": 2,
        "no_regret": True,
        "reversible": True,
        "min_severity": Severity.ADVISORY,
    },
    # ---------------------- FOOD INSECURITY ----------------------
    {
        "action_id": "fi_hh_nutrition_screening",
        "hazard": HazardType.FOOD_INSECURITY,
        "actor": ActorType.HOUSEHOLD,
        "instruction": "Take children under five to the nearest health facility "
                       "for nutrition screening.",
        "rationale": "Acute malnutrition caught early is treatable at home; "
                     "caught late it requires inpatient care and is often fatal.",
        "deadline_days": 14,
        "no_regret": True,
        "reversible": True,
        "min_severity": Severity.ADVISORY,
    },
    # ---------------------- COUNTY / RESPONDER ----------------------
    {
        "action_id": "ops_county_preposition_water",
        "hazard": HazardType.DROUGHT,
        "actor": ActorType.COUNTY_OFFICER,
        "instruction": "Pre-position water trucking contracts and repair "
                       "strategic boreholes in the affected wards.",
        "rationale": "Procurement lead times mean contracts signed after a "
                     "declaration deliver water weeks after it was needed.",
        "deadline_days": 30,
        "no_regret": True,
        "reversible": True,
        "min_severity": Severity.WATCH,
    },
    {
        "action_id": "ops_county_activate_eoc",
        "hazard": HazardType.FLOOD,
        "actor": ActorType.COUNTY_OFFICER,
        "instruction": "Activate the county emergency operations centre and "
                       "confirm evacuation routes and reception sites.",
        "rationale": "Evacuation routes must be confirmed passable before they "
                     "are needed, not during the event.",
        "deadline_days": 3,
        "no_regret": True,
        "reversible": True,
        "min_severity": Severity.WARNING,
    },
    {
        "action_id": "ops_ngo_preposition_supplies",
        "hazard": HazardType.FLOOD,
        "actor": ActorType.NGO_RESPONDER,
        "instruction": "Pre-position shelter, water treatment and dignity kits "
                       "upstream of the forecast flood extent.",
        "rationale": "Supplies positioned behind a cut road are unavailable "
                     "exactly when they are needed.",
        "deadline_days": 5,
        "no_regret": True,
        "reversible": True,
        "min_severity": Severity.WATCH,
    },
    {
        "action_id": "ops_health_cholera_readiness",
        "hazard": HazardType.DISEASE_OUTBREAK,
        "actor": ActorType.HEALTH_WORKER,
        "instruction": "Check oral rehydration and water treatment stocks and "
                       "brief community health volunteers on cholera signs.",
        "rationale": "Case fatality in diarrhoeal outbreaks depends almost "
                     "entirely on how quickly rehydration begins.",
        "deadline_days": 7,
        "no_regret": True,
        "reversible": True,
        "min_severity": Severity.ADVISORY,
    },
    {
        "action_id": "ops_county_livestock_offtake",
        "hazard": HazardType.DROUGHT,
        "actor": ActorType.COUNTY_OFFICER,
        "instruction": "Open commercial destocking and slaughter-offtake "
                       "programmes before market prices collapse.",
        "rationale": "Offtake launched after prices fall transfers far less "
                     "value to herders and costs the same to run.",
        "deadline_days": 45,
        "no_regret": False,
        "reversible": False,
        "min_severity": Severity.WARNING,
    },
]

# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

ACTIONS: dict[str, AnticipatoryAction] = {
    a["action_id"]: AnticipatoryAction(
        action_id=a["action_id"],
        actor=a["actor"],
        instruction=a["instruction"],
        rationale=a["rationale"],
        deadline_days=a["deadline_days"],
        no_regret=a["no_regret"],
        reversible=a["reversible"],
    )
    for a in _LIBRARY
}

_META: dict[str, dict] = {a["action_id"]: a for a in _LIBRARY}


def exists(action_id: str) -> bool:
    """Used by the Verifier. An advisory citing an unknown action is blocked."""
    return action_id in ACTIONS


def candidates(
    hazard: HazardType,
    severity: Severity,
    *,
    no_regret_only: bool = False,
    actors: list[ActorType] | None = None,
) -> list[AnticipatoryAction]:
    """Actions eligible for this hazard at this severity.

    ``no_regret_only`` is set by the Action Planner whenever confidence is
    LOW or MODERATE. This is the mechanism that stops a 45%-confidence drought
    signal from telling a family to sell its breeding stock.
    """
    out: list[AnticipatoryAction] = []
    for action_id, meta in _META.items():
        if meta["hazard"] != hazard:
            continue
        if severity.rank < meta["min_severity"].rank:
            continue
        if no_regret_only and not meta["no_regret"]:
            continue
        if actors and meta["actor"] not in actors:
            continue
        out.append(ACTIONS[action_id])
    return out


def catalogue_for_prompt(
    hazard: HazardType, severity: Severity, *, no_regret_only: bool = False
) -> str:
    """Render eligible actions for the Action Planner prompt.

    The model sees only actions it is permitted to select. It cannot choose a
    destocking action at moderate confidence because that action is not in the
    list it is shown — the constraint is enforced before generation, not
    policed afterwards.
    """
    eligible = candidates(hazard, severity, no_regret_only=no_regret_only)
    if not eligible:
        return "(no eligible actions for this hazard and severity)"
    lines = []
    for a in eligible:
        flag = "no-regret" if _META[a.action_id]["no_regret"] else "committing"
        lines.append(
            f"- {a.action_id} [{a.actor.value}, {flag}, "
            f"act within {a.deadline_days}d]: {a.instruction}"
        )
    return "\n".join(lines)


def stats() -> dict[str, int]:
    return {
        "total": len(ACTIONS),
        "no_regret": sum(1 for m in _META.values() if m["no_regret"]),
        "hazards": len({m["hazard"] for m in _META.values()}),
        "actors": len({m["actor"] for m in _META.values()}),
    }
