# diagnose.py — place in the same folder as db.py, run with the same python/venv as the app
from db import get_cached_cdragon

data = get_cached_cdragon()
if not data:
    print("get_cached_cdragon() returned None (cache empty/stale) — the app would fetch fresh JSON live on next load.")
else:
    print("=== setData summary ===")
    numbers = []
    for s in data.get("setData", []):
        n = s.get("number")
        champ_count = len(s.get("champions", []))
        print(f"number={n}  champions={champ_count}  mutator={s.get('mutator')}")
        if isinstance(n, int):
            numbers.append(n)

    target_set = max(numbers) if numbers else None
    print(f"\ncurrent_set_number() would pick: {target_set}")

    # Simulate what load_maps() actually builds for units
    unit_map = {}
    for s in data.get("setData", []):
        if target_set is not None and s.get("number") != target_set:
            continue
        for champ in s.get("champions", []):
            api_name = champ.get("apiName", "")
            if api_name:
                unit_map[api_name.lower()] = True

    print(f"\nunit_map would contain {len(unit_map)} entries.")
    print("Sample keys:", list(unit_map.keys())[:10])