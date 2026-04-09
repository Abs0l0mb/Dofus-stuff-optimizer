# Dofus Stuff Optimizer

Automatically picks the best gear combination for a given level in Dofus, using **Mixed Integer Programming** (MIP) to find the mathematically optimal set of items across all equipment slots — including set bonuses and trophy restrictions.

---

## How it works

### 1. Data fetching

Item data is pulled from the [dofusdb API](https://api.dofusdb.fr) and cached locally in `items.json`. For each item the optimizer keeps:

- Name, level, slot type
- Effects (stat bonuses with min/max range, all correctly named from the characteristics endpoint)
- Item set info: which other items are in the set, and what bonuses are unlocked per number of pieces equipped

### 2. Optimization (MIP)

The optimizer solves a **Binary Integer Program** with one binary variable per item (`1` = equipped, `0` = not). It maximizes a weighted score across all stats, subject to:

- **Slot constraints** — exactly 1 weapon, 2 rings, 6 dofus/trophées, etc. All slots are always filled.
- **Set bonus modeling** — auxiliary variables track exactly how many pieces of each set are equipped and add the correct cumulative bonus to the objective.
- **Pk<3 trophy constraint** — certain trophées require that the sum of `(pieces − 1)` across all active sets is ≤ 2. This allows 1×3-piece set (Pk=2) or 2×2-piece sets (Pk=2), but not 2×3-piece sets (Pk=4).
- **Minimum stat constraints** — hard floors per stat, or across a sum of stats.
- **Stat caps** — upper bounds on individual stats (e.g. PA ≤ 5).
- **Locked items** — force specific items to always be equipped.
- **Excluded items** — blacklist items that should never appear.

By default, item effects are evaluated at their **maximum** roll. Use `--avg` to use the average of min/max instead.

### Equipment slots

| Slot | Count | Types |
|---|---|---|
| Weapon | 1 | Arc, Baguette, Bâton, Dague, Épée, Marteau, Pelle, Hache, Outil, Pioche, Faux |
| Amulette | 1 | Amulette |
| Anneau | 2 | Anneau |
| Ceinture | 1 | Ceinture |
| Bottes | 1 | Bottes |
| Chapeau | 1 | Chapeau |
| Cape | 1 | Cape |
| Bouclier | 1 | Bouclier |
| Animal | 1 | Familier, Montilier, Dragodinde, Muldo, Volkorne |
| Dofus/Trophée | 6 | Dofus, Trophée |

---

## Installation

```bash
pip install requests pulp flask playwright
playwright install chromium
```

---

## Web interface (recommended)

Launch the local web app:

```bash
python app.py
```

Then open **http://localhost:5000** in your browser.

The interface lets you:

- **Configure** all optimizer parameters (stat weights, minimums, caps, locked/excluded items, stat sum constraints) directly in the browser
- **Fetch** the latest item data from the API with one click
- **Optimize** for any level and see the results with set bonus highlighting
- **Open directly in DofusDB** — one click opens a Chromium browser on dofusdb.fr with the optimized gear pre-loaded in the stuff creator (requires Playwright)
- **Export via console snippet** — copy a JS snippet to paste in the browser DevTools console as a fallback

### Set bonus highlighting

Items from the same set share a colour in the results table. Active set bonuses are shown below the results.

---

## CLI usage

### Step 1 — Fetch item data

Downloads all items from the API and saves them to `items.json`. Only needed once (or to refresh data).

```bash
python optimizer.py fetch
```

Options:

```
--output PATH        Output file path (default: items.json)
--min-level N        Minimum item level to fetch (default: 0)
--max-level N        Maximum item level to fetch (default: 200)
```

### Step 2 — Optimize

```bash
python optimizer.py optimize --level 200
```

Options:

```
--level N            Max item level to consider (required)
--config PATH        Config file path (default: config.json)
--data PATH          Items data file (default: items.json)
--weights Stat:N     Override or add a stat weight from the command line
--min Stat:N         Override or add a minimum stat requirement
--avg                Use average of min/max rolls instead of max
```

### Examples

```bash
# Optimize for level 160, using config.json weights
python optimizer.py optimize --level 160

# Require at least 3000 Vitalité
python optimizer.py optimize --level 200 --min "Vitalité:3000"

# Override the weight of Force on the fly
python optimizer.py optimize --level 200 --weights "Force:5"

# Use average rolls instead of max
python optimizer.py optimize --level 200 --avg
```

---

## Configuration — `config.json`

```json
{
  "stat_weights": { ... },
  "min_stats":    { ... },
  "stat_caps":    { ... },
  "min_stat_sums": [ ... ],
  "excluded_items": [ ... ],
  "locked_items":   [ ... ]
}
```

### `stat_weights`

How much **1 point** of each stat is worth to the optimizer. The optimizer maximizes `Σ stat_value × weight` across all equipped items and active set bonuses.

Stat names must exactly match the French names used by the API (e.g. `"PA"`, `"PM"`, `"Portée"`, `"Terre (%)"`, `"Retrait PA"`). These are visible in `items.json` and in the web interface.

```json
"stat_weights": {
  "PA":       100,
  "PM":       90,
  "Portée":   51,
  "Vitalité": 0.2,
  "Force":    1
}
```

To build around a specific element, zero out the others:

```json
"Terre (%)": 6,  "Terre": 5,  "Force": 1,
"Feu (%)":   0,  "Feu":   0,
"Eau (%)":   0,  "Eau":   0,
"Air (%)":   0,  "Air":   0
```

### `min_stats`

Hard lower bounds per stat. The optimizer will reject any build that doesn't meet all of them.

```json
"min_stats": {
  "PA": 5,
  "PM": 2,
  "Portée": 6
}
```

### `stat_caps`

Upper bounds per stat — points beyond this value are not counted in the objective.

```json
"stat_caps": {
  "PA": 5,
  "PM": 3,
  "Portée": 6
}
```

### `min_stat_sums`

Minimum constraints on the **sum** of multiple stats. Each entry is an object with a `stats` list and a `min` value.

```json
"min_stat_sums": [
  { "stats": ["Chance", "Puissance"], "min": 1075 },
  { "stats": ["Force", "Puissance"],  "min": 800  }
]
```

### `excluded_items`

Items that will never be equipped, by exact name.

```json
"excluded_items": ["Annobusé de Maître Jarbo", "Surpuissant Chacha de Combat"]
```

### `locked_items`

Items that are **always** equipped, regardless of their stat contribution. Useful to fix one or more slots to a specific item while letting the optimizer fill the rest.

```json
"locked_items": ["Gelano", "Dofus Pourpre"]
```

---

## Files

| File | Description |
|---|---|
| `optimizer.py` | Data fetching and MIP optimization engine |
| `app.py` | Flask web interface |
| `templates/index.html` | Web UI (single-page app) |
| `config.json` | Optimizer configuration (weights, constraints, locks) |
| `items.json` | Cached item data from the API (generated by `fetch`) |
