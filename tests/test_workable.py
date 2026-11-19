"""Tests of boards/workable.py's own helpers (how it's used in a run: WorkableTests in test_job_monitor.py)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from boards import workable  # noqa: E402


class NameTests(unittest.TestCase):
    def test_names_are_compared_without_case_punctuation_accents_or_legal_words(self):
        cases = {"Bask Health, Inc.": "bask health", "Cadillac Formula 1® Team": "cadillac formula 1 team",
                 "Société Générale": "societe generale", "Tecsys Inc.": "tecsys", "RAVE Aerospace LLC": "rave aerospace",
                 "Scientific Systems Company": "scientific systems", "Co": "", "": "", None: ""}
        for name, key in cases.items():
            self.assertEqual(workable.name_key(name), key, name)
        # Words that merely contain a legal word stay.
        self.assertEqual(workable.name_key("Incode Collective"), "incode collective")

    def test_only_a_name_one_company_has_matches(self):
        one, other = ("Bask Health", "https://bask.example"), ("Bask Health LLC", "https://bask.example.org")
        by_name = {"bask health": [one], "acme": [one, other]}
        self.assertEqual(workable._only_company_named(by_name, ("Bask Health",)), one)
        self.assertIsNone(workable._only_company_named(by_name, ("Acme", "Bask Health")))  # several: no fallback to the next name
        self.assertEqual(workable._only_company_named(by_name, ("Bask Telehealth", "Bask Health")), one)  # not in the file: next
        self.assertIsNone(workable._only_company_named(by_name, (None, "", "Nobody")))


class CodeTests(unittest.TestCase):
    def test_the_account_of_a_board(self):
        self.assertEqual(workable.account_of("https://apply.workable.com/bask-health-1/"), "bask-health-1")
        self.assertEqual(workable.account_of("https://apply.workable.com/bask-health-1"), "bask-health-1")

    def test_codes_are_kept_until_an_account_is_matched_by_one(self):
        key = "https://apply.workable.com/bask"
        known = {key: {"url": key + "/", "codes": ["AAAAAAAAAA"]}}
        workable.remember_codes(known, {key: ["BBBBBBBBBB"]})
        self.assertEqual(known[key]["codes"], ["BBBBBBBBBB", "AAAAAAAAAA"])  # newest first
        known[key].update(tenant=["Bask", "https://bask.example"], matched_by="name")
        workable.remember_codes(known, {key: ["CCCCCCCCCC"]})  # matched by name: a code may still correct it
        self.assertEqual(known[key]["codes"][0], "CCCCCCCCCC")
        known[key].pop("matched_by")
        workable.remember_codes(known, {key: ["DDDDDDDDDD"]})  # matched by a code: matched again by its own jobs
        self.assertNotIn("DDDDDDDDDD", known[key]["codes"])
        workable.remember_codes(known, {"https://apply.workable.com/unknown": ["EEEEEEEEEE"]})  # not registered: nothing
        self.assertNotIn("https://apply.workable.com/unknown", known)

    def test_at_most_codes_kept(self):
        key = "https://apply.workable.com/bask"
        known = {key: {"url": key + "/"}}
        workable.remember_codes(known, {key: [f"{i:010X}" for i in range(workable.CODES_KEPT + 5)]})
        self.assertEqual(len(known[key]["codes"]), workable.CODES_KEPT)


if __name__ == "__main__":
    unittest.main()
