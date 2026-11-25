#!/usr/bin/env python3
"""
Entity-linker error study (Phase 3). Because query generation itself chooses which product(s)
or category a query is about, the correct entity set is known by construction -- this script
regenerates queries the same way load/locustfile.py and load/open_loop_gen.py do, and checks
EntityLinker.resolve() against that known-correct answer, over every (template, reference-form)
combination, not just a handful of examples.

    python3 analysis/entity_linker_study.py --products 200 --seed 20260906
"""
import argparse, json, os, random, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
from queries import PRODUCT_TEMPLATES, COMPARE_TEMPLATES, CATEGORY_TEMPLATES, EntityLinker  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--products", type=int, default=200)
    ap.add_argument("--seed", type=int, default=20260906)
    ap.add_argument("--names", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "load", "product_names.json"))
    a = ap.parse_args()
    names = json.load(open(a.names))
    ids = [int(i) for i in list(names)[:a.products]]
    cats = sorted({v["category"] for v in names.values()})
    linker = EntityLinker([(i, names[str(i)]["name"]) for i in ids], cats)
    rng = random.Random(a.seed)

    def ref_forms(pid):
        return [f"product {pid}", f"item #{pid}", names[str(pid)]["name"]]

    rows = []
    for pid in ids:
        for form in ref_forms(pid):
            for tmpl in PRODUCT_TEMPLATES:
                q = tmpl.format(ref=form)
                got = linker.resolve(q)
                rows.append({"kind": "product", "template": tmpl, "ref_form": form, "expected": [f"product:{pid}"], "got": got,
                             "correct": got == [f"product:{pid}"]})
    for cat in cats:
        for tmpl in CATEGORY_TEMPLATES:
            q = tmpl.format(cat=cat)
            got = linker.resolve(q)
            rows.append({"kind": "category", "template": tmpl, "ref_form": cat, "expected": [f"category:{cat}"], "got": got,
                         "correct": got == [f"category:{cat}"]})
    for _ in range(200):
        a1, a2 = rng.sample(ids, 2)
        form1, form2 = rng.choice(ref_forms(a1)), rng.choice(ref_forms(a2))
        for tmpl in COMPARE_TEMPLATES:
            q = tmpl.format(ref1=form1, ref2=form2)
            got = linker.resolve(q)
            expected = sorted([f"product:{a1}", f"product:{a2}"])
            rows.append({"kind": "compare", "template": tmpl, "ref_form": f"{form1} / {form2}", "expected": expected, "got": got,
                         "correct": got == expected})

    # Stress case: the clean templates above use exact reference forms by construction, which is
    # why they show 0% error -- that is a property of the generator, not evidence the linker is
    # infallible. Two harder, realistic probes:
    stress = {"name_collisions": 0, "partial_name_failures": 0, "partial_name_total": 0}
    all_names = [names[str(i)]["name"] for i in ids]
    for i, n1 in enumerate(all_names):
        for n2 in all_names[i + 1:]:
            if n1.lower() in n2.lower() or n2.lower() in n1.lower():
                stress["name_collisions"] += 1
    for pid in ids[:50]:
        full = names[str(pid)]["name"]
        partial = " ".join(full.split()[1:])  # drop the brand/first word -- a realistic partial reference
        if not partial:
            continue
        stress["partial_name_total"] += 1
        got = linker.resolve(f"how much does the {partial} cost")
        if got != [f"product:{pid}"]:
            stress["partial_name_failures"] += 1
    print("stress probes (not part of the clean-template error rate above):")
    print(json.dumps(stress, indent=1))

    by_kind = {}
    for row in rows:
        k = row["kind"]
        by_kind.setdefault(k, {"n": 0, "correct": 0})
        by_kind[k]["n"] += 1
        by_kind[k]["correct"] += int(row["correct"])
    print(json.dumps({k: {"n": v["n"], "correct": v["correct"], "error_rate": round(1 - v["correct"] / v["n"], 4)}
                      for k, v in by_kind.items()}, indent=1))
    errors = [r for r in rows if not r["correct"]]
    print(f"\n{len(errors)} / {len(rows)} total mismatches")
    for e in errors[:20]:
        print(" ", e["kind"], repr(e["ref_form"]), "expected", e["expected"], "got", e["got"], "tmpl:", e["template"][:60])


if __name__ == "__main__":
    main()
