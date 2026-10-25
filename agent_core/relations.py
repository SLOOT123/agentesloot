"""Topología de un grafo de semejanza textual; no es inferencia causal ni homología persistente."""
import re
from fractions import Fraction
from urllib.parse import urlsplit


STOP = set("para como sobre entre desde hasta este esta estos estas una unos unas que con por del las los sus sin son ser the and for with from this that are was have".split())


def research_graph(sources, threshold=.15):
    exact_threshold = Fraction(str(threshold))
    nodes = [{"id": source["id"], "title": source["title"], "url": source.get("url", "")}
             for source in sources[:8]]
    tokens = [set(re.findall(r"\w{4,}", (s["title"] + " " + s.get("excerpt", "")).casefold())) - STOP
              for s in sources[:8]]
    edges, adjacency = [], {node["id"]: set() for node in nodes}
    for first in range(len(nodes)):
        for second in range(first + 1, len(nodes)):
            union = tokens[first] | tokens[second]
            intersection = len(tokens[first] & tokens[second])
            similarity = Fraction(intersection, len(union)) if union else Fraction(0)
            if similarity >= exact_threshold:
                a, b = nodes[first]["id"], nodes[second]["id"]
                edges.append({"source": a, "target": b, "similarity": round(float(similarity), 4),
                              "similarity_exact": {"numerator": similarity.numerator, "denominator": similarity.denominator},
                              "intersection_terms": intersection, "union_terms": len(union)})
                adjacency[a].add(b); adjacency[b].add(a)
    components, seen = [], set()
    for node in nodes:
        if node["id"] in seen:
            continue
        component, stack = [], [node["id"]]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current); component.append(current)
            stack.extend(adjacency[current] - seen)
        components.append(sorted(component))
    vertices, links, count = len(nodes), len(edges), len(components)
    domains = {urlsplit(node["url"]).hostname for node in nodes if node["url"]}
    return {"nodes": nodes, "edges": edges, "components": components,
            "statistics": {"vertices": vertices, "edges": links, "beta_0": count,
                           "beta_1": links - vertices + count,
                           "density": round(2 * links / (vertices * (vertices - 1)), 4) if vertices > 1 else 0,
                           "web_domains": len(domains)},
            "method": "Jaccard de términos, umbral " + str(threshold),
            "scope": "Betti del grafo como complejo 1D; conexiones por semejanza textual. No demuestra causalidad, exactitud ni significación estadística."}
