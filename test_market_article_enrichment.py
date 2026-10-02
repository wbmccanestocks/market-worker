import datetime as dt
import tempfile
import unittest
from pathlib import Path

import market_worker_cron as worker


class MarketArticleEnrichmentTest(unittest.TestCase):
    def test_rss_records_keep_a_stable_handoff_id(self):
        sources = worker.rss_sources_from_records(
            [{"id": "rss-1", "category": "Technology", "label": "Test", "feed_url": "https://feed.example/rss", "url": "https://feed.example/article", "title": "NVDA raises guidance", "published": "", "clean_text": "NVDA raises guidance"}],
            {"NVDA"},
        )
        self.assertEqual(sources[0]["id"], "rss-1")

    def test_extracts_http_links_from_email_html(self):
        links = worker.article_links_from_html(
            '<a href="https://news.example/story">Story</a>'
            '<a href="https://news.example/unsubscribe">Unsubscribe</a>'
            '<a href="mailto:test@example.com">Mail</a>'
        )
        self.assertEqual(links, ["https://news.example/story"])

    def test_paywall_is_ignored_then_retried_after_a_week(self):
        now = dt.datetime(2026, 9, 15, tzinfo=dt.timezone.utc)
        source = {
            "source_type": "rss_atom", "label": "RSS: Test", "snippets": [],
            "tickers": [], "article_urls": ["https://paywall.example/story"],
        }
        calls = []

        def paywalled(url):
            calls.append(url)
            return 200, url, "<html><title>Subscribe to continue</title><p>Subscription required.</p></html>"

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "article-state.json"
            gaps = []
            worker.enrich_linked_articles([source], gaps, now, fetcher=paywalled, state_path=state)
            self.assertEqual(len(calls), 1)
            self.assertIn("ignored paywalled article", gaps[0])

            worker.enrich_linked_articles([source], [], now + dt.timedelta(days=1), fetcher=paywalled, state_path=state)
            self.assertEqual(len(calls), 1)

            worker.enrich_linked_articles([source], [], now + dt.timedelta(days=8), fetcher=paywalled, state_path=state)
            self.assertEqual(len(calls), 2)

    def test_readable_article_evidence_is_appended(self):
        source = {
            "source_type": "email_html", "label": "Email: Test", "snippets": [],
            "tickers": [], "article_urls": ["https://news.example/story"],
        }
        with tempfile.TemporaryDirectory() as directory:
            worker.enrich_linked_articles(
                [source], [], dt.datetime(2026, 9, 15, tzinfo=dt.timezone.utc),
                fetcher=lambda url: (200, url, "<html><title>NVDA raises guidance</title><article>NVDA reported strong earnings and raised guidance.</article></html>"),
                state_path=Path(directory) / "article-state.json",
            )
        self.assertTrue(any("Linked article" in snippet for snippet in source["snippets"]))
        self.assertIn("NVDA", source["tickers"])

