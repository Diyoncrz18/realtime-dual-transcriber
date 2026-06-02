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


PHRASE_TIMEOUT = float(get_config("RTDT_PHRASE_TIMEOUT", "3.0"))
MAX_PHRASES = int(get_config("RTDT_MAX_PHRASES", "12"))
MIN_AUDIO_SECONDS = float(get_config("RTDT_MIN_AUDIO_SECONDS", "0.30"))
MIN_AUDIO_RMS = int(get_config("RTDT_MIN_AUDIO_RMS", "120"))
MIN_MIC_AUDIO_RMS = int(get_config("RTDT_MIN_MIC_RMS", str(MIN_AUDIO_RMS)))
MIN_SPEAKER_AUDIO_RMS = int(get_config("RTDT_MIN_SPEAKER_RMS", "90"))
TRANSLATION_SILENCE_DELAY = max(
    PHRASE_TIMEOUT,
    float(get_config("RTDT_TRANSLATION_SILENCE_DELAY", str(PHRASE_TIMEOUT))),
)
TRANSLATION_WAITING_TEXT = "Klik Selesai untuk menerjemahkan."
TRANSLATION_RUNNING_TEXT = "Menerjemahkan..."
TRANSLATION_PENDING_TEXT = TRANSLATION_WAITING_TEXT
SPEAKING_STATUS_SECONDS = float(get_config("RTDT_SPEAKING_STATUS_SECONDS", "1.2"))
PROCESSING_STATUS_DELAY = float(get_config("RTDT_PROCESSING_STATUS_DELAY", "0.15"))
TRANSCRIPTION_LOOP_SLEEP = float(get_config("RTDT_TRANSCRIPTION_LOOP_SLEEP", "0.05"))
PRIORITIZE_SPEAKER_AUDIO = get_config("RTDT_PRIORITIZE_SPEAKER", "1") != "0"
ENABLE_AUDIO_NORMALIZATION = get_config("RTDT_ENABLE_AUDIO_NORMALIZATION", "1") != "0"
TARGET_AUDIO_RMS = int(get_config("RTDT_TARGET_AUDIO_RMS", "800"))
MAX_AUDIO_GAIN = float(get_config("RTDT_MAX_AUDIO_GAIN", "3.0"))
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
            source_batches = (
                [("Speaker", speaker_data), ("You", mic_data)]
                if PRIORITIZE_SPEAKER_AUDIO
                else [("You", mic_data), ("Speaker", speaker_data)]
            )

            try:
                for who_spoke, audio_data in source_batches:
                    if not audio_data:
                        continue

                    processing_sources.append(who_spoke)
                    latest_time = max(time for _, time in audio_data)
                    self._set_segment_transcribing(who_spoke, True, latest_time)
                    transcription = self._transcribe_source(who_spoke, audio_data)
                    if transcription:
                        pending_transcriptions.append(transcription)
                    else:
                        self._discard_empty_active_entry(who_spoke)

                if pending_transcriptions:
                    pending_transcriptions.sort(key=lambda x: x[2])
                    for who_spoke, text, time_spoken in pending_transcriptions:
                        self.update_transcript(who_spoke, text, time_spoken)
            finally:
                for who_spoke in processing_sources:
                    self._set_segment_transcribing(who_spoke, False)

            threading.Event().wait(TRANSCRIPTION_LOOP_SLEEP)

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
            prepared_sample = self._prepare_audio_sample(who_spoke, sample)
            source_info["process_data_func"](prepared_sample, path)
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

        source_info["last_sample"] = data
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
            entry = self._ensure_active_entry_locked(who_spoke, time_spoken)

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
            self._cancel_translation_timer_locked(who_spoke)
            if visual_changed:
                self._bump_revision()

    def get_entries(self):
        with self.lock:
            now = time.monotonic()
            entries = []
            for entry in self.transcript_data["You"] + self.transcript_data["Speaker"]:
                entry_copy = entry.copy()
                entry_copy["status"] = self._entry_status_locked(entry, now)
                entries.append(entry_copy)
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

    def _ensure_active_entry_locked(self, who_spoke, time_spoken):
        segment = self.segment_state[who_spoke]
        transcript = self.transcript_data[who_spoke]
        source_info = self.audio_sources[who_spoke]

        if (
            source_info.get("new_phrase")
            and source_info.get("last_spoken") is not None
            and not segment["is_finalized"]
            and segment["active_card_id"] is not None
        ):
            active_entry = self._find_entry(segment["active_card_id"])
            if active_entry and self._is_meaningful_text(active_entry.get("original")):
                segment["active_card_id"] = None
                segment["buffer"] = ""
                segment["is_finalized"] = True
                source_info["active_entry_id"] = None

        if segment["is_finalized"] or segment["active_card_id"] is None:
            entry = self._new_entry(who_spoke, "", time_spoken)
            transcript.append(entry)
            segment["active_card_id"] = entry["id"]
            segment["buffer"] = ""
            segment["is_finalized"] = False
            if len(transcript) > MAX_PHRASES:
                transcript.pop(0)
            return entry

        entry = self._find_entry(segment["active_card_id"])
        if entry is None:
            entry = self._new_entry(who_spoke, "", time_spoken)
            transcript.append(entry)
            segment["active_card_id"] = entry["id"]
            segment["buffer"] = ""
            segment["is_finalized"] = False
        return entry

    def _entry_status_locked(self, entry, now):
        translation_state = entry.get("translation_state")
        if translation_state == "translating":
            return "Menerjemahkan"
        if translation_state == "done":
            return "Selesai"

        segment = self.segment_state.get(entry["speaker"])
        if segment and segment.get("active_card_id") == entry["id"]:
            last_activity = segment.get("last_activity") or self.last_audio_activity.get(entry["speaker"])
            if segment.get("is_transcribing"):
                started_at = segment.get("transcribing_started_at") or now
                if now - started_at < PROCESSING_STATUS_DELAY:
                    return "Sedang berbicara"
                return "Sedang memproses teks"
            if last_activity and now - last_activity <= SPEAKING_STATUS_SECONDS:
                return "Sedang berbicara"
            return "Teks siap diterjemahkan"

        return "Teks siap diterjemahkan"

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
            "transcribing_started_at": 0.0,
        }

    def _set_segment_transcribing(self, who_spoke, value, time_spoken=None):
        with self.lock:
            segment = self.segment_state[who_spoke]
            segment["is_transcribing"] = value
            if value and time_spoken is not None:
                started_at = time.monotonic()
                self._ensure_active_entry_locked(who_spoke, time_spoken)
                segment["last_activity"] = started_at
                segment["transcribing_started_at"] = started_at
                self._cancel_translation_timer_locked(who_spoke)
                self._bump_revision()
                self._schedule_processing_status_refresh(who_spoke, started_at)
            elif not value:
                segment["transcribing_started_at"] = 0.0
                self._bump_revision()
                self._schedule_status_refresh(who_spoke, segment.get("last_activity"))

    def _schedule_processing_status_refresh(self, who_spoke, started_at):
        timer = threading.Timer(PROCESSING_STATUS_DELAY, self._refresh_processing_status_if_active, args=(who_spoke, started_at))
        timer.daemon = True
        timer.start()

    def _refresh_processing_status_if_active(self, who_spoke, started_at):
        with self.lock:
            segment = self.segment_state[who_spoke]
            if segment.get("is_transcribing") and segment.get("transcribing_started_at") == started_at:
                self._bump_revision()

    def _schedule_status_refresh(self, who_spoke, last_activity):
        if not last_activity:
            return

        timer = threading.Timer(SPEAKING_STATUS_SECONDS, self._refresh_status_if_idle, args=(who_spoke, last_activity))
        timer.daemon = True
        timer.start()

    def _refresh_status_if_idle(self, who_spoke, last_activity):
        with self.lock:
            segment = self.segment_state[who_spoke]
            if (
                segment.get("active_card_id") is not None
                and not segment.get("is_transcribing")
                and segment.get("last_activity") == last_activity
            ):
                self._bump_revision()

    def finish_active_segments(self):
        ready_translations = []
        with self.lock:
            for who_spoke in ("You", "Speaker"):
                ready_translation = self._finalize_segment_locked(who_spoke, force=True)
                if ready_translation:
                    ready_translations.append(ready_translation)

        for entry_id, original in ready_translations:
            self._queue_translation(entry_id, original)

        return len(ready_translations)

    def finish_entry(self, entry_id):
        ready_translation = None
        with self.lock:
            entry = self._find_entry(entry_id)
            if not entry:
                return 0

            original = self._normalize_text(entry["original"])
            if not self._is_meaningful_text(original):
                return 0

            if entry.get("translation_state") == "translating":
                return 0
            if entry.get("translation_state") == "done" and entry.get("translated_original") == original:
                return 0

            who_spoke = entry["speaker"]
            segment = self.segment_state[who_spoke]
            if segment.get("active_card_id") == entry_id:
                ready_translation = self._finalize_segment_locked(who_spoke, force=True)
            else:
                entry["translation"] = TRANSLATION_RUNNING_TEXT
                entry["translation_pending"] = True
                entry["translation_state"] = "translating"
                entry["translation_requested_for"] = original
                ready_translation = (entry_id, original)
                self._bump_revision()

        if ready_translation:
            self._queue_translation(*ready_translation)
            return 1
        return 0

    def _cancel_translation_timer_locked(self, who_spoke):
        segment = self.segment_state[who_spoke]
        timer = segment.get("translation_timer")
        if timer:
            timer.cancel()
        segment["translation_timer"] = None
        segment["timer_generation"] += 1

    def _discard_empty_active_entry(self, who_spoke):
        with self.lock:
            segment = self.segment_state[who_spoke]
            entry_id = segment.get("active_card_id")
            entry = self._find_entry(entry_id) if entry_id else None
            if not entry or self._normalize_text(entry.get("original")):
                return

            self._remove_entry_locked(entry_id)
            self._reset_segment_locked(who_spoke)
            self._bump_revision()

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

    def finalize_segment(self, who_spoke, generation=None, force=False):
        with self.lock:
            ready_translation = self._finalize_segment_locked(who_spoke, generation, force)

        if ready_translation:
            self._queue_translation(*ready_translation)

    def _finalize_segment_locked(self, who_spoke, generation=None, force=False):
        segment = self.segment_state[who_spoke]
        if generation is not None and generation != segment["timer_generation"]:
            return None
        if segment["is_finalized"] or segment["active_card_id"] is None:
            return None

        if not force:
            now = time.monotonic()
            elapsed = now - segment["last_activity"]
            if segment["is_transcribing"]:
                self._schedule_translation_timer_locked(who_spoke, delay=0.25)
                return None
            if elapsed < TRANSLATION_SILENCE_DELAY:
                self._schedule_translation_timer_locked(
                    who_spoke,
                    delay=max(0.1, TRANSLATION_SILENCE_DELAY - elapsed),
                )
                return None

        entry_id = segment["active_card_id"]
        full_text = self._normalize_text(segment["buffer"])
        entry = self._find_entry(entry_id)
        if not entry:
            self._reset_segment_locked(who_spoke)
            return None

        if not self._is_meaningful_text(full_text):
            self._remove_entry_locked(entry_id)
            self._reset_segment_locked(who_spoke)
            self._bump_revision()
            return None

        entry["original"] = full_text
        entry["translation"] = TRANSLATION_RUNNING_TEXT
        entry["translation_pending"] = True
        entry["translation_state"] = "translating"
        entry["translation_requested_for"] = full_text
        self._reset_segment_locked(who_spoke, keep_transcribing=True)
        self._bump_revision()
        return entry_id, full_text

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
                "transcribing_started_at": segment.get("transcribing_started_at") if is_transcribing else 0.0,
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
        min_rms = self._min_audio_rms(who_spoke)
        if rms < min_rms:
            if self.debug:
                print(f"[DEBUG] Skipping {who_spoke}: likely silence/noise (rms={rms}, min={min_rms}).")
            return True

        return False

    def _prepare_audio_sample(self, who_spoke, data):
        if not ENABLE_AUDIO_NORMALIZATION or not data:
            return data

        sample_width = self.audio_sources[who_spoke]["sample_width"]
        try:
            average = audioop.avg(data, sample_width)
            cleaned = audioop.bias(data, sample_width, -average) if average else data
            rms = audioop.rms(cleaned, sample_width)
            if rms <= 0:
                return cleaned

            gain = min(MAX_AUDIO_GAIN, TARGET_AUDIO_RMS / rms)
            if gain > 1.05:
                return audioop.mul(cleaned, sample_width, gain)
            return cleaned
        except audioop.error:
            return data

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
    def _min_audio_rms(who_spoke):
        if who_spoke == "Speaker":
            return MIN_SPEAKER_AUDIO_RMS
        return MIN_MIC_AUDIO_RMS

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
