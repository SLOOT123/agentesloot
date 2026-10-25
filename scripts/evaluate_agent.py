"""Evaluación offline de ranking con etiquetas explícitas; no consulta red ni entrena."""
import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_core.retrieval import canonical_url, ranking_metrics, rank_sources


def load_cases(path):
    data = json.loads(path.read_text("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list) or not data["cases"]:
        raise ValueError("El conjunto necesita una lista cases no vacía.")
    identifiers, queries = set(), set()
    for case in data["cases"]:
        if not isinstance(case, dict) or not {"id", "split", "query", "candidates", "judgments"} <= case.keys():
            raise ValueError("Cada caso necesita id, split, query, candidates y judgments.")
        if (not isinstance(case["id"], str) or not case["id"] or case["id"] in identifiers
                or case["split"] not in {"train", "validation", "test"}
                or not isinstance(case["query"], str) or not case["query"].strip()):
            raise ValueError("Revisa identificadores únicos, split y consulta.")
        # Una misma consulta no puede aparecer en entrenamiento y evaluación.
        query = " ".join(case["query"].casefold().split())
        if query in queries:
            raise ValueError("Hay consultas duplicadas entre casos; evita contaminación entre particiones.")
        identifiers.add(case["id"]); queries.add(query)
        candidates, judgments = case["candidates"], case["judgments"]
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 20 or not isinstance(judgments, dict):
            raise ValueError("Usa uno a veinte candidatos y un diccionario de etiquetas por URL.")
        urls = set()
        for source in candidates:
            if (not isinstance(source, dict) or not all(isinstance(source.get(k), str) for k in ("url", "title", "excerpt"))
                    or not source["url"].startswith("https://")):
                raise ValueError("Cada candidato necesita url HTTPS, title y excerpt.")
            url = canonical_url(source["url"])
            if url in urls:
                raise ValueError("Los candidatos contienen URLs equivalentes duplicadas.")
            urls.add(url)
        label_urls = {canonical_url(url) for url in judgments}
        if label_urls != urls or len(label_urls) != len(judgments):
            raise ValueError("Etiqueta todos los candidatos una sola vez, incluidos los irrelevantes.")
        if any(type(g) is not int or not 0 <= g <= 3 for g in judgments.values()) or not any(judgments.values()):
            raise ValueError("Etiquetas: enteros 0..3; al menos un candidato relevante.")
    return data


def evaluate(data, split="test", k=3):
    if type(k) is not int or not 1 <= k <= 10:
        raise ValueError("k debe estar entre 1 y 10.")
    rows = []
    for case in data["cases"]:
        if case["split"] != split:
            continue
        baseline = ranking_metrics(case["candidates"], case["judgments"], k)
        lexical = rank_sources(case["query"], case["candidates"], limit=k, method="bm25")
        ranked = rank_sources(case["query"], case["candidates"], limit=k)
        rows.append({"id": case["id"], "baseline": baseline,
                     "bm25": ranking_metrics(lexical, case["judgments"], k),
                     "ranked": ranking_metrics(ranked, case["judgments"], k),
                     "selected_urls": [s["url"] for s in ranked]})
    if not rows:
        raise ValueError("No hay casos en la partición elegida.")
    averages = {method: {metric: round(math.fsum(row[method][metric] for row in rows) / len(rows), 6)
                        for metric in rows[0][method]} for method in ("baseline", "bm25", "ranked")}
    return {"dataset": data.get("name", "sin nombre"), "split": split, "k": k, "case_count": len(rows),
            "metrics": averages, "cases": rows,
            "scope": "Corpus ilustrativo etiquetado manualmente; no mide exactitud factual, red real ni generalización. No se entrenaron pesos."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path(__file__).resolve().parent.parent / "evaluation/search_cases.json")
    parser.add_argument("--split", choices=["train", "validation", "test"], default="test")
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = evaluate(load_cases(args.dataset), args.split, args.k)
        content = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(content + "\n", "utf-8")
        print(content)
    except (ValueError, OSError) as error:
        raise SystemExit("Evaluación no ejecutada: " + str(error)) from None
