import os
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.pop("COMUNI_API_TOKEN", None)

import main
from local_geography import find_comune


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

    def test_comune_without_dealer_resolves_without_external_api(self):
        self.assertEqual(find_comune("Reggello")["provincia"], "FI")
        location = main.resolve_city("Reggello", self.db)
        self.assertEqual(location["scope"], "city")
        self.assertEqual(location["provincia"], "FI")
        self.assertTrue(main.find_dealers_near(self.db, location))

    def test_ambiguous_comune_requires_province(self):
        self.assertIsNone(find_comune("Castro"))
        self.assertEqual(find_comune("Castro (BG)")["provincia"], "BG")
        self.assertEqual(find_comune("Castro (LE)")["provincia"], "LE")

    def test_current_provinces_override_saved_city(self):
        locations = main.resolve_requested_locations(
            "Firenze o Arezzo", None, "Reggello", self.db
        )
        self.assertEqual([item["provincia"] for item in locations], ["FI", "AR"])
        self.assertTrue(all(main.find_dealers_near(self.db, item) for item in locations))

    def test_unrecognized_new_location_does_not_reuse_saved_city(self):
        locations = main.resolve_requested_locations(
            "Sedi vicino a Località Inesistente",
            "Località Inesistente",
            "Reggello",
            self.db,
        )
        self.assertEqual(locations, [])

    def test_reported_two_turn_conversation(self):
        memory_engine = create_engine("sqlite:///:memory:")
        main.Base.metadata.create_all(memory_engine)
        with Session(memory_engine) as db:
            db.add(main.Dealer(
                company_name="Sede Firenze", address_showroom="Firenze FI",
                city="Firenze", provincia="FI",
            ))
            db.add(main.Dealer(
                company_name="Sede Arezzo", address_showroom="Arezzo AR",
                city="Arezzo", provincia="AR",
            ))
            db.add(main.Lead(session_id="test-session", status="in_progress"))
            db.add(main.ConversationState(
                session_id="test-session", history_json="[]", extra_json="{}"
            ))
            db.commit()

            first_model = {
                "assistant_reply": "Ora ti indico le sedi.",
                "intent_type": "dealer_nearby",
                "lead_updates": {"z_name": "Monica", "z_city": "Reggello"},
                "new_status": "in_progress",
                "dealer_city_raw": "Reggello",
            }
            second_model = {
                "assistant_reply": "Ora ti indico Firenze e Arezzo.",
                "intent_type": "dealer_nearby",
                "lead_updates": {"z_city": "Reggello"},
                "new_status": "in_progress",
                "dealer_city_raw": None,
            }
            with patch.object(main, "search_kb", return_value=[]), patch.object(
                main, "call_beatrice", side_effect=[first_model, second_model]
            ):
                first = main.chat(main.ChatRequest(
                    session_id="test-session",
                    message="Monica potrei sapere dove si trova una concessionaria vicino a Reggello",
                ), db)
                second = main.chat(main.ChatRequest(
                    session_id="test-session", message="Firenze o Arezzo"
                ), db)

            self.assertIn("Sede Firenze", first.reply)
            self.assertIn("Sede Firenze", second.reply)
            self.assertIn("Sede Arezzo", second.reply)


if __name__ == "__main__":
    unittest.main()
