"""Ranking léxico y vectorial reproducible; sin red, dependencias ni pesos entrenados."""
import math
import re
import unicodedata
from collections import Counter
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


STOP = set("a al con de del el en es la las lo los para por que un una y the and for of on to with".split())
HYBRID_METHOD = "bm25_tfidf_cosine_v1"


def tfidf_vectors(query, documents):
    """TF sublineal, IDF suavizado y norma L2; vocabulario ajustado solo al corpus."""
    counts = [Counter(terms(text)) for text in documents]
    frequency = Counter(term for document in counts for term in document)
    idf = {term: 1 + math.log((1 + len(counts)) / (1 + count)) for term, count in frequency.items()}

    def vector(counts):
        weighted = {term: (1 + math.log(count)) * idf[term] for term, count in counts.items() if term in idf}
        norm = math.sqrt(math.fsum(value * value for value in weighted.values())) or 1
        return {term: value / norm for term, value in weighted.items()}

    return vector(Counter(terms(query))), [vector(document) for document in counts]


def cosine(left, right):
    """Producto escalar de vectores TF-IDF ya normalizados; acotado por redondeo."""
    return min(1.0, max(0.0, math.fsum(value * right.get(term, 0) for term, value in left.items())))


def terms(text):
    normalized = unicodedata.normalize("NFKD", text.casefold())
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    return [t for t in re.findall(r"[a-z0-9_]{2,}", normalized) if t not in STOP]


def canonical_url(url):
    parsed = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if not k.casefold().startswith("utm_") and k.casefold() not in {"fbclid", "gclid"}]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", urlencode(sorted(query)), ""))


def rank_sources(query, sources, *, limit=3, k1=1.2, b=.75, method="hybrid"):
    """Fusión BM25/TF-IDF/título, con BM25 anterior disponible como comparación."""
    if method not in {"hybrid", "bm25"}:
        raise ValueError("Método de ranking desconocido")
    candidates, seen = [], set()
    for source in sources[:20]:
        identity = canonical_url(source["url"])
        if identity not in seen:
            seen.add(identity)
            candidates.append(dict(source))
    if not candidates:
        return []
    # site: es un filtro del buscador y no un término de contenido.
    content_query = re.sub(r"\bsite:\S+", "", query, flags=re.I)
    query_terms = set(terms(content_query))
    texts = [s["title"] + " " + s.get("excerpt", "") for s in candidates]
    documents = [Counter(terms(text)) for text in texts]
    query_vector, vectors = tfidf_vectors(content_query, texts)
    average = sum(sum(d.values()) for d in documents) / len(documents) or 1
    frequency = Counter(t for d in documents for t in d)
    ranked = []
    for index, (source, document) in enumerate(zip(candidates, documents)):
        size, score = sum(document.values()), 0.0
        for term in query_terms:
            tf = document[term]
            idf = math.log(1 + (len(documents) - frequency[term] + .5) / (frequency[term] + .5))
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * size / average))
        coverage = len(query_terms & set(terms(source["title"]))) / len(query_terms) if query_terms else 0
        similarity = cosine(query_vector, vectors[index])
        ranked.append((score, coverage, similarity, index, source))
    maximum = max((row[0] for row in ranked), default=0) or 1
    scored = []
    for bm25, coverage, similarity, index, source in ranked:
        score = (.5 * bm25 / maximum + .35 * similarity + .15 * coverage
                 if method == "hybrid" else bm25 + coverage)
        source["ranking"] = {"method": HYBRID_METHOD if method == "hybrid" else "bm25_title_v1",
                             "score": round(score, 6), "original_rank": index + 1,
                             "components": {"bm25": round(bm25, 6), "bm25_normalized": round(bm25 / maximum, 6),
                                            "tfidf_cosine": round(similarity, 6), "title_coverage": round(coverage, 6)},
                             "scope": "Pertinencia léxica y vectorial local; no verifica hechos ni interpreta sinónimos."}
        scored.append((score, index, source))
    ranked = scored
    ranked.sort(key=lambda item: (-item[0], item[1]))
    selected, deferred, domains = [], [], Counter()
    for _, _, source in ranked:
        domain = urlsplit(source["url"]).hostname
        if domains[domain] >= 2:
            deferred.append(source)
        else:
            selected.append(source)
            domains[domain] += 1
    return (selected + deferred)[:limit]


def ranking_metrics(ranked, judgments, k=3):
    """Precision@k, Recall@k, MRR@k y nDCG@k por URL canónica."""
    labels = {canonical_url(url): grade for url, grade in judgments.items()}
    grades = [labels.get(canonical_url(s["url"]), 0) for s in ranked[:k]]
    positive = sum(value > 0 for value in labels.values())
    hits = sum(value > 0 for value in grades)
    dcg = lambda values: sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(values))
    ideal = dcg(sorted(labels.values(), reverse=True)[:k])
    return {"precision_at_k": hits / k, "recall_at_k": hits / positive if positive else 0,
            "mrr_at_k": next((1 / (i + 1) for i, grade in enumerate(grades) if grade > 0), 0),
            "ndcg_at_k": dcg(grades) / ideal if ideal else 0}
