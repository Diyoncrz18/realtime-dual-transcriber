import unittest
import sys
import types
import time
from datetime import datetime, timedelta

sys.modules.setdefault("pyaudiowpatch", types.SimpleNamespace(paInt16=8, PyAudio=object))

from AudioTranscriber import AudioTranscriber, PHRASE_TIMEOUT


class FakeSource:
    SAMPLE_RATE = 16000
    SAMPLE_WIDTH = 2
    channels = 1


class FakeModel:
    def translate_to_indonesian(self, text):
        return f"ID: {text}"


class AudioTranscriberTests(unittest.TestCase):
    def make_transcriber(self):
        return AudioTranscriber(FakeSource(), FakeSource(), FakeModel())

    def wait_for_entry(self, transcriber, predicate, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            entries = transcriber.get_entries()
            if entries and predicate(entries[0]):
                return entries[0]
            time.sleep(0.01)
        self.fail("Timed out waiting for transcript entry state")

    def test_merge_transcript_keeps_overlapping_words_once(self):
        merged = AudioTranscriber._merge_transcript(
            "hello this is a live transcription",
            "live transcription test",
        )

        self.assertEqual(merged, "hello this is a live transcription test")

    def test_finish_entry_translates_selected_segment(self):
        transcriber = self.make_transcriber()
        transcriber.update_transcript("Speaker", "hello world", datetime.now())

        entry_id = transcriber.get_entries()[0]["id"]
        transcriber.finish_entry(entry_id)

        entry = self.wait_for_entry(
            transcriber,
            lambda item: not item["translation_pending"],
        )
        self.assertEqual(entry["translation"], "ID: hello world")
        self.assertEqual(entry["translation_state"], "done")

    def test_phrase_timeout_starts_new_card_without_translation(self):
        transcriber = self.make_transcriber()
        first_time = datetime.now()
        second_time = first_time + timedelta(seconds=PHRASE_TIMEOUT + 0.1)

        transcriber.update_last_sample_and_phrase_status("You", b"\x01\x00" * 16000, first_time)
        transcriber.update_transcript("You", "first phrase", first_time)
        transcriber.update_last_sample_and_phrase_status("You", b"\x01\x00" * 16000, second_time)
        transcriber.update_transcript("You", "second phrase", second_time)

        entries = transcriber.get_entries()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["original"], "first phrase")
        self.assertEqual(entries[0]["translation_state"], "waiting")
        self.assertEqual(entries[1]["original"], "second phrase")

    def test_clear_transcript_resets_public_buffers(self):
        transcriber = self.make_transcriber()
        transcriber.update_transcript("You", "testing one two", datetime.now())

        transcriber.clear_transcript_data()

        self.assertEqual(transcriber.get_entries(), [])
        self.assertEqual(transcriber.mic_buffer, "")
        self.assertEqual(transcriber.speaker_buffer, "")


if __name__ == "__main__":
    unittest.main()
