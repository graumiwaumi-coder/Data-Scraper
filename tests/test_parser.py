import os
import unittest

from futbin_scraper.scraper import (
    detect_last_page,
    extract_player_links,
    page_url,
)

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "sample_players_page.html")


class ExtractPlayerLinksTest(unittest.TestCase):
    def setUp(self):
        with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
            self.html = f.read()

    def test_extracts_unique_players_in_order(self):
        links = extract_player_links(self.html, "https://www.futbin.com/26/players", page_num=1)
        ids = [link.player_id for link in links]
        self.assertEqual(ids, ["25561", "25562", "25563"])

    def test_builds_absolute_urls(self):
        links = extract_player_links(self.html, "https://www.futbin.com/26/players", page_num=1)
        self.assertEqual(
            links[0].url, "https://www.futbin.com/26/player/25561/rodrigo-hernandez-cascante"
        )

    def test_dedupes_repeated_thumbnail_and_name_links(self):
        links = extract_player_links(self.html, "https://www.futbin.com/26/players", page_num=1)
        self.assertEqual(len(links), 3)

    def test_prefers_non_empty_name_text(self):
        links = extract_player_links(self.html, "https://www.futbin.com/26/players", page_num=1)
        by_id = {link.player_id: link for link in links}
        self.assertEqual(by_id["25561"].name, "Rodrigo Hernández Cascante")

    def test_ignores_unrelated_nav_links(self):
        links = extract_player_links(self.html, "https://www.futbin.com/26/players", page_num=1)
        urls = {link.url for link in links}
        self.assertNotIn("https://www.futbin.com/27/squad-builder", urls)

    def test_source_page_recorded(self):
        links = extract_player_links(self.html, "https://www.futbin.com/26/players", page_num=7)
        self.assertTrue(all(link.source_page == 7 for link in links))


class DetectLastPageTest(unittest.TestCase):
    def test_finds_max_page_number(self):
        with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
            html = f.read()
        self.assertEqual(detect_last_page(html), 952)

    def test_returns_none_when_no_pagination(self):
        self.assertIsNone(detect_last_page("<html><body>no pages here</body></html>"))


class PageUrlTest(unittest.TestCase):
    def test_page_one_has_no_query_param(self):
        self.assertEqual(page_url("https://www.futbin.com/26/players", 1), "https://www.futbin.com/26/players")

    def test_other_pages_add_query_param(self):
        self.assertEqual(
            page_url("https://www.futbin.com/26/players", 2),
            "https://www.futbin.com/26/players?page=2",
        )

    def test_appends_with_ampersand_if_query_already_present(self):
        self.assertEqual(
            page_url("https://www.futbin.com/26/players?version=gold", 3),
            "https://www.futbin.com/26/players?version=gold&page=3",
        )


if __name__ == "__main__":
    unittest.main()
