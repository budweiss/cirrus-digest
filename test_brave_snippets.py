"""S592: brave_search can hand back each result's text for contact_list.
Synthetic: no network, no key, no usage file (the counter is patched out)."""
import unittest
from unittest.mock import Mock, patch

import cirrus_daily as cd

RESULTS = {"web": {"results": [
    {"url": "https://www.yelp.com/search?a", "title": "Best Snow Removal - Yelp",
     "description": "Eastern Cape Landscaping, Taylor Lawn Care", "extra_snippets": ["More", 7]},
    {"url": "https://example.test/b", "title": "B", "description": "two"},
    {"url": "https://example.test/c", "title": "C", "description": "three"}]}}


class BraveSnippets(unittest.TestCase):
    def call(self, **kw):
        resp = Mock(status_code=200)
        resp.json.return_value = RESULTS
        with patch.object(cd, "_load_brave_key", return_value="test-key"), \
                patch.object(cd, "_count_search"), patch.object(cd, "_SPEND", None), \
                patch.object(cd, "log"), patch.object(cd.requests, "get", return_value=resp) as get:
            urls = cd.brave_search("snow removal Lewes", 2, "test", **kw)
        return urls, get.call_args.kwargs["params"]

    def test_snippets_filled_for_returned_urls_only(self):
        sn = {}
        urls, params = self.call(snippets=sn)
        self.assertEqual(urls, ["https://www.yelp.com/search?a", "https://example.test/b"])
        self.assertEqual(params.get("extra_snippets"), "true")
        self.assertEqual(sn["https://www.yelp.com/search?a"],
                         ["Best Snow Removal - Yelp", "Eastern Cape Landscaping, Taylor Lawn Care", "More"])
        self.assertEqual(set(sn), set(urls))

    def test_other_callers_unchanged(self):
        urls, params = self.call()
        self.assertEqual(len(urls), 2)
        self.assertNotIn("extra_snippets", params)

    def test_search_web_passes_the_store_to_brave(self):
        sn = {}
        with patch.object(cd, "brave_search", return_value=["u"]) as bs:
            self.assertEqual(cd.search_web("q", 3, "test", snippets=sn), ["u"])
        self.assertIs(bs.call_args.kwargs["snippets"], sn)


if __name__ == "__main__":
    unittest.main()
