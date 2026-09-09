import os
import unittest

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.pop("COMUNI_API_TOKEN", None)

import main


class DealerSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = main.SessionLocal()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_reported_provinces_are_resolved_locally(self):
        expected = {"Brescia": "BS", "Bergamo": "BG", "Mantova": "MN"}
        for name, code in expected.items():
            with self.subTest(name=name):
                self.assertEqual(main.resolve_city(name, self.db)["provincia"], code)

    def test_natural_province_phrase_is_cleaned(self):
        result = main.resolve_city("Sedi in provincia di MANTOVA", self.db)
        self.assertEqual(result["city"], "Mantova")
        self.assertEqual(result["provincia"], "MN")
        self.assertEqual(result["scope"], "province")

    def test_every_covered_province_can_find_its_dealers_without_api(self):
        covered_codes = {
            code.strip().upper()
            for (code,) in self.db.query(main.Dealer.provincia).distinct().all()
        }
        province_rows = (
            self.db.query(main.ProvinceItalia)
            .filter(main.ProvinceItalia.sigla.in_(covered_codes))
            .all()
        )
        mapped_codes = {row.sigla.strip().upper() for row in province_rows}
        self.assertEqual(covered_codes, mapped_codes)

        for province in province_rows:
            with self.subTest(province=province.provincia):
                location = main.resolve_city(province.provincia, self.db)
                dealers = main.find_dealers_near(self.db, location, limit=3)
                self.assertTrue(dealers)
                self.assertTrue(all(d.provincia == province.sigla for d in dealers))

    def test_reported_results(self):
        expected_counts = {"Brescia": 1, "Bergamo": 2, "Mantova": 0}
        for name, count in expected_counts.items():
            with self.subTest(name=name):
                location = main.resolve_city(name, self.db)
                dealers = main.find_dealers_near(self.db, location, limit=10)
                self.assertEqual(len(dealers), count)

    def test_mismatched_address_is_not_shown_in_wrong_province(self):
        location = main.resolve_city("Bergamo", self.db)
        dealers = main.find_dealers_near(self.db, location, limit=10)
        self.assertNotIn("GI.CA.R. SRL", {dealer.company_name for dealer in dealers})


if __name__ == "__main__":
    unittest.main()
