import copy
import asyncio
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from agent_core.retrieval import HYBRID_METHOD, canonical_url, cosine, ranking_metrics, rank_sources, tfidf_vectors
from scripts.evaluate_agent import evaluate, load_cases
from agent_core.tools import Tools


class RetrievalTest(unittest.TestCase):
    def test_tfidf_vectors_have_unit_norm_and_known_cosines(self):
        query, vectors = tfidf_vectors('python python', ['python', 'cafe', ''])
        self.assertAlmostEqual(sum(value ** 2 for value in query.values()), 1)
        self.assertAlmostEqual(cosine(query, vectors[0]), 1)
        self.assertEqual(cosine(query, vectors[1]), 0)
        self.assertEqual(cosine(query, vectors[2]), 0)
        unknown, _ = tfidf_vectors('desconocido', ['python'])
        self.assertEqual(unknown, {})

    def test_hybrid_scores_expose_components_and_lexical_baseline(self):
        sources = [{'url': 'https://a.org/', 'title': 'Python', 'excerpt': 'unittest pruebas'},
                   {'url': 'https://b.org/', 'title': 'Café', 'excerpt': 'tostado'}]
        ranked = rank_sources('Python unittest', sources)
        for row in ranked:
            parts = row['ranking']['components']
            self.assertAlmostEqual(row['ranking']['score'], .5 * parts['bm25_normalized'] + .35 * parts['tfidf_cosine'] + .15 * parts['title_coverage'], places=5)
        self.assertEqual(ranked[1]['ranking']['score'], 0)
        self.assertEqual(rank_sources('Python', sources, method='bm25')[0]['ranking']['method'], 'bm25_title_v1')

    def test_ranks_relevant_sources_before_unrelated_ones_without_modifying_input(self):
        sources = [{"url": "https://a.org/a", "title": "Clima", "excerpt": "Pronóstico del tiempo"},
                   {"url": "https://b.org/b", "title": "Python unittest", "excerpt": "Ejecutar pruebas Python"}]
        original = copy.deepcopy(sources)
        ranked = rank_sources("Python unittest pruebas", sources)
        self.assertEqual(ranked[0]["url"], "https://b.org/b")
        self.assertGreater(ranked[0]["ranking"]["score"], ranked[1]["ranking"]["score"])
        self.assertEqual(sources, original)

    def test_deduplicates_tracking_urls_and_keeps_distinct_query_parameters(self):
        self.assertEqual(canonical_url("https://A.org/x?utm_source=chat&lang=es#heading"), "https://a.org/x?lang=es")
        sources = [{"url": "https://a.org/x?utm_source=1", "title": "Python", "excerpt": "Python"},
                   {"url": "https://a.org/x#p", "title": "Duplicada", "excerpt": "Python"},
                   {"url": "https://a.org/x?lang=es", "title": "Otra versión", "excerpt": "Python"}]
        self.assertEqual(len(rank_sources("Python", sources)), 2)

    def test_domain_diversity_and_ties_are_deterministic(self):
        sources = [{"url": "https://a.org/" + str(i), "title": "Python", "excerpt": "Python"} for i in range(3)]
        sources += [{"url": "https://b.org/", "title": "Python", "excerpt": "Python"}]
        self.assertEqual([s["url"] for s in rank_sources("Python", sources)], ["https://a.org/0", "https://a.org/1", "https://b.org/"])
        self.assertEqual(rank_sources("Python", []), [])
        self.assertTrue(all(math.isfinite(s["ranking"]["score"]) for s in rank_sources("de la y", sources)))

    def test_metrics_match_a_known_rank_order(self):
        labels = {"https://a.org/": 3, "https://b.org/": 1, "https://c.org/": 0}
        ranked = [{"url": url} for url in ("https://c.org/", "https://a.org/", "https://b.org/")]
        metrics = ranking_metrics(ranked, labels)
        self.assertAlmostEqual(metrics["precision_at_k"], 2/3)
        self.assertEqual(metrics["recall_at_k"], 1)
        self.assertEqual(metrics["mrr_at_k"], .5)
        expected = (7/math.log2(3) + 1/math.log2(4)) / (7 + 1/math.log2(3))
        self.assertAlmostEqual(metrics["ndcg_at_k"], expected)

    def test_evaluation_partitions_and_complete_judgments_are_validated(self):
        data = load_cases(Path(__file__).resolve().parent.parent / "evaluation/search_cases.json")
        report = evaluate(data)
        self.assertGreaterEqual(report["case_count"], 3)
        self.assertEqual(report["split"], "test")
        self.assertTrue(all(0 <= value <= 1 for method in report["metrics"].values() for value in method.values()))
        with self.assertRaises(ValueError):
            evaluate(data, k=0)
        invalid_cases = []
        duplicate = copy.deepcopy(data)
        duplicate["cases"][1]["query"] = duplicate["cases"][0]["query"]
        invalid_cases.append(duplicate)
        missing_label = copy.deepcopy(data)
        missing_label["cases"][0]["judgments"].pop(next(iter(missing_label["cases"][0]["judgments"])))
        invalid_cases.append(missing_label)
        bad_grade = copy.deepcopy(data)
        first_url = next(iter(bad_grade["cases"][0]["judgments"]))
        bad_grade["cases"][0]["judgments"][first_url] = True
        invalid_cases.append(bad_grade)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cases.json"
            for invalid in invalid_cases:
                path.write_text(json.dumps(invalid), "utf-8")
                with self.assertRaises(ValueError):
                    load_cases(path)

    def test_search_integration_filters_private_urls_ranks_and_records_evidence(self):
        def upstream(request):
            self.assertEqual(request.url.params["q"], "Python unittest")
            return httpx.Response(200, json={"results": [
                {"url": "http://127.0.0.1/secret", "title": "Python unittest", "content": "Privado"},
                {"url": "https://noise.org/", "title": "Clima", "content": "Pronóstico"},
                {"url": "https://docs.python.org/3/library/unittest.html", "title": "Python unittest", "content": "Python pruebas unittest"},
                {"url": "https://docs.python.org/3/library/unittest.html?utm_source=tracking", "title": "Duplicado", "content": "Python"}]})
        events = []
        tools = Tools(None, 1, 2, httpx.MockTransport(upstream), event=lambda *event: events.append(event))
        with patch.dict("os.environ", {"SEARXNG_URL": "http://127.0.0.1:8888"}):
            sources = asyncio.run(tools.search("Python unittest"))
        self.assertEqual(sources[0]["url"], "https://docs.python.org/3/library/unittest.html")
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0]["id"], 1)
        self.assertEqual(events[0][0], "retrieval")
        self.assertEqual(sources[0]["ranking"]["method"], HYBRID_METHOD)

