import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

import app


class PriceMatchTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        self.client = app.app.test_client()

    def login_session(self, user_id=1, csrf_token="csrf-test"):
        with self.client.session_transaction() as session:
            session["user_id"] = user_id
            session["csrf_token"] = csrf_token

    def test_private_pages_and_api_require_login(self):
        self.assertEqual(self.client.get("/automatico").status_code, 302)
        self.assertEqual(self.client.get("/manuale").status_code, 302)
        self.assertEqual(self.client.post("/api/search", json={"query": "MO9833"}).status_code, 401)

    def test_search_requires_csrf(self):
        self.login_session()
        response = self.client.post("/api/search", json={"query": "MO9833"})
        self.assertEqual(response.status_code, 403)

    def test_search_is_submitted_to_bounded_executor(self):
        self.login_session()
        with patch.object(app.search_executor, "submit") as submit:
            response = self.client.post(
                "/api/search",
                json={"query": "MO9833"},
                headers={"X-CSRF-Token": "csrf-test"},
            )
        self.assertEqual(response.status_code, 202)
        self.assertIn("task_id", response.get_json())
        submit.assert_called_once()

    def test_manual_search_rejects_private_address(self):
        self.login_session()
        response = self.client.post(
            "/api/manuale",
            json={"code": "MO9833", "sites": [{"name": "Locale", "url": "http://127.0.0.1:5000"}]},
            headers={"X-CSRF-Token": "csrf-test"},
        )
        self.assertEqual(response.status_code, 400)

    def test_price_parser_prefers_labeled_price(self):
        self.assertEqual(app.parse_price("Quantita 100 Prezzo 2,50"), 2.5)
        self.assertEqual(app.parse_price("100 pz EUR 2,50"), 2.5)
        self.assertIsNone(app.parse_price("SKU MO9833"))

    def test_product_verification_rejects_unrelated_body_mention(self):
        unrelated = BeautifulSoup(
            "<h1>Borraccia</h1><aside>Prodotti correlati: MO9833</aside>",
            "html.parser",
        )
        labeled = BeautifulSoup(
            "<h1>Borraccia</h1><p>SKU: MO-9833</p>",
            "html.parser",
        )
        self.assertFalse(app.exact_code_present(unrelated, "https://example.com/prodotto", "MO9833"))
        self.assertTrue(app.exact_code_present(labeled, "https://example.com/prodotto", "MO9833"))

    def test_scraper_summary_uses_real_pipeline(self):
        result = {
            "found": True,
            "verified_prices": True,
            "site": "Demo",
            "neutral": {"10": 2.5},
            "printed": {},
            "vat": "IVA inclusa",
        }
        with patch.object(app, "load_sites", return_value=[{"name": "Demo", "url": "https://example.com"}]), patch.object(
            app, "run_sites", return_value=[result]
        ) as run_sites:
            summary = app.avvia_scraping_siti("MO9833")
        run_sites.assert_called_once()
        self.assertEqual(summary["total_sites"], 1)
        self.assertEqual(summary["found_sites"], 1)
        self.assertEqual(summary["price_sites"], 1)

    def test_routes_are_unique(self):
        rules = [(rule.rule, tuple(sorted(rule.methods - {"HEAD", "OPTIONS"}))) for rule in app.app.url_map.iter_rules()]
        self.assertEqual(len(rules), len(set(rules)))


if __name__ == "__main__":
    unittest.main()
