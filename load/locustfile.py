"""
Locust workload for the semantic-cache study: 500 concurrent users, mixed semantic reads and OLTP writes.

  reads  (READ_SHARE, default 60 %): 70 % single-product questions, 15 % two-product comparisons, 15 % category
         questions; each drawn from the paraphrase templates in app/queries.py with a uniformly random template and a
         uniformly random reference form (id, "#id", full name). Products follow a Zipf-like popularity (alpha 1.07)
         so that a small set of hot products receives most reads.
  writes (1 - READ_SHARE): price change (+-15 %) or stock change on a uniformly drawn product (catalogue maintenance
         touches every product, whereas reads concentrate on popular ones).

Every response is logged to a per-request CSV with the gateway's own correctness verdict (hit / stale / wrong_entity),
similarity, token counts and latency, so staleness is judged against the committed database state at response time,
not against client-side bookkeeping.
"""
import csv, json, os, random, sys, time
from locust import FastHttpUser, task, events, LoadTestShape

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
from queries import PRODUCT_TEMPLATES, COMPARE_TEMPLATES, CATEGORY_TEMPLATES

N_PRODUCTS = int(os.environ.get("SC_PRODUCTS", "5000"))
READ_SHARE = float(os.environ.get("READ_SHARE", "0.60"))
DURATION = int(os.environ.get("SC_DURATION", "120"))
USERS = int(os.environ.get("SC_USERS", "500"))
OUT = os.environ.get("SC_REQ_LOG", "requests.csv")
NAMES = json.load(open(os.environ.get("SC_NAMES", os.path.join(os.path.dirname(os.path.abspath(__file__)), "product_names.json"))))
CATS = sorted({v["category"] for v in NAMES.values()})

_f = open(OUT, "w", buffering=1 << 16, newline="")
_w = csv.writer(_f)
_w.writerow(["ts_ms", "kind", "qclass", "status", "latency_ms", "hit", "stale", "wrong_entity", "sim", "prompt_tokens", "completion_tokens", "n_entities", "invalidated", "lookup_ms",
             "entities", "answer_versions", "truth_versions_selfreported", "pid", "cache_entities"])


def zipf_id(n, alpha=1.07):
    return max(1, min(n, int(n ** (random.random() ** alpha))))


def ref(pid):
    r = random.random()
    if r < 0.4:
        return f"product {pid}"
    if r < 0.6:
        return f"item #{pid}"
    return NAMES[str(pid)]["name"]


def make_query():
    r = random.random()
    if r < 0.70:
        return "product", random.choice(PRODUCT_TEMPLATES).format(ref=ref(zipf_id(N_PRODUCTS)))
    if r < 0.85:
        a = zipf_id(N_PRODUCTS); b = zipf_id(N_PRODUCTS)
        while b == a:
            b = zipf_id(N_PRODUCTS)
        return "compare", random.choice(COMPARE_TEMPLATES).format(ref1=ref(a), ref2=ref(b))
    return "category", random.choice(CATEGORY_TEMPLATES).format(cat=random.choice(CATS))


@events.quitting.add_listener
def on_quit(**kw):
    _f.flush(); _f.close()


class ApiUser(FastHttpUser):
    connection_timeout = 30.0
    network_timeout = 30.0

    def wait_time(self):
        return random.uniform(1.0, 3.0)

    @task
    def step(self):
        t0 = time.time()
        if random.random() < READ_SHARE:
            qclass, q = make_query()
            with self.client.post("/api/query", json={"q": q}, name="POST /api/query", catch_response=True) as resp:
                lat = (time.time() - t0) * 1000
                if resp.status_code == 200:
                    try:
                        d = resp.json()
                    except Exception:
                        d = {}
                    # cache_entities comes straight from the DB's canonical() format, which joins
                    # with "|" (app/main.py) -- re-split and re-join with ";" so this column uses
                    # the same delimiter as `entities` above; logging it verbatim caused every hit
                    # to look like a wrong-entity mismatch when independent_checker.py split on ";"
                    # (confirmed during the Phase 4 smoke test: 5/626 hits falsely disagreed before
                    # this fix, all differing only by delimiter, not by actual entity content)
                    cache_ents_raw = d.get("cache_entities", "")
                    cache_ents = ";".join(cache_ents_raw.split("|")) if cache_ents_raw and cache_ents_raw != "-" else ""
                    _w.writerow([int(t0 * 1000), "read", qclass, resp.status_code, f"{lat:.2f}", int(bool(d.get("hit"))), int(bool(d.get("stale"))),
                                 int(bool(d.get("wrong_entity"))), d.get("sim", ""), d.get("prompt_tokens", 0), d.get("completion_tokens", 0),
                                 len(d.get("entities", [])), "", d.get("lookup_ms", ""),
                                 ";".join(d.get("entities", [])), json.dumps(d.get("answer_versions", {})),
                                 json.dumps(d.get("truth_versions", {})), "", cache_ents])
                    if d.get("stale") or d.get("wrong_entity"):
                        resp.failure("STALE_READ" if d.get("stale") else "WRONG_ENTITY")
                    else:
                        resp.success()
                else:
                    _w.writerow([int(t0 * 1000), "read", qclass, resp.status_code, f"{lat:.2f}", 0, 0, 0, "", 0, 0, 0, "", "", "", "", "", "", ""])
                    resp.failure(str(resp.status_code))
        else:
            pid = random.randint(1, N_PRODUCTS)
            if random.random() < 0.7:
                base = NAMES[str(pid)]["price_cents"]
                body = {"price_cents": max(100, int(base * random.uniform(0.85, 1.15)))}
            else:
                body = {"stock": random.choice([0, 5, 20, 50, 100, 200])}
            with self.client.post(f"/api/product/{pid}", json=body, name="POST /api/product/{id}", catch_response=True) as resp:
                lat = (time.time() - t0) * 1000
                inv = ""
                if resp.status_code == 200:
                    try:
                        inv = resp.json().get("invalidated", "")
                    except Exception:
                        pass
                    resp.success()
                else:
                    resp.failure(str(resp.status_code))
                _w.writerow([int(t0 * 1000), "write", "update", resp.status_code, f"{lat:.2f}", "", "", "", "", 0, 0, 1, inv, "",
                             f"product:{pid}", "", "", pid, ""])


class Shape(LoadTestShape):
    def tick(self):
        t = self.get_run_time()
        if t > DURATION:
            return None
        return (USERS, 500)
