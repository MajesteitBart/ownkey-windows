import unittest

from text_cleanup import (
    apply_corrections,
    clean_transcript,
    filler_words,
    normalize_corrections,
    normalize_vocabulary,
    remove_fillers,
)


class FillerRemovalTests(unittest.TestCase):
    words = filler_words(["en", "nl"])

    def clean(self, text):
        return remove_fillers(text, self.words)

    def test_mid_sentence_fillers_and_pause_commas_disappear(self):
        self.assertEqual(self.clean("I think, uh, we should ship it."), "I think, we should ship it.")
        self.assertEqual(self.clean("I think uh, we should ship it."), "I think we should ship it.")
        self.assertEqual(self.clean("I think uh we should ship it."), "I think we should ship it.")

    def test_sentence_start_filler_recapitalizes_the_next_word(self):
        self.assertEqual(self.clean("Um, hello there."), "Hello there.")
        self.assertEqual(self.clean("Yes. Uh, then we left."), "Yes. Then we left.")
        self.assertEqual(self.clean("Um. Hello."), "Hello.")

    def test_sentence_end_punctuation_survives(self):
        self.assertEqual(self.clean("We should go, uh."), "We should go.")
        self.assertEqual(self.clean("I think, uh. We should go."), "I think. We should go.")

    def test_repeated_and_dutch_fillers(self):
        self.assertEqual(self.clean("Dat is uh uhm, ehm goed."), "Dat is goed.")
        self.assertEqual(self.clean("Dat is, eh, gewoon dus goed."), "Dat is, gewoon dus goed.")

    def test_meaningful_words_stay(self):
        self.assertEqual(self.clean("Uh-huh, that is like, well, fine."), "Uh-huh, that is like, well, fine.")
        self.assertEqual(self.clean("Er is een umbrella."), "Er is een umbrella.")
        self.assertEqual(self.clean("Hmm, not sure."), "Hmm, not sure.")

    def test_language_awareness_protects_ordinary_words(self):
        self.assertIn("er", filler_words(["en"]))
        self.assertNotIn("er", filler_words(["en", "nl"]))
        self.assertIn("um", filler_words(["en"]))
        self.assertNotIn("um", filler_words(["en", "de"]))
        self.assertEqual(remove_fillers("Wir gehen um acht.", filler_words(["en", "de"])), "Wir gehen um acht.")

    def test_custom_fillers_and_empty_lists(self):
        self.assertEqual(remove_fillers("So basically, we, basically, left.", filler_words(["en"], "basically")),
                         "So we, left.")
        self.assertEqual(remove_fillers("uh uh", ()), "uh uh")
        self.assertEqual(remove_fillers("", self.words), "")

    def test_newlines_are_preserved(self):
        self.assertEqual(self.clean("First line, uh.\num second line"), "First line.\nSecond line")


class CorrectionTests(unittest.TestCase):
    def test_whole_word_case_insensitive_replacement(self):
        rules = [{"from": "own key", "to": "Ownkey"}, {"from": "bart", "to": "Bart"}]
        self.assertEqual(apply_corrections("Own key is by bart, not bartender.", rules), "Ownkey is by Bart, not bartender.")

    def test_normalization_drops_incomplete_and_duplicate_rules(self):
        rules = normalize_corrections([{"from": " a ", "to": "b"}, {"from": "a", "to": "c"}, ("x", ""), ("same", "Same"), "junk", ["p", "q"]])
        self.assertEqual(rules, [{"from": "a", "to": "b"}, {"from": "same", "to": "Same"}, {"from": "p", "to": "q"}])

    def test_vocabulary_normalization(self):
        self.assertEqual(normalize_vocabulary(["Ownkey", " ownkey ", "", "Orukeet\tv1"]), ["Ownkey", "Orukeet v1"])
        self.assertEqual(normalize_vocabulary("a, b\nc"), ["a", "b", "c"])
        self.assertEqual(normalize_vocabulary(None), [])


class PipelineTests(unittest.TestCase):
    def test_clean_transcript_applies_fillers_then_corrections(self):
        cfg = {"remove_fillers": True, "filler_languages": ["en", "nl"],
               "corrections": [{"from": "own key", "to": "Ownkey"}]}
        self.assertEqual(clean_transcript("Um, own key uh works.", cfg), "Ownkey works.")

    def test_filler_removal_can_be_disabled(self):
        cfg = {"remove_fillers": False, "corrections": []}
        self.assertEqual(clean_transcript("Um, hello.", cfg), "Um, hello.")


if __name__ == "__main__":
    unittest.main()
