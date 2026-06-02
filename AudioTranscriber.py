import audioop
import io
import os
import queue
import re
import tempfile
import threading
import time
import wave
from datetime import timedelta
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


PHRASE_TIMEOUT = float(get_config("RTDT_PHRASE_TIMEOUT", "5.0"))
MAX_PHRASES = int(get_config("RTDT_MAX_PHRASES", "12"))
MIN_AUDIO_SECONDS = float(get_config("RTDT_MIN_AUDIO_SECONDS", "0.45"))
MIN_AUDIO_RMS = int(get_config("RTDT_MIN_AUDIO_RMS", "120"))
TRANSLATION_SILENCE_DELAY = max(
    PHRASE_TIMEOUT,
    float(get_config("RTDT_TRANSLATION_SILENCE_DELAY", str(PHRASE_TIMEOUT))),
)
TRANSLATION_WAITING_TEXT = "⏳ Menunggu pembicara berhenti..."
TRANSLATION_RUNNING_TEXT = "Menerjemahkan..."
TRANSLATION_PENDING_TEXT = TRANSLATION_WAITING_TEXT
NOISE_TEXTS = {
    "uh",
    "um",
    "umm",
    "hmm",
    "mm",
    "mmm",
    "ah",
    "eh",
    "you",
    "thank you",
    "thanks for watching",
}


class AudioTranscriber:
    def __init__(self, mic_source, speaker_source, model):
        self.transcript_data = {"You": [], "Speaker": []}
        self.transcript_changed_event = threading.Event()
        self.audio_model = model
        self.debug = os.environ.get("RTDT_DEBUG", "0") == "1"
        self.lock = threading.RLock()
        self.revision = 0
        self.next_transcript_id = 1
        self.translation_cache = {}
        self.translation_queue = queue.Queue()
        self.segment_state = {
            "You": self._new_segment_state(),
            "Speaker": self._new_segment_state(),
        }
        self.active_mic_segment = self.segment_state["You"]
        self.active_speaker_segment = self.segment_state["Speaker"]
        self.mic_buffer = ""
        self.speaker_buffer = ""
        self.text_buffers = {"You": "", "Speaker": ""}
        self.last_text_activity = {"You": None, "Speaker": None}
        self.last_audio_activity = {"You": None, "Speaker": None}
        self.audio_sources = {
            "You": {
                "sample_rate": mic_source.SAMPLE_RATE,
                "sample_width": mic_source.SAMPLE_WIDTH,
                "channels": mic_source.channels,
                "last_sample": bytes(),
                "last_spoken": None,
                "new_phrase": True,
                "active_entry_id": None,
                "process_data_func": self.process_mic_data,
            },
            "Speaker": {
                "sample_rate": speaker_source.SAMPLE_RATE,
                "sample_width": speaker_source.SAMPLE_WIDTH,
                "channels": speaker_source.channels,
                "last_sample": bytes(),
                "last_spoken": None,
                "new_phrase": True,
                "active_entry_id": None,
                "process_data_func": self.process_speaker_data,
            },
        }
        self.translation_thread = threading.Thread(target=self._translation_worker, daemon=True)
        self.translation_thread.start()

    def transcribe_audio_queue(self, speaker_queue, mic_queue):
        while True:
            pending_transcriptions = []
            processing_sources = []
            mic_data = self._drain_audio_queue("You", mic_queue)
            speaker_data = self._drain_audio_queue("Speaker", speaker_queue)

            try:
                if mic_data:
                    processing_sources.append("You")
                    self._set_segment_transcribing("You", True)
                    transcription = self._transcribe_source("You", mic_data)
                    if transcription:
                        pending_transcriptions.append(transcription)

                if speaker_data:
                    processing_sources.append("Speaker")
                    self._set_segment_transcribing("Speaker", True)
                    transcription = self._transcribe_source("Speaker", speaker_data)
                    if transcription:
                        pending_transcriptions.append(transcription)

                if pending_transcriptions:
                    pending_transcriptions.sort(key=lambda x: x[2])
                    for who_spoke, text, time_spoken in pending_transcriptions:
                        self.update_transcript(who_spoke, text, time_spoken)
            finally:
                for who_spoke in processing_sources:
                    self._set_segment_transcribing(who_spoke, False)

            threading.Event().wait(0.1)

    def _drain_audio_queue(self, who_spoke, audio_queue):
        audio_data = []
        while True:
            try:
                data, time_spoken = audio_queue.get_nowait()
                self.update_last_sample_and_phrase_status(who_spoke, data, time_spoken)
                audio_data.append((data, time_spoken))
            except queue.Empty:
                return audio_data

    def _transcribe_source(self, who_spoke, audio_data):
        source_info = self.audio_sources[who_spoke]
        sample = b"".join(data for data, _ in audio_data)

        if self._should_skip_audio(who_spoke, sample):
            return None

        if self.debug:
            duration = self._audio_duration_seconds(who_spoke, sample)
            rms = self._audio_rms(who_spoke, sample)
            print(f"[DEBUG] {who_spoke} audio: {duration:.2f}s rms={rms}")

        path = None
        try:
            fd, path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            source_info["process_data_func"](sample, path)
            text = self._normalize_text(self.audio_model.get_transcription(path))
            if text and text.lower() != "you":
                latest_time = max(time for _, time in audio_data)
                return who_spoke, text, latest_time
            if self.debug:
                print(f"[DEBUG] {who_spoke} transcription returned empty text.")
        except Exception as e:
            print(f"[ERROR] Transcription error for {who_spoke}: {e}")
        finally:
            if path and os.path.exists(path):
                os.unlink(path)

        return None

    def update_last_sample_and_phrase_status(self, who_spoke, data, time_spoken):
        source_info = self.audio_sources[who_spoke]
        last_spoken = source_info["last_spoken"]
        should_start_new_phrase = (
            last_spoken is None
            or time_spoken - last_spoken > timedelta(seconds=PHRASE_TIMEOUT)
        )

        if should_start_new_phrase:
            source_info["last_sample"] = bytes()
            source_info["new_phrase"] = True
        else:
            source_info["new_phrase"] = False

        source_info["last_sample"] += data
        source_info["last_spoken"] = time_spoken
        self.last_audio_activity[who_spoke] = time.monotonic()

    def process_mic_data(self, data, temp_file_name):
        audio_data = sr.AudioData(
            data,
            self.audio_sources["You"]["sample_rate"],
            self.audio_sources["You"]["sample_width"],
        )
        wav_data = io.BytesIO(audio_data.get_wav_data())
        with open(temp_file_name, "w+b") as f:
            f.write(wav_data.read())

    def process_speaker_data(self, data, temp_file_name):
        with wave.open(temp_file_name, "wb") as wf:
            wf.setnchannels(self.audio_sources["Speaker"]["channels"])
            p = pyaudio.PyAudio()
            wf.setsampwidth(p.get_sample_size(pyaudio.paInt16))
            wf.setframerate(self.audio_sources["Speaker"]["sample_rate"])
            wf.writeframes(data)

    def update_transcript(self, who_spoke, text, time_spoken):
        text = self._normalize_text(text)
        if not self._is_meaningful_text(text):
            return

        with self.lock:
            segment = self.segment_state[who_spoke]
            transcript = self.transcript_data[who_spoke]

            entry = None
            if segment["is_finalized"] or segment["active_card_id"] is None:
                entry = self._new_entry(who_spoke, "", time_spoken)
                transcript.append(entry)
                segment["active_card_id"] = entry["id"]
                segment["buffer"] = ""
                segment["is_finalized"] = False
                if len(transcript) > MAX_PHRASES:
                    transcript.pop(0)
            else:
                entry = self._find_entry(segment["active_card_id"])
                if entry is None:
                    entry = self._new_entry(who_spoke, "", time_spoken)
                    transcript.append(entry)
                    segment["active_card_id"] = entry["id"]
                    segment["buffer"] = ""
                    segment["is_finalized"] = False

            merged_text = self._merge_transcript(segment["buffer"], text)
            visual_changed = (
                entry["original"] != merged_text
                or entry["translation"] != TRANSLATION_WAITING_TEXT
                or entry["translation_state"] != "waiting"
            )

            segment["buffer"] = merged_text
            segment["last_activity"] = time.monotonic()
            entry["original"] = merged_text
            entry["translation"] = TRANSLATION_WAITING_TEXT
            entry["translation_pending"] = True
            entry["translation_state"] = "waiting"
            entry["translation_requested_for"] = None
            entry["translated_original"] = None

            self.text_buffers[who_spoke] = merged_text
            if who_spoke == "You":
                self.mic_buffer = merged_text
            else:
                self.speaker_buffer = merged_text
            self.last_text_activity[who_spoke] = segment["last_activity"]
            self.audio_sources[who_spoke]["active_entry_id"] = segment["active_card_id"]
            self._schedule_translation_timer_locked(who_spoke)
            if visual_changed:
                self._bump_revision()

    def get_entries(self):
        with self.lock:
            entries = [entry.copy() for entry in self.transcript_data["You"] + self.transcript_data["Speaker"]]
        return sorted(entries, key=lambda x: x["timestamp"])[-MAX_PHRASES:]

    def get_transcript(self):
        return "".join(self.format_entry(entry) for entry in self.get_entries())

    def get_revision(self):
        with self.lock:
            return self.revision

    def format_entry(self, entry):
        label = "[YOU / MIC]" if entry["speaker"] == "You" else "[SPEAKER]"
        timestamp = entry["timestamp"].strftime("%H:%M:%S")
        translation = entry.get("translation") or entry["original"]
        return (
            f"{label} {timestamp}\n"
            f"Original:\n{entry['original']}\n\n"
            f"Terjemahan:\n{translation}\n\n"
        )

    def clear_transcript_data(self):
        with self.lock:
            self.transcript_data["You"].clear()
            self.transcript_data["Speaker"].clear()

            self.audio_sources["You"]["last_sample"] = bytes()
            self.audio_sources["Speaker"]["last_sample"] = bytes()

            self.audio_sources["You"]["new_phrase"] = True
            self.audio_sources["Speaker"]["new_phrase"] = True
            self.audio_sources["You"]["active_entry_id"] = None
            self.audio_sources["Speaker"]["active_entry_id"] = None
            self._reset_segment_locked("You")
            self._reset_segment_locked("Speaker")
            self.text_buffers["You"] = ""
            self.text_buffers["Speaker"] = ""
            self.mic_buffer = ""
            self.speaker_buffer = ""
            self.last_text_activity["You"] = None
            self.last_text_activity["Speaker"] = None
            self.last_audio_activity["You"] = None
            self.last_audio_activity["Speaker"] = None
            self._bump_revision()

    def _new_entry(self, who_spoke, text, time_spoken):
        entry = {
            "id": self.next_transcript_id,
            "speaker": who_spoke,
            "original": text,
            "translation": TRANSLATION_WAITING_TEXT,
            "translation_pending": True,
            "translation_state": "waiting",
            "translation_requested_for": None,
            "translated_original": None,
            "timestamp": time_spoken,
        }
        self.next_transcript_id += 1
        return entry

    @staticmethod
    def _new_segment_state():
        return {
            "active_card_id": None,
            "buffer": "",
            "last_activity": 0.0,
            "translation_timer": None,
            "timer_generation": 0,
            "is_finalized": True,
            "is_transcribing": False,
        }

    def _set_segment_transcribing(self, who_spoke, value):
        with self.lock:
            self.segment_state[who_spoke]["is_transcribing"] = value

    def _schedule_translation_timer_locked(self, who_spoke, delay=TRANSLATION_SILENCE_DELAY):
        segment = self.segment_state[who_spoke]
        timer = segment.get("translation_timer")
        if timer:
            timer.cancel()

        segment["timer_generation"] += 1
        generation = segment["timer_generation"]
        timer = threading.Timer(delay, self.finalize_segment, args=(who_spoke, generation))
        timer.daemon = True
        segment["translation_timer"] = timer
        timer.start()

    def finalize_segment(self, who_spoke, generation=None):
        ready_translation = None

        with self.lock:
            segment = self.segment_state[who_spoke]
            if generation is not None and generation != segment["timer_generation"]:
                return
            if segment["is_finalized"] or segment["active_card_id"] is None:
                return

            now = time.monotonic()
            elapsed = now - segment["last_activity"]
            if segment["is_transcribing"]:
                self._schedule_translation_timer_locked(who_spoke, delay=0.25)
                return
            if elapsed < TRANSLATION_SILENCE_DELAY:
                self._schedule_translation_timer_locked(
                    who_spoke,
                    delay=max(0.1, TRANSLATION_SILENCE_DELAY - elapsed),
                )
                return

            entry_id = segment["active_card_id"]
            full_text = self._normalize_text(segment["buffer"])
            entry = self._find_entry(entry_id)
            if not entry:
                self._reset_segment_locked(who_spoke)
                return

            if not self._is_meaningful_text(full_text):
                self._remove_entry_locked(entry_id)
                self._reset_segment_locked(who_spoke)
                self._bump_revision()
                return

            entry["original"] = full_text
            entry["translation"] = TRANSLATION_RUNNING_TEXT
            entry["translation_pending"] = True
            entry["translation_state"] = "translating"
            entry["translation_requested_for"] = full_text
            self._reset_segment_locked(who_spoke, keep_transcribing=True)
            ready_translation = (entry_id, full_text)
            self._bump_revision()

        if ready_translation:
            self._queue_translation(*ready_translation)

    def _reset_segment_locked(self, who_spoke, keep_transcribing=False):
        segment = self.segment_state[who_spoke]
        timer = segment.get("translation_timer")
        if timer:
            timer.cancel()
        is_transcribing = segment["is_transcribing"] if keep_transcribing else False
        segment["timer_generation"] += 1
        segment.update(
            {
                "active_card_id": None,
                "buffer": "",
                "last_activity": 0.0,
                "translation_timer": None,
                "is_finalized": True,
                "is_transcribing": is_transcribing,
            }
        )
        self.audio_sources[who_spoke]["active_entry_id"] = None
        self.text_buffers[who_spoke] = ""
        if who_spoke == "You":
            self.mic_buffer = ""
        else:
            self.speaker_buffer = ""

    def _queue_translation(self, entry_id, text):
        if not self._is_meaningful_text(text):
            self._apply_translation(entry_id, text, "")
            return

        cache_key = self._cache_key(text)
        cached_translation = self.translation_cache.get(cache_key)
        if cached_translation is not None:
            self._apply_translation(entry_id, text, cached_translation)
            return

        self.translation_queue.put((entry_id, text))

    def _translation_worker(self):
        while True:
            entry_id, original = self.translation_queue.get()
            with self.lock:
                entry = self._find_entry(entry_id)
                if not entry or entry["original"] != original or entry.get("translation_state") != "translating":
                    continue

            cache_key = self._cache_key(original)
            translation = self.translation_cache.get(cache_key)
            if translation is None:
                translation = self._translate_text(original)
                self.translation_cache[cache_key] = translation
            self._apply_translation(entry_id, original, translation)

    def _translate_text(self, text):
        translator = getattr(self.audio_model, "translate_to_indonesian", None)
        if not translator:
            return text

        try:
            translated = self._normalize_text(translator(text))
            return translated or text
        except Exception as e:
            print(f"[WARN] Translation failed: {e}")
            return text

    def _apply_translation(self, entry_id, original, translation):
        with self.lock:
            entry = self._find_entry(entry_id)
            if not entry or entry["original"] != original:
                return

            entry["translation"] = translation or ""
            entry["translation_pending"] = False
            entry["translation_state"] = "done"
            entry["translated_original"] = original
            entry["translation_requested_for"] = None
            source_info = self.audio_sources.get(entry["speaker"])
            if source_info and source_info.get("active_entry_id") == entry_id:
                source_info["active_entry_id"] = None
            self._bump_revision()

    def _find_entry(self, entry_id):
        for transcript in self.transcript_data.values():
            for entry in transcript:
                if entry["id"] == entry_id:
                    return entry
        return None

    def _remove_entry_locked(self, entry_id):
        for transcript in self.transcript_data.values():
            for index, entry in enumerate(transcript):
                if entry["id"] == entry_id:
                    del transcript[index]
                    return

    def _bump_revision(self):
        self.revision += 1
        self.transcript_changed_event.set()

    def _should_skip_audio(self, who_spoke, data):
        duration = self._audio_duration_seconds(who_spoke, data)
        if duration < MIN_AUDIO_SECONDS:
            if self.debug:
                print(f"[DEBUG] Skipping {who_spoke}: too short ({duration:.2f}s).")
            return True

        rms = self._audio_rms(who_spoke, data)
        if rms < MIN_AUDIO_RMS:
            if self.debug:
                print(f"[DEBUG] Skipping {who_spoke}: likely silence/noise (rms={rms}).")
            return True

        return False

    def _audio_duration_seconds(self, who_spoke, data):
        if not data:
            return 0
        source_info = self.audio_sources[who_spoke]
        bytes_per_second = (
            source_info["sample_rate"]
            * source_info["sample_width"]
            * max(source_info["channels"], 1)
        )
        return len(data) / bytes_per_second if bytes_per_second else 0

    def _audio_rms(self, who_spoke, data):
        if not data:
            return 0
        sample_width = self.audio_sources[who_spoke]["sample_width"]
        try:
            return audioop.rms(data, sample_width)
        except audioop.error:
            return 0

    @staticmethod
    def _normalize_text(text):
        return re.sub(r"\s+", " ", text or "").strip()

    @classmethod
    def _merge_transcript(cls, old, new):
        old = cls._normalize_text(old)
        new = cls._normalize_text(new)
        if not old:
            return new
        if not new:
            return old

        old_key = cls._text_key(old)
        new_key = cls._text_key(new)
        if old_key == new_key:
            return old
        if new_key.startswith(old_key):
            return new

        old_words = old.split()
        new_words = new.split()
        old_word_keys = [cls._word_key(word) for word in old_words]
        new_word_keys = [cls._word_key(word) for word in new_words]
        if len(new_word_keys) <= len(old_word_keys) and old_word_keys[-len(new_word_keys):] == new_word_keys:
            return old

        max_overlap = min(len(old_words), len(new_words))
        for size in range(max_overlap, 0, -1):
            if old_word_keys[-size:] == new_word_keys[:size]:
                return cls._normalize_text(" ".join(old_words + new_words[size:]))

        return cls._normalize_text(f"{old} {new}")

    @staticmethod
    def _word_key(word):
        return re.sub(r"[^0-9A-Za-zÀ-ÿ]+", "", word).casefold()

    @classmethod
    def _text_key(cls, text):
        return " ".join(cls._word_key(word) for word in cls._normalize_text(text).split())

    @staticmethod
    def _cache_key(text):
        return AudioTranscriber._normalize_text(text).casefold()

    @staticmethod
    def _is_meaningful_text(text):
        normalized = AudioTranscriber._normalize_text(text)
        if not normalized:
            return False
        if normalized.strip(".,!?;:-_()[]{}'\" ") == "":
            return False
        noise_key = re.sub(r"[^0-9A-Za-zÀ-ÿ ]+", "", normalized).casefold().strip()
        if noise_key in NOISE_TEXTS:
            return False
        meaningful_chars = re.findall(r"[A-Za-z0-9À-ÿ]", normalized)
        return len(meaningful_chars) >= 3
