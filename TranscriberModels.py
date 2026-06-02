import os
import time
from pathlib import Path

import ctranslate2
from faster_whisper import WhisperModel
from openai import OpenAI


DOTENV_LOADED = False
DOTENV_VALUES = {}
PLACEHOLDER_VALUES = {
    "",
    "api key",
    "your-api-key",
    "your-openai-api-key",
    "your-groq-api-key",
    "your-groq-api-key-here",
    "gsk_your-groq-api-key",
}
FAST_TRANSCRIPTION_MODEL = "whisper-large-v3-turbo"
ACCURATE_TRANSCRIPTION_MODEL = "whisper-large-v3"
DEFAULT_TRANSCRIPTION_PROMPT = (
    "This is an English motivational speech. Common words: power of words, adversity, "
    "opportunity, weakness, strength, disabled, differently abled, disability."
)


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
        DOTENV_VALUES[key] = value
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
        groq_key, groq_key_name, groq_key_source = self._get_config_with_source("GROQ_API_KEY")
        fallback_key, fallback_key_name, fallback_key_source = self._get_config_with_source(
            "OPENAI_API_KEY",
            "CUSTOM_API_KEY",
        )
        if api_key:
            api_key_name = "constructor api_key"
            api_key_source = "runtime argument"
        elif groq_key:
            api_key = groq_key
            api_key_name = groq_key_name
            api_key_source = groq_key_source
        else:
            api_key = fallback_key
            api_key_name = fallback_key_name
            api_key_source = fallback_key_source

        self.api_key_config_name = api_key_name
        self.api_key_config_source = api_key_source

        base_url = (
            base_url
            or self._get_config("GROQ_BASE_URL", "OPENAI_BASE_URL", "CUSTOM_API_BASE_URL", "CUSTOM_API_URL")
            or ("https://api.groq.com/openai/v1" if groq_key else None)
        )

        configured_transcription_mode = self._get_config("RTDT_TRANSCRIPTION_MODE", "GROQ_TRANSCRIPTION_MODE")
        self.transcription_mode = self._normalize_transcription_mode(configured_transcription_mode or "fast")
        configured_transcription_model = model or self._get_config(
            "GROQ_TRANSCRIPTION_MODEL",
            "OPENAI_TRANSCRIPTION_MODEL",
            "CUSTOM_API_TRANSCRIPTION_MODEL",
        )
        is_groq_provider = bool(groq_key or self._is_groq_url(base_url))
        mode_selected_model = (
            self._model_for_mode(self.transcription_mode)
            if self.transcription_mode in {"fast", "accurate"}
            else None
        )
        selected_transcription_model = (
            mode_selected_model
            if configured_transcription_mode and mode_selected_model and is_groq_provider
            else configured_transcription_model
            or (mode_selected_model if is_groq_provider else "whisper-1")
        )
        self.transcription_model = self._normalize_model_name(
            selected_transcription_model
            or (FAST_TRANSCRIPTION_MODEL if is_groq_provider else "whisper-1")
        )
        self.translation_model = self._normalize_model_name(
            self._get_config("GROQ_TRANSLATION_MODEL", "OPENAI_TRANSLATION_MODEL", "CUSTOM_API_TRANSLATION_MODEL")
            or "llama-3.1-8b-instant"
        )
        self.transcription_language = self._optional_config(
            "RTDT_TRANSCRIPTION_LANGUAGE",
            "GROQ_TRANSCRIPTION_LANGUAGE",
            "OPENAI_TRANSCRIPTION_LANGUAGE",
            "CUSTOM_API_TRANSCRIPTION_LANGUAGE",
        )
        self.transcription_prompt = self._prompt_config(
            "RTDT_TRANSCRIPTION_PROMPT",
            "GROQ_TRANSCRIPTION_PROMPT",
            "OPENAI_TRANSCRIPTION_PROMPT",
            "CUSTOM_API_TRANSCRIPTION_PROMPT",
        )
        self.transcription_temperature = self._float_config(
            "RTDT_TRANSCRIPTION_TEMPERATURE",
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
        self._validate_api_key(api_key)

        client_options = {}
        if api_key:
            client_options["api_key"] = api_key
        if base_url:
            client_options["base_url"] = base_url

        self.client = OpenAI(**client_options)
        print(f"[INFO] API transcription mode: {self.transcription_mode}")
        print(f"[INFO] API transcription model: {self.transcription_model}")
        print(f"[INFO] API translation model: {self.translation_model}")
        if self.api_key_config_name:
            print(f"[INFO] API key source: {self.api_key_config_name} from {self.api_key_config_source}")
        if base_url:
            print(f"[INFO] API base URL: {base_url}")
        if self.transcription_language:
            print(f"[INFO] API transcription language: {self.transcription_language}")
        else:
            print("[INFO] API transcription language: auto")
        if self.transcription_prompt:
            print("[INFO] API transcription prompt: enabled")

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

    @staticmethod
    def _get_config_with_source(*names, default=None):
        load_dotenv_once()
        for name in names:
            value = os.environ.get(name)
            if value:
                source = ".env" if DOTENV_VALUES.get(name) == value else "environment"
                return value, name, source

        try:
            import keys
        except ImportError:
            return default, None, None

        for name in names:
            value = getattr(keys, name, None)
            if value:
                return value, name, "keys.py"

        return default, None, None

    @classmethod
    def _optional_config(cls, *names):
        value = cls._get_config(*names)
        if not value or str(value).lower() in {"auto", "none", "null"}:
            return None
        return str(value)

    @classmethod
    def _prompt_config(cls, *names):
        value = cls._get_config(*names)
        if value is None:
            return DEFAULT_TRANSCRIPTION_PROMPT
        if not value or str(value).lower() in {"auto", "none", "null", "off", "0"}:
            return None
        return str(value)

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
        model = str(model)
        if model.startswith("groq/"):
            return model.split("/", 1)[1]
        return model

    @staticmethod
    def _normalize_transcription_mode(mode):
        normalized = (mode or "fast").strip().lower()
        if normalized in {"accurate", "accuracy", "quality", "high"}:
            return "accurate"
        if normalized in {"custom", "manual", "model"}:
            return "custom"
        return "fast"

    @staticmethod
    def _model_for_mode(mode):
        if APIWhisperTranscriber._normalize_transcription_mode(mode) == "accurate":
            return ACCURATE_TRANSCRIPTION_MODEL
        return FAST_TRANSCRIPTION_MODEL

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

    def _validate_api_key(self, api_key):
        if api_key and str(api_key).strip().lower() not in PLACEHOLDER_VALUES:
            return

        self.api_available = False
        expected_key = self.api_key_config_name or "GROQ_API_KEY"
        expected_source = self.api_key_config_source or ".env or keys.py"
        print(
            "[ERROR] API key is missing or still a placeholder. "
            f"Set a valid {expected_key} in {expected_source}, then restart realtime-dual-transcriber."
        )

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
        key_hint = getattr(self, "api_key_config_name", None) or "GROQ_API_KEY or OPENAI_API_KEY"
        source_hint = getattr(self, "api_key_config_source", None) or ".env or keys.py"
        print(
            "[ERROR] API key rejected by provider. "
            f"Update {key_hint} in {source_hint}, then restart realtime-dual-transcriber. "
            f"Provider response: {error}"
        )

    def _log_error(self, message):
        now = time.monotonic()
        if message != self.last_error or now - self.last_error_time > 10:
            print(f"[ERROR] {message}")
            self.last_error = message
            self.last_error_time = now
