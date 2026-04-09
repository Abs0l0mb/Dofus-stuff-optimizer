"""
Web interface for the Dofus gear optimizer.
Run with: python app.py
Then open http://localhost:5000 in your browser.
"""

import json
import os
import threading
import requests as req
from flask import Flask, jsonify, request, render_template

from optimizer import optimize, _load_config, fetch_all

DOFUSDB_API  = "https://api.dofusdb.fr"
DOFUSDB_URL  = "https://dofusdb.fr/fr/tools/stuff/creator"

# Optimizer slot name → dofusdb localStorage key
SLOT_TO_DOFUSDB = {
    "hat":      "helmet",
    "amulette": "amulet",
    "ring":     "rings",    # array of 2
    "shield":   "shield",
    "cloak":    "cape",
    "boots":    "boots",
    "belt":     "belt",
    "dofus":    "dofus",    # array of 6
    "weapon":   "weapon",
    "pet":      "pet",
}

# Slots that are stored as arrays in localStorage
ARRAY_SLOTS = {"rings": 2, "dofus": 6}

app = Flask(__name__)

CONFIG_PATH = "config.json"
ITEMS_PATH  = "items.json"


def _load_items():
    if not os.path.exists(ITEMS_PATH):
        return None
    with open(ITEMS_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def get_config():
    if not os.path.exists(CONFIG_PATH):
        return jsonify({"stat_weights": {}, "min_stats": {}, "stat_caps": {}, "excluded_items": [], "min_stat_sums": []})
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    # Ensure all keys exist
    cfg.setdefault("stat_weights", {})
    cfg.setdefault("min_stats", {})
    cfg.setdefault("stat_caps", {})
    cfg.setdefault("excluded_items", [])
    cfg.setdefault("min_stat_sums", [])
    cfg.setdefault("locked_items", [])
    return jsonify(cfg)


@app.route("/api/config", methods=["POST"])
def save_config():
    cfg = request.get_json()
    # Preserve the comment if it exists
    existing = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            existing = json.load(f)
    if "_comment" in existing:
        cfg["_comment"] = existing["_comment"]
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return jsonify({"ok": True})


@app.route("/api/fetch", methods=["POST"])
def fetch_items():
    body       = request.get_json() or {}
    level_min  = int(body.get("level_min", 0))
    level_max  = int(body.get("level_max", 200))
    try:
        data = fetch_all(level_min, level_max)
        with open(ITEMS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return jsonify({"ok": True, "total": len(data["items"])})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/optimize", methods=["POST"])
def run_optimize():
    body = request.get_json()

    data = _load_items()
    if data is None:
        return jsonify({"error": "items.json not found — run Fetch first"}), 400

    cfg = _load_config(CONFIG_PATH)

    # CLI-style overrides from the request body
    stat_weights   = {**cfg["stat_weights"],  **body.get("stat_weights", {})}
    min_stats      = {**cfg["min_stats"],     **body.get("min_stats", {})}
    stat_caps      = {**cfg["stat_caps"],     **body.get("stat_caps", {})}
    excluded       = cfg["excluded_items"] | set(body.get("excluded_items", []))
    min_stat_sums  = body.get("min_stat_sums", cfg.get("min_stat_sums", []))
    locked         = cfg["locked_items"] | set(body.get("locked_items", []))
    target_level   = int(body.get("level", 200))
    use_max        = not body.get("use_avg", False)

    if not stat_weights:
        return jsonify({"error": "No stat weights configured"}), 400

    result = optimize(
        items=data["items"],
        target_level=target_level,
        stat_weights=stat_weights,
        min_stats=min_stats,
        stat_caps=stat_caps,
        excluded_items=excluded,
        use_max_effects=use_max,
        min_stat_sums=min_stat_sums,
        locked_items=locked,
    )

    if "error" in result:
        return jsonify(result), 400

    output_items = []
    for it in sorted(result["items"], key=lambda i: i["slot"]):
        output_items.append({
            "id":      it["id"],
            "slot":         it["slot"],
            "name":         it["name"],
            "level":        it["level"],
            "effects":      it["effects"],
            "set_id":       it["item_set"]["id"]   if it.get("item_set") else None,
            "set_name":     it["item_set"]["name"] if it.get("item_set") else None,
        })

    return jsonify({
        "items":       output_items,
        "stats":       result["stats"],
        "set_bonuses": result["set_bonuses"],
        "objective":   result["objective"],
    })


def _fetch_effects(effect_ids: list) -> dict:
    """
    Batch-fetch Effect objects from the API (paginated).
    Returns {effect_id: effect_dict} with $fromCache: True added,
    matching the format dofusdb.fr stores in localStorage.
    """
    cache = {}
    ids   = list(set(effect_ids))   # deduplicate
    PAGE  = 50

    for i in range(0, len(ids), PAGE):
        chunk  = ids[i:i + PAGE]
        params = [("$limit", PAGE), ("lang", "fr")]
        for eid in chunk:
            params.append(("id[$in][]", eid))
        resp = req.get(f"{DOFUSDB_API}/effects", params=params)
        if resp.ok:
            for e in resp.json().get("data", []):
                cache[e["id"]] = {**e, "$fromCache": True}

    return cache


def _embed_effects(item: dict, effects_cache: dict) -> dict:
    """Embed resolved Effect objects into each possibleEffect entry."""
    for pe in item.get("possibleEffects", []):
        if pe.get("effect") is None:
            eid = pe.get("effectId")
            if eid is not None and eid in effects_cache:
                pe["effect"] = effects_cache[eid]
    return item


def _fetch_full_item(item_id: int) -> dict | None:
    """
    Fetch a single full item object using the list endpoint.
    The list endpoint embeds sub-objects (e.g. effect inside possibleEffects)
    that the single-item endpoint omits, and dofusdb.fr requires them.
    """
    resp = req.get(f"{DOFUSDB_API}/items", params={
        "id[$in][]": item_id,
        "$limit":    1,
        "lang":      "fr",
    })
    if resp.ok:
        data = resp.json().get("data", [])
        return data[0] if data else None
    return None


def _build_stuff(items_in: list, level: int) -> dict:
    """Fetch full API item objects, embed Effect sub-objects, and assemble the dofusdb stuff structure."""
    # 1. Fetch full items
    full_items = {}
    for it in items_in:
        full = _fetch_full_item(it["id"])
        if full:
            full_items[it["id"]] = full
        else:
            print(f"Warning: could not fetch item {it['id']} ({it.get('name', '?')})")

    # 2. Collect all effectIds whose Effect sub-object is null
    missing_effect_ids = set()
    for item in full_items.values():
        for pe in item.get("possibleEffects", []):
            if pe.get("effect") is None and pe.get("effectId") is not None:
                missing_effect_ids.add(pe["effectId"])

    # 3. Batch-fetch and embed Effect objects
    if missing_effect_ids:
        effects_cache = _fetch_effects(list(missing_effect_ids))
        for item in full_items.values():
            _embed_effects(item, effects_cache)

    stuff = {
        "level":           level,
        "helmet":          None,
        "amulet":          None,
        "rings":           [None, None],
        "shield":          None,
        "cape":            None,
        "boots":           None,
        "belt":            None,
        "dofus":           [None] * 6,
        "weapon":          None,
        "pet":             None,
        "base":            None,
        "parchment":       None,
        "breed":           {"id": 1},
        "sexe":            "male",
        "exo":             [None],
        "itemsForgemagie": None,
        "pinned_spells":   [],
        "boosts":          [],
        "stats":           None,
        "weaponSmith":     [],
    }

    ring_idx  = 0
    dofus_idx = 0
    for it in items_in:
        dofusdb_key = SLOT_TO_DOFUSDB.get(it["slot"])
        if not dofusdb_key:
            continue
        full = full_items.get(it["id"])
        if not full:
            continue
        if dofusdb_key == "rings":
            if ring_idx < 2:
                stuff["rings"][ring_idx] = full
                ring_idx += 1
        elif dofusdb_key == "dofus":
            if dofus_idx < 6:
                stuff["dofus"][dofus_idx] = full
                dofus_idx += 1
        else:
            stuff[dofusdb_key] = full

    return stuff


def _playwright_open(stuff: dict):
    """Open a browser via Playwright, inject stuff into localStorage, navigate."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed — run: pip install playwright && playwright install chromium")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page    = browser.new_page()

        # Navigate first so the origin exists and localStorage is accessible
        page.goto(DOFUSDB_URL)
        page.wait_for_load_state("networkidle")

        # Merge into existing localStorage
        page.evaluate("""(stuffJson) => {
            try {
                const data = JSON.parse(localStorage.getItem('dofusdb-local2') || '{}');
                data.stuff = JSON.parse(stuffJson);
                localStorage.setItem('dofusdb-local2', JSON.stringify(data));
            } catch(e) { console.error(e); }
        }""", json.dumps(stuff))

        # Reload so the app picks up the new data
        page.reload()
        page.wait_for_load_state("networkidle")

        # Keep the browser open until the user closes the tab
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass


@app.route("/api/open-dofusdb", methods=["POST"])
def open_dofusdb():
    """Build the stuff object and open it directly in a browser via Playwright."""
    body     = request.get_json()
    items_in = body.get("items", [])
    level    = body.get("level", 200)

    if not items_in:
        return jsonify({"error": "No items provided"}), 400

    try:
        stuff = _build_stuff(items_in, level)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Run Playwright in a background thread so Flask returns immediately
    threading.Thread(target=_playwright_open, args=(stuff,), daemon=True).start()

    return jsonify({"ok": True})


@app.route("/api/export-dofusdb", methods=["POST"])
def export_dofusdb():
    """Return a JS console snippet as a fallback for manual injection."""
    body     = request.get_json()
    items_in = body.get("items", [])
    level    = body.get("level", 200)

    if not items_in:
        return jsonify({"error": "No items provided"}), 400

    try:
        stuff = _build_stuff(items_in, level)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    stuff_json = json.dumps(stuff, ensure_ascii=False)
    snippet = f"""(function() {{
  try {{
    var data = JSON.parse(localStorage.getItem('dofusdb-local2') || '{{}}');
    data.stuff = {stuff_json};
    localStorage.setItem('dofusdb-local2', JSON.stringify(data));
    window.location.href = '{DOFUSDB_URL}';
  }} catch(e) {{ alert('Erreur: ' + e.message); }}
}})();"""

    return jsonify({"snippet": snippet})


@app.route("/api/status", methods=["GET"])
def status():
    data = _load_items()
    return jsonify({
        "items_loaded": data is not None,
        "item_count":   len(data["items"]) if data else 0,
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)
