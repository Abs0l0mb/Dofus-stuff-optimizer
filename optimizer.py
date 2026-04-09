"""
Dofus gear optimizer.

Usage:
  1. Fetch and cache all item data:
       python optimizer.py fetch

  2. Run the optimizer:
       python optimizer.py optimize --level 200 --weights Vitalité:1 Force:2 --min Vitalité:3000
"""

import json
import sys
import requests

# ---------------------------------------------------------------------------
# Equipment slot definitions
# ---------------------------------------------------------------------------

EQUIPMENT_TYPES = {
    1:   "Amulette",
    2:   "Arc",
    3:   "Baguette",
    4:   "Bâton",
    5:   "Dague",
    6:   "Épée",
    7:   "Marteau",
    8:   "Pelle",
    9:   "Anneau",
    10:  "Ceinture",
    11:  "Bottes",
    16:  "Chapeau",
    17:  "Cape",
    18:  "Familier",
    19:  "Hache",
    20:  "Outil",
    21:  "Pioche",
    22:  "Faux",
    23:  "Dofus",
    82:  "Bouclier",
    121: "Montilier",
    151: "Trophée",
    331: "Dragodinde",
    332: "Muldo",
    333: "Volkorne",
}

# Logical slots: (slot_name, max_items, [typeIds that fill this slot])
SLOTS = [
    ("weapon",   1, [2, 3, 4, 5, 6, 7, 8, 19, 20, 21, 22]),
    ("amulette", 1, [1]),
    ("ring",     2, [9]),
    ("belt",     1, [10]),
    ("boots",    1, [11]),
    ("hat",      1, [16]),
    ("cloak",    1, [17]),
    ("shield",   1, [82]),
    ("pet",      1, [18, 121, 331, 332, 333]),
    ("dofus",    6, [23, 151]),  # dofus + trophées share 6 slots
]

BASE_URL = "https://api.dofusdb.fr"


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_characteristics() -> dict:
    """Return {characteristic_id: stat_name_fr}."""
    char_map = {}
    skip = 0
    PAGE = 50
    while True:
        resp = requests.get(f"{BASE_URL}/characteristics", params={
            "$limit": PAGE,
            "$skip":  skip,
            "lang":   "fr",
        })
        resp.raise_for_status()
        body = resp.json()
        for c in body["data"]:
            name = c.get("name", "")
            name_fr = (name.get("fr") if isinstance(name, dict) else name) or f"stat_{c['id']}"
            char_map[c["id"]] = name_fr
        skip += len(body["data"])
        if skip >= body["total"]:
            break
    return char_map


def simplify_effects(raw_effects: list, char_map: dict) -> list:
    result = []
    for e in raw_effects:
        char_id = e.get("characteristic", -1)
        result.append({
            "stat":    char_map.get(char_id, f"stat_{char_id}"),
            "stat_id": char_id,
            "min":     e.get("from", 0),
            "max":     e.get("to", 0),
        })
    return result


def simplify_item_set(raw_set: dict | None, char_map: dict) -> dict | None:
    if not raw_set:
        return None
    bonuses = {}
    for i, piece_effects in enumerate(raw_set.get("effects", [])):
        piece_count = i + 1
        if piece_effects:
            bonuses[piece_count] = simplify_effects(piece_effects, char_map)
    return {
        "id":      raw_set["id"],
        "name":    raw_set.get("name", {}).get("fr", ""),
        "items":   raw_set.get("items", []),
        "bonuses": bonuses,
    }


def fetch_items_by_type(type_id: int, level_min: int, level_max: int, char_map: dict) -> list:
    PAGE = 50
    items = []
    skip  = 0

    while True:
        resp = requests.get(f"{BASE_URL}/items", params={
            "$limit":        PAGE,
            "$sort[level]":  -1,
            "$sort[id]":     -1,
            "typeId[$in][]": type_id,
            "level[$gte]":   level_min,
            "level[$lte]":   level_max,
            "$skip":         skip,
            "etheral":       "false",
            "lang":          "fr",
        })
        resp.raise_for_status()
        body = resp.json()

        for item in body["data"]:
            criterions = item.get("criterions", "")
            items.append({
                "id":        item["id"],
                "name":      item.get("name", {}).get("fr", ""),
                "level":     item["level"],
                "type_id":   type_id,
                "slot":      EQUIPMENT_TYPES.get(type_id, f"type_{type_id}"),
                "effects":   simplify_effects(item.get("effects", []), char_map),
                "item_set":  simplify_item_set(item.get("itemSet"), char_map),
                "pk_less_3": "Pk<3" in criterions,
            })

        skip += PAGE
        if skip >= body["total"]:
            break

    return items


def fetch_all(level_min: int = 0, level_max: int = 200) -> dict:
    print("Fetching characteristics...")
    char_map = fetch_characteristics()
    print(f"  -> {len(char_map)} characteristics")

    all_items = []
    for type_id, slot_name in EQUIPMENT_TYPES.items():
        print(f"Fetching {slot_name} (typeId={type_id})...")
        items = fetch_items_by_type(type_id, level_min, level_max, char_map)
        print(f"  -> {len(items)} items")
        all_items.extend(items)

    print(f"\nTotal: {len(all_items)} items")
    return {"characteristics": char_map, "items": all_items}


# ---------------------------------------------------------------------------
# Optimizer
# ---------------------------------------------------------------------------

def _effect_value(effect: dict, use_max: bool = True) -> float:
    """
    Return the usable numeric value of an effect.
    Fixed effects store their value in 'min' with 'max'=0.
    Variable effects have a range [min, max].
    """
    lo, hi = effect["min"], effect["max"]
    if hi == 0:
        return lo          # fixed value
    return hi if use_max else (lo + hi) / 2


def _item_stat_value(item: dict, stat_name: str, use_max: bool = True) -> float:
    """Sum of all effect values for a given stat on one item."""
    return sum(
        _effect_value(e, use_max)
        for e in item["effects"]
        if e["stat"] == stat_name
    )


def optimize(
    items: list,
    target_level: int,
    stat_weights: dict,           # {stat_name: float}  — how much each stat point is worth
    min_stats: dict = None,       # {stat_name: float}  — hard minimums per stat
    stat_caps: dict = None,       # {stat_name: float}  — maximum useful value from gear
    excluded_items: set = None,   # set of item names to never equip
    use_max_effects: bool = True,
    min_stat_sums: list = None,   # [{"stats": [...], "min": value}, ...]
    locked_items: set = None,     # set of item names to force-equip
) -> dict:
    """
    Solve the gear optimization problem as a Mixed Integer Program.

    Returns {"items": [...chosen items...], "stats": {...total stats...},
             "set_bonuses": [...active set bonuses...]}
    """
    try:
        import pulp
    except ImportError:
        sys.exit("pulp is required for optimization. Install it with: pip install pulp")

    min_stats      = min_stats or {}
    stat_caps      = stat_caps or {}
    excluded_items = excluded_items or set()
    min_stat_sums  = min_stat_sums or []
    locked_items   = locked_items or set()

    # --- Filter items to valid level, excluding blacklisted items ---
    pool = [
        it for it in items
        if it["level"] <= target_level and it["name"] not in excluded_items
    ]

    # --- Assign each item to its logical slot ---
    slot_of = {}
    for slot_name, _, type_ids in SLOTS:
        for it in pool:
            if it["type_id"] in type_ids:
                slot_of[it["id"]] = slot_name

    # Drop items that don't map to any slot (shouldn't happen with clean data)
    pool = [it for it in pool if it["id"] in slot_of]

    # --- Collect all stat names that matter ---
    all_stats = set(stat_weights) | set(min_stats)

    # --- Build set info ---
    # set_id -> list of item ids in pool that belong to this set
    sets: dict[int, list[int]] = {}
    set_bonuses: dict[int, dict] = {}  # set_id -> {pieces: [{stat, value}]}
    for it in pool:
        if it["item_set"]:
            sid = it["item_set"]["id"]
            sets.setdefault(sid, [])
            if it["id"] in it["item_set"]["items"]:
                sets[sid].append(it["id"])
            set_bonuses[sid] = {int(k): v for k, v in it["item_set"]["bonuses"].items()}

    # --- Build MIP ---
    prob = pulp.LpProblem("dofus_gear", pulp.LpMaximize)

    # Binary variable per item: 1 = equipped
    x = {it["id"]: pulp.LpVariable(f"x_{it['id']}", cat="Binary") for it in pool}

    # Set-state variables: y[set_id][k] = 1 iff exactly k pieces of that set equipped
    y = {}
    for sid, item_ids in sets.items():
        max_k = len(item_ids)
        y[sid] = {
            k: pulp.LpVariable(f"y_{sid}_{k}", cat="Binary")
            for k in range(1, max_k + 1)
        }

    # --- Slot capacity constraints ---
    slot_items: dict[str, list] = {}
    for it in pool:
        slot_items.setdefault(slot_of[it["id"]], []).append(it["id"])

    print("\n[debug] Items in pool per slot:")
    for slot_name, max_count, _ in SLOTS:
        iids = slot_items.get(slot_name, [])
        print(f"  {slot_name:12} {len(iids):5} items  (need {min(max_count, len(iids))})")
        if not iids:
            continue
        available = len(iids)
        required  = min(max_count, available)
        # Force exactly `required` items to be equipped for this slot.
        # This guarantees every slot is filled rather than relying on
        # the optimizer caring about items with 0 weighted contribution.
        prob += pulp.lpSum(x[iid] for iid in iids) == required, f"slot_{slot_name}"

    # --- Locked item constraints ---
    for it in pool:
        if it["name"] in locked_items:
            prob += x[it["id"]] == 1, f"lock_{it['id']}"

    # Debug: sets with 2+ available items (can contribute an active-set bonus)
    print("\n[debug] Sets with 2+ available items (candidates for set bonus):")
    set_names = {}
    for it in pool:
        if it["item_set"]:
            set_names[it["item_set"]["id"]] = it["item_set"]["name"]
    for sid, item_ids in sorted(sets.items(), key=lambda kv: -len(kv[1])):
        if len(item_ids) >= 2:
            name = set_names.get(sid, f"set_{sid}")
            slots_used = [slot_of[iid] for iid in item_ids]
            print(f"  [{len(item_ids)}] {name} — slots: {slots_used}")

    # --- Set-state constraints ---
    for sid, item_ids in sets.items():
        # Σ_k k*y[s,k] = Σ_{i∈s} x[i]
        prob += (
            pulp.lpSum(k * y[sid][k] for k in y[sid])
            == pulp.lpSum(x[iid] for iid in item_ids if iid in x),
            f"set_count_{sid}",
        )
        # Σ_k y[s,k] ≤ 1
        prob += pulp.lpSum(y[sid].values()) <= 1, f"set_state_{sid}"

    # --- Pk<3 trophy constraints ---
    # In Dofus, Pk = Σ_s (pieces_in_set - 1) for every set with ≥2 pieces.
    # "Pk<3" means this sum must be strictly less than 3, i.e. Pk ≤ 2.
    # This allows:  1×3 pieces from one set  → Pk = (3-1)       = 2 ✓
    #               2×2 pieces from two sets  → Pk = (2-1)+(2-1) = 2 ✓
    # but not:      2×3 pieces               → Pk = (3-1)+(3-1) = 4 ✗
    #               1×3 + 1×2                → Pk = (3-1)+(2-1) = 3 ✗
    #
    # Expressed via y variables:  Σ_s Σ_{k≥2} (k-1)*y[s][k] ≤ 2
    pk_items = [it for it in pool if it.get("pk_less_3")]
    if pk_items:
        eligible_sids = [sid for sid in y if any(k >= 2 for k in y[sid])]
        N = sum(max(y[sid].keys()) - 1 for sid in eligible_sids)  # tight big-M
        for pk_item in pk_items:
            prob += (
                pulp.lpSum((k - 1) * y[sid][k]
                           for sid in eligible_sids
                           for k in y[sid] if k >= 2)
                <= 2 + N * (1 - x[pk_item["id"]]),
                f"pk3_{pk_item['id']}",
            )

    # --- Objective & min-stat constraints ---
    # Build stat expression: item contributions + set bonus contributions
    def stat_expr(stat_name):
        item_contrib = pulp.lpSum(
            _item_stat_value(it, stat_name, use_max_effects) * x[it["id"]]
            for it in pool
            if any(e["stat"] == stat_name for e in it["effects"])
        )
        set_contrib = pulp.lpSum(
            (
                sum(
                    _effect_value(e, use_max_effects)
                    for e in bonus_effects
                    if e["stat"] == stat_name
                )
            ) * y[sid][k]
            for sid, piece_bonuses in set_bonuses.items()
            for k, bonus_effects in piece_bonuses.items()
            if k in y.get(sid, {})
            if any(e["stat"] == stat_name for e in bonus_effects)
        )
        return item_contrib + set_contrib

    prob += pulp.lpSum(
        weight * stat_expr(stat_name)
        for stat_name, weight in stat_weights.items()
    ), "objective"

    for stat_name, min_val in min_stats.items():
        prob += stat_expr(stat_name) >= min_val, f"min_{stat_name}"

    for i, entry in enumerate(min_stat_sums):
        stats_in_sum = entry["stats"]
        min_val      = entry["min"]
        prob += (
            pulp.lpSum(stat_expr(s) for s in stats_in_sum) >= min_val,
            f"min_sum_{i}",
        )

    for stat_name, cap_val in stat_caps.items():
        prob += stat_expr(stat_name) <= cap_val, f"cap_{stat_name}"

    # --- Solve ---
    solver = pulp.PULP_CBC_CMD(msg=0)
    prob.solve(solver)

    if pulp.LpStatus[prob.status] not in ("Optimal", "Feasible"):
        return {"error": f"No solution found (status: {pulp.LpStatus[prob.status]})"}

    # --- Extract results ---
    chosen_items = [it for it in pool if pulp.value(x[it["id"]]) > 0.5]

    # Compute actual stat totals
    total_stats: dict[str, float] = {}
    for stat_name in all_stats:
        total_stats[stat_name] = sum(
            _item_stat_value(it, stat_name, use_max_effects) for it in chosen_items
        )

    # Debug: show Pk<3 status
    if pk_items:
        pk_score = sum(
            (k - 1)
            for sid in eligible_sids
            for k in y[sid] if k >= 2 and pulp.value(y[sid][k]) is not None and pulp.value(y[sid][k]) > 0.5
        )
        pk_equipped = [it["name"] for it in pk_items if pulp.value(x[it["id"]]) > 0.5]
        print(f"\n[debug] Pk<3 trophies equipped: {pk_equipped}")
        print(f"[debug] Pk score: {pk_score} / 2 allowed")

    # Add set bonus contributions
    active_set_bonuses = []
    for sid in y:
        for k, yvar in y[sid].items():
            if pulp.value(yvar) and pulp.value(yvar) > 0.5:
                bonus_effects = set_bonuses[sid].get(k, [])
                if bonus_effects:
                    set_name = next(
                        (it["item_set"]["name"] for it in chosen_items if it["item_set"] and it["item_set"]["id"] == sid),
                        f"Set {sid}",
                    )
                    active_set_bonuses.append({
                        "set": set_name,
                        "pieces": k,
                        "bonuses": bonus_effects,
                    })
                    for e in bonus_effects:
                        stat = e["stat"]
                        total_stats[stat] = total_stats.get(stat, 0) + _effect_value(e, use_max_effects)

    return {
        "items": chosen_items,
        "stats": total_stats,
        "set_bonuses": active_set_bonuses,
        "objective": pulp.value(prob.objective),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_kv_list(args: list[str]) -> dict:
    """Parse ['Key:value', ...] into {key: float(value)}."""
    result = {}
    for kv in args:
        k, v = kv.split(":", 1)
        result[k.strip()] = float(v.strip())
    return result


def cmd_fetch(args):
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--min-level", type=int, default=0)
    p.add_argument("--max-level", type=int, default=200)
    p.add_argument("--output", default="items.json")
    ns = p.parse_args(args)

    data = fetch_all(ns.min_level, ns.max_level)
    with open(ns.output, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Saved to {ns.output}")


def _load_config(path: str = "config.json") -> dict:
    """Load config.json. Returns {"stat_weights": {}, "min_stats": {}} on missing file."""
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        return {
            "stat_weights":   {k: float(v) for k, v in cfg.get("stat_weights", {}).items()},
            "min_stats":      {k: float(v) for k, v in cfg.get("min_stats", {}).items()},
            "stat_caps":      {k: float(v) for k, v in cfg.get("stat_caps", {}).items()},
            "excluded_items": set(cfg.get("excluded_items", [])),
            "min_stat_sums":  cfg.get("min_stat_sums", []),
            "locked_items":   set(cfg.get("locked_items", [])),
        }
    except FileNotFoundError:
        return {"stat_weights": {}, "min_stats": {}, "stat_caps": {}, "excluded_items": set(), "min_stat_sums": [], "locked_items": set()}


def cmd_optimize(args):
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--data",    default="items.json")
    p.add_argument("--config",  default="config.json")
    p.add_argument("--level",   type=int, required=True, help="Max item level")
    p.add_argument("--weights", nargs="+", default=[], metavar="Stat:value",
                   help="Override config weights, e.g. Vitalité:1 Force:2")
    p.add_argument("--min",     nargs="+", default=[], metavar="Stat:value",
                   help="Override config min stats")
    p.add_argument("--avg",     action="store_true",
                   help="Use average of min/max effects instead of max")
    ns = p.parse_args(args)

    with open(ns.data, encoding="utf-8") as f:
        data = json.load(f)

    # Load config, then let CLI args override individual entries
    cfg          = _load_config(ns.config)
    stat_weights = {**cfg["stat_weights"], **_parse_kv_list(ns.weights)}
    min_stats    = {**cfg["min_stats"],    **_parse_kv_list(ns.min)}

    if not stat_weights:
        sys.exit("No stat weights found. Add them to config.json or pass --weights Stat:value")

    print(f"Optimizing for level {ns.level}...")
    result = optimize(
        items=data["items"],
        target_level=ns.level,
        stat_weights=stat_weights,
        min_stats=min_stats,
        stat_caps=cfg["stat_caps"],
        excluded_items=cfg["excluded_items"],
        use_max_effects=not ns.avg,
        min_stat_sums=cfg["min_stat_sums"],
        locked_items=cfg["locked_items"],
    )

    if "error" in result:
        print(f"ERROR: {result['error']}")
        return

    print("\n=== Chosen gear ===")
    for it in sorted(result["items"], key=lambda i: i["slot"]):
        effects_str = ", ".join(
            f"{e['stat']} {_effect_value(e):.0f}"
            for e in it["effects"] if e["stat"] in stat_weights
        )
        print(f"  [{it['slot']:10}] Lv{it['level']:3}  {it['name']}  ({effects_str})")

    if result["set_bonuses"]:
        print("\n=== Active set bonuses ===")
        for sb in result["set_bonuses"]:
            bonuses_str = ", ".join(f"{e['stat']} {_effect_value(e):.0f}" for e in sb["bonuses"])
            print(f"  {sb['set']} ({sb['pieces']} pièces): {bonuses_str}")

    print("\n=== Total stats ===")
    for stat, val in sorted(result["stats"].items()):
        print(f"  {stat}: {val:.0f}")

    print(f"\nObjective score: {result['objective']:.1f}")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("fetch", "optimize"):
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    rest = sys.argv[2:]
    if cmd == "fetch":
        cmd_fetch(rest)
    elif cmd == "optimize":
        cmd_optimize(rest)
