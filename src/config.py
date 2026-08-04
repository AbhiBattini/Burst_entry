"""Load config.yaml + seed.json. Single source of truth for all params (see ../config.yaml)."""
import json
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    sp = ROOT / "seed.json"
    seed = json.loads(sp.read_text()) if sp.exists() else {"reach_p998": {}, "swspan_median": None}
    cfg["_root"] = ROOT
    return cfg, seed
