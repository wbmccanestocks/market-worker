import json
import unittest
from pathlib import Path
from unittest.mock import patch

import market_rss_listener as rss

RSS = """<rss><channel><item><guid>one</guid><title>Acme <b>raises</b> guidance</title><link>https://example.test/acme</link><description><![CDATA[<p>Revenue rose 20%.</p><script>bad()</script>]]></description></item></channel></rss>"""
ATOM = """<feed xmlns='http://www.w3.org/2005/Atom'><entry><id>two</id><title>Beta wins contract</title><link href='https://example.test/beta'/><summary><![CDATA[<div>New energy supply agreement.</div>]]></summary></entry></feed>"""

class MarketRssListenerTest(unittest.TestCase):
    def test_parse_rss_and_atom_strip_markup(self):
        rss_entry = rss.parse_feed(RSS, "https://feed.test/rss")[0]
        atom_entry = rss.parse_feed(ATOM, "https://feed.test/atom")[0]
        self.assertEqual(rss_entry["clean_text"], "Acme raises guidance Revenue rose 20%.")
        self.assertNotIn("bad", rss_entry["clean_text"])
        self.assertEqual(atom_entry["url"], "https://example.test/beta")

    def test_hype_filter_is_conservative(self):
        self.assertIsNotNone(rss.hype_reason("Sponsored content: act now and double your money"))
        self.assertIsNone(rss.hype_reason("Analyst says must-buy stock"))
        self.assertIsNone(rss.hype_reason("Company raises guidance and reports revenue growth"))

    def test_poll_deduplicates_and_baselines(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            base = Path(directory)
            config, state = base / "feeds.json", base / "state.json"
            config.write_text(json.dumps({"feeds": [{"label": "Test", "url": "https://feed.test/rss"}]}))
            fetcher = lambda _url: (200, "https://feed.test/rss", RSS)
            self.assertEqual(rss.poll(config, state, fetcher=fetcher), ([], []))
            self.assertEqual(rss.poll(config, state, backfill=True, fetcher=fetcher), ([], []))

    def test_handoff_claim_retries_until_acknowledged(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            handoff = Path(directory) / "rss.jsonl"
            rss.append_handoff([{"id": "one", "title": "Acme"}], handoff)
            records, claim = rss.claim_handoff(handoff)
            self.assertEqual([record["id"] for record in records], ["one"])
            retried, retry_claim = rss.claim_handoff(handoff)
            self.assertEqual([record["id"] for record in retried], ["one"])
            self.assertEqual(claim, retry_claim)
            rss.acknowledge_handoff(claim)
            self.assertEqual(rss.claim_handoff(handoff), ([], None))

    def test_retry_after_state_skips_fetch(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            base = Path(directory)
            config, state = base / "feeds.json", base / "state.json"
            url = "https://feed.test/rss"
            config.write_text(json.dumps({"feeds": [{"label": "Test", "url": url}]}))
            state.write_text(json.dumps({"seen_ids": [], "initialized": True, "feeds": {url: {"retry_after": "2999-01-01T00:00:00+00:00"}}}))
            records, gaps = rss.poll(config, state, fetcher=lambda _: self.fail("backed-off feed fetched"))
            self.assertEqual(records, [])
            self.assertIn("skipped until Retry-After", gaps[0])

    def test_429_retry_after_is_persisted(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            base = Path(directory)
            config, state = base / "feeds.json", base / "state.json"
            url = "https://feed.test/rss"
            config.write_text(json.dumps({"feeds": [{"label": "Test", "url": url}]}))
            with patch.object(rss, "_fetch_conditional", return_value=(429, url, "", {"Retry-After": "120"})):
                records, gaps = rss.poll(config, state)
            saved = json.loads(state.read_text())
            self.assertEqual(records, [])
            self.assertIn("rate-limited; deferred", gaps[0])
            self.assertIn("retry_after", saved["feeds"][url])
