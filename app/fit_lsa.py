#!/usr/bin/env python3
"""
Fit the lexical embedding family (TF-IDF -> truncated SVD, 256 dimensions) used by EMBEDDING=lsa256.

The model is fitted on a sample drawn from the workload's own query generator, with a fixed seed, and
saved next to the gateway. Fitting is offline and is not part of any measured run; the gateway only
loads the fitted pipeline.
"""
import os, random, sys, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "load"))

import joblib
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.pipeline import make_pipeline

N = int(os.environ.get("LSA_FIT_N", "40000"))
SEED = int(os.environ.get("LSA_SEED", "20260906"))


def main():
    random.seed(SEED)
    import locustfile as L                      # the generator the benchmark itself uses
    corpus = [L.make_query()[1] for _ in range(N)]
    pipe = make_pipeline(
        TfidfVectorizer(ngram_range=(1, 2), min_df=2, sublinear_tf=True, lowercase=True),
        TruncatedSVD(n_components=256, random_state=SEED),
    )
    pipe.fit(corpus)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lsa_model.joblib")
    joblib.dump(pipe, out)
    v = pipe.transform(corpus[:2])
    print(json.dumps({"corpus": len(corpus), "unique": len(set(corpus)), "dim": int(v.shape[1]),
                      "vocab": len(pipe.steps[0][1].vocabulary_), "out": out,
                      "explained_variance": round(float(pipe.steps[1][1].explained_variance_ratio_.sum()), 4)}))


if __name__ == "__main__":
    main()
