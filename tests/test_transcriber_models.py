import io
import sys
import types
import unittest
from contextlib import redirect_stdout

sys.modules.setdefault(
    "ctranslate2",
    types.SimpleNamespace(get_cuda_device_count=lambda: 0),
)
sys.modules.setdefault(
    "faster_whisper",
    types.SimpleNamespace(WhisperModel=object),
)
sys.modules.setdefault(
    "openai",
    types.SimpleNamespace(OpenAI=object),
)

from TranscriberModels import APIWhisperTranscriber


class FakeAuthError(Exception):
    status_code = 401
    code = "expired_api_key"


class APIWhisperTranscriberTests(unittest.TestCase):
    def test_auth_error_detection_handles_expired_key_response(self):
        error = FakeAuthError("Invalid API Key: expired_api_key")

        self.assertTrue(APIWhisperTranscriber._is_auth_error(error))

    def test_auth_error_disables_api_and_logs_once(self):
        transcriber = APIWhisperTranscriber.__new__(APIWhisperTranscriber)
        transcriber.api_available = True
        transcriber.auth_error_logged = False

        output = io.StringIO()
        with redirect_stdout(output):
            transcriber._disable_api_after_auth_error(FakeAuthError("expired_api_key"))
            transcriber._disable_api_after_auth_error(FakeAuthError("expired_api_key"))

        self.assertFalse(transcriber.api_available)
        self.assertEqual(output.getvalue().count("API key rejected by provider"), 1)

    def test_transcription_mode_selects_groq_whisper_model(self):
        self.assertEqual(APIWhisperTranscriber._model_for_mode("fast"), "whisper-large-v3-turbo")
        self.assertEqual(APIWhisperTranscriber._model_for_mode("accurate"), "whisper-large-v3")
        self.assertEqual(APIWhisperTranscriber._model_for_mode("quality"), "whisper-large-v3")


if __name__ == "__main__":
    unittest.main()
