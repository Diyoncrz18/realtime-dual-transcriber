import time
import unittest
import sys
import types
from datetime import datetime

sys.modules.setdefault("pyaudiowpatch", types.SimpleNamespace(paInt16=8, PyAudio=object))

from AudioTranscriber import AudioTranscriber, TRANSLATION_SILENCE_DELAY


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

    def test_finalize_segment_translates_after_silence(self):
        transcriber = self.make_transcriber()
        transcriber.update_transcript("Speaker", "hello world", datetime.now())

        with transcriber.lock:
            segment = transcriber.segment_state["Speaker"]
            segment["last_activity"] = time.monotonic() - TRANSLATION_SILENCE_DELAY - 0.1
            generation = segment["timer_generation"]

        transcriber.finalize_segment("Speaker", generation)

        entry = self.wait_for_entry(
            transcriber,
            lambda item: not item["translation_pending"],
        )
        self.assertEqual(entry["translation"], "ID: hello world")
        self.assertEqual(entry["translation_state"], "done")

    def test_clear_transcript_resets_public_buffers(self):
        transcriber = self.make_transcriber()
        transcriber.update_transcript("You", "testing one two", datetime.now())

        transcriber.clear_transcript_data()

        self.assertEqual(transcriber.get_entries(), [])
        self.assertEqual(transcriber.mic_buffer, "")
        self.assertEqual(transcriber.speaker_buffer, "")


if __name__ == "__main__":
    unittest.main()
