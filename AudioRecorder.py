import os
from datetime import datetime
from pathlib import Path

import custom_speech_recognition as sr
import pyaudiowpatch as pyaudio


DOTENV_LOADED = False


def load_dotenv_once():
    global DOTENV_LOADED
    if DOTENV_LOADED:
        return

    env_path = Path(__file__).with_name(".env")
    if env_path.exists():
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


def get_config(name, default):
    load_dotenv_once()
    value = os.environ.get(name)
    if value is not None:
        return value

    try:
        import keys
    except ImportError:
        return default

    value = getattr(keys, name, None)
    if value is None:
        return default
    return str(value)


RECORD_TIMEOUT = float(get_config("ECOUTE_RECORD_TIMEOUT", "1.4"))
ENERGY_THRESHOLD = int(get_config("ECOUTE_ENERGY_THRESHOLD", "300"))
DYNAMIC_ENERGY_THRESHOLD = get_config("ECOUTE_DYNAMIC_ENERGY_THRESHOLD", "1") != "0"
PAUSE_THRESHOLD = float(get_config("ECOUTE_PAUSE_THRESHOLD", "0.65"))
PHRASE_THRESHOLD = float(get_config("ECOUTE_PHRASE_THRESHOLD_SECONDS", "0.25"))
NON_SPEAKING_DURATION = float(get_config("ECOUTE_NON_SPEAKING_DURATION", "0.35"))
NOISE_ADJUST_SECONDS = float(get_config("ECOUTE_NOISE_ADJUST_SECONDS", "0.6"))


class BaseRecorder:
    def __init__(self, source):
        self.recorder = sr.Recognizer()
        self.recorder.energy_threshold = ENERGY_THRESHOLD
        self.recorder.dynamic_energy_threshold = DYNAMIC_ENERGY_THRESHOLD
        self.recorder.pause_threshold = PAUSE_THRESHOLD
        self.recorder.phrase_threshold = PHRASE_THRESHOLD
        self.recorder.non_speaking_duration = NON_SPEAKING_DURATION

        if source is None:
            raise ValueError("audio source can't be None")

        self.source = source

    def adjust_for_noise(self, device_name, msg):
        print(f"[INFO] Adjusting for ambient noise from {device_name}. " + msg)
        with self.source:
            self.recorder.adjust_for_ambient_noise(self.source, duration=NOISE_ADJUST_SECONDS)
        print(f"[INFO] Completed ambient noise adjustment for {device_name}.")

    def record_into_queue(self, audio_queue):
        def record_callback(_, audio: sr.AudioData) -> None:
            data = audio.get_raw_data()
            audio_queue.put((data, datetime.now()))

        self.recorder.listen_in_background(
            self.source,
            record_callback,
            phrase_time_limit=RECORD_TIMEOUT,
        )


class DefaultMicRecorder(BaseRecorder):
    def __init__(self):
        super().__init__(source=sr.Microphone(sample_rate=16000))
        self.adjust_for_noise("Default Mic", "Please stay quiet for a moment...")


class DefaultSpeakerRecorder(BaseRecorder):
    def __init__(self):
        with pyaudio.PyAudio() as p:
            wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_speakers = p.get_device_info_by_index(wasapi_info["defaultOutputDevice"])

            if not default_speakers["isLoopbackDevice"]:
                for loopback in p.get_loopback_device_info_generator():
                    if default_speakers["name"] in loopback["name"]:
                        default_speakers = loopback
                        break
                else:
                    print("[ERROR] No loopback device found.")

        source = sr.Microphone(
            speaker=True,
            device_index=default_speakers["index"],
            sample_rate=int(default_speakers["defaultSampleRate"]),
            chunk_size=pyaudio.get_sample_size(pyaudio.paInt16),
            channels=default_speakers["maxInputChannels"],
        )
        super().__init__(source=source)
        self.adjust_for_noise("Default Speaker", "Please keep speaker audio quiet for a moment...")
