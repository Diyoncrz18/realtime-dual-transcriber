import os
import time
from pathlib import Path

import ctranslate2
from faster_whisper import WhisperModel
from openai import OpenAI


DOTENV_LOADED = False


def get_model(use_api):
    if use_api:
        return APIWhisperTranscriber()
    return FasterWhisperTranscriber()


def load_dotenv_once():
    global DOTENV_LOADED
    if DOTENV_LOADED:
        return

    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        DOTENV_LOADED = True
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value

    DOTENV_LOADED = True


class FasterWhisperTranscriber:
    def __init__(self):
        print("[INFO] Loading Faster Whisper model...")
        device = self._get_device()
        compute_type = "float16" if device == "cuda" else "int8"
        try:
            self.model = WhisperModel("tiny.en", device=device, compute_type=compute_type)
            self.device = device
        except Exception as e:
            if device != "cuda":
                raise
            print(f"[WARN] CUDA model load failed, falling back to CPU: {e}")
            self.model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
            self.device = "cpu"
        print(f"[INFO] Faster Whisper device: {self.device}")

    @staticmethod
    def _get_device():
        try:
            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda"
        except Exception as e:
            print(f"[WARN] CUDA detection failed, falling back to CPU: {e}")
        return "cpu"

    def get_transcription(self, wav_file_path):
        try:
            segments, _ = self.model.transcribe(wav_file_path, beam_size=5)
            full_text = " ".join(segment.text for segment in segments)
            return full_text.strip()
        except Exception as e:
            print(e)
            return ""


class APIWhisperTranscriber:
    def __init__(self, api_key=None, base_url=None, model=None):
        groq_key = self._get_config("GROQ_API_KEY")
        api_key = api_key or groq_key or self._get_config("OPENAI_API_KEY", "CUSTOM_API_KEY")
        base_url = (
            base_url
            or self._get_config("GROQ_BASE_URL", "OPENAI_BASE_URL", "CUSTOM_API_BASE_URL", "CUSTOM_API_URL")
            or ("https://api.groq.com/openai/v1" if groq_key else None)
        )

        self.transcription_model = self._normalize_model_name(
            model
            or self._get_config("GROQ_TRANSCRIPTION_MODEL", "OPENAI_TRANSCRIPTION_MODEL", "CUSTOM_API_TRANSCRIPTION_MODEL")
            or ("whisper-large-v3-turbo" if groq_key or self._is_groq_url(base_url) else "whisper-1")
        )
        self.translation_model = self._normalize_model_name(
            self._get_config("GROQ_TRANSLATION_MODEL", "OPENAI_TRANSLATION_MODEL", "CUSTOM_API_TRANSLATION_MODEL")
            or "llama-3.1-8b-instant"
        )
        self.transcription_language = self._optional_config(
            "GROQ_TRANSCRIPTION_LANGUAGE",
            "OPENAI_TRANSCRIPTION_LANGUAGE",
            "CUSTOM_API_TRANSCRIPTION_LANGUAGE",
        )
        self.transcription_prompt = self._optional_config(
            "GROQ_TRANSCRIPTION_PROMPT",
            "OPENAI_TRANSCRIPTION_PROMPT",
            "CUSTOM_API_TRANSCRIPTION_PROMPT",
        )
        self.transcription_temperature = self._float_config(
            "GROQ_TRANSCRIPTION_TEMPERATURE",
            "OPENAI_TRANSCRIPTION_TEMPERATURE",
            "CUSTOM_API_TRANSCRIPTION_TEMPERATURE",
            default=0.0,
        )
        self.translation_enabled = self._get_config("RTDT_ENABLE_TRANSLATION", default="1") != "0"
        self.last_error = None
        self.last_error_time = 0
        self.api_available = True
        self.auth_error_logged = False

        client_options = {}
        if api_key:
            client_options["api_key"] = api_key
        if base_url:
            client_options["base_url"] = base_url

        self.client = OpenAI(**client_options)
        print(f"[INFO] API transcription model: {self.transcription_model}")
        print(f"[INFO] API translation model: {self.translation_model}")
        if base_url:
            print(f"[INFO] API base URL: {base_url}")
        if self.transcription_language:
            print(f"[INFO] API transcription language: {self.transcription_language}")

    @staticmethod
    def _get_config(*names, default=None):
        load_dotenv_once()
        for name in names:
            value = os.environ.get(name)
            if value:
                return value

        try:
            import keys
        except ImportError:
            return default

        for name in names:
            value = getattr(keys, name, None)
            if value:
                return value

        return default

    @classmethod
    def _optional_config(cls, *names):
        value = cls._get_config(*names)
        if not value or value.lower() in {"auto", "none", "null"}:
            return None
        return value

    @classmethod
    def _float_config(cls, *names, default):
        value = cls._get_config(*names)
        if value is None:
            return default
        try:
            return float(value)
        except ValueError:
            return default

    @staticmethod
    def _is_groq_url(base_url):
        return bool(base_url and "groq.com" in base_url.lower())

    @staticmethod
    def _normalize_model_name(model):
        if model.startswith("groq/"):
            return model.split("/", 1)[1]
        return model

    def get_transcription(self, wav_file_path):
        if not self.api_available:
            return ""

        try:
            with open(wav_file_path, "rb") as audio_file:
                request = {
                    "model": self.transcription_model,
                    "file": audio_file,
                    "temperature": self.transcription_temperature,
                }
                if self.transcription_language:
                    request["language"] = self.transcription_language
                if self.transcription_prompt:
                    request["prompt"] = self.transcription_prompt

                result = self.client.audio.transcriptions.create(**request)

            text = self._extract_text(result)
            if not text:
                self._log_error(f"API transcription returned no text: {result}")
            return text
        except Exception as e:
            if self._is_auth_error(e):
                self._disable_api_after_auth_error(e)
                return ""

            self._log_error(f"API transcription failed: {e}")
            return ""

    def translate_to_indonesian(self, text):
        if not self.api_available or not self.translation_enabled or not text:
            return text

        try:
            prompt = (
                "Translate the text to natural Indonesian. Only translate. "
                "Do not explain, expand, summarize, answer, or add information. "
                "Preserve mixed-language meaning naturally. "
                "If the input is already Indonesian, lightly clean grammar only. "
                "Return only the final Indonesian text.\n\n"
                f"Text: {text}"
            )
            completion = self.client.chat.completions.create(
                model=self.translation_model,
                temperature=0,
                max_tokens=max(64, min(512, len(text.split()) * 6 + 32)),
                messages=[{"role": "user", "content": prompt}],
            )
            return self._clean_translation_output(completion.choices[0].message.content)
        except Exception as e:
            if self._is_auth_error(e):
                self._disable_api_after_auth_error(e)
                return text

            self._log_error(f"API translation failed: {e}")
            return text

    @staticmethod
    def _extract_text(result):
        text = getattr(result, "text", None)
        if text:
            return text.strip()

        if isinstance(result, dict):
            text = result.get("text")
            if text:
                return text.strip()

        if isinstance(result, str):
            return result.strip()

        return ""

    @staticmethod
    def _clean_translation_output(text):
        cleaned = (text or "").strip()
        cleaned = cleaned.strip("\"'")
        prefixes = ("Terjemahan:", "Translation:", "Indonesian:", "Bahasa Indonesia:")
        for prefix in prefixes:
            if cleaned.lower().startswith(prefix.lower()):
                return cleaned[len(prefix):].strip()
        return cleaned

    @staticmethod
    def _is_auth_error(error):
        status_code = getattr(error, "status_code", None)
        code = getattr(error, "code", None)
        message = str(error).lower()

        return (
            status_code == 401
            or code in {"invalid_api_key", "expired_api_key"}
            or "invalid api key" in message
            or "expired_api_key" in message
            or "incorrect api key" in message
        )

    def _disable_api_after_auth_error(self, error):
        self.api_available = False
        if self.auth_error_logged:
            return

        self.auth_error_logged = True
        print(
            "[ERROR] API key rejected by provider. "
            "Update GROQ_API_KEY in .env or keys.py, then restart realtime-dual-transcriber. "
            f"Provider response: {error}"
        )

    def _log_error(self, message):
        now = time.monotonic()
        if message != self.last_error or now - self.last_error_time > 10:
            print(f"[ERROR] {message}")
            self.last_error = message
            self.last_error_time = now
