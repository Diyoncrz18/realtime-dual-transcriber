import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import customtkinter as ctk

import AudioRecorder
import TranscriberModels
from AudioTranscriber import AudioTranscriber


def configure_textbox_tags(textbox):
    tags = {
        "you": {"foreground": "#7dd3fc", "font": ("Segoe UI Semibold", 15)},
        "speaker": {"foreground": "#fbbf24", "font": ("Segoe UI Semibold", 15)},
        "timestamp": {"foreground": "#94a3b8", "font": ("Segoe UI", 12)},
        "heading": {"foreground": "#cbd5e1", "font": ("Segoe UI Semibold", 13)},
        "original": {"foreground": "#f8fafc", "font": ("Segoe UI", 17)},
        "translation": {"foreground": "#bbf7d0", "font": ("Segoe UI", 16)},
        "pending": {"foreground": "#94a3b8", "font": ("Segoe UI Italic", 15)},
        "spacer": {"font": ("Segoe UI", 6)},
    }
    target = getattr(textbox, "_textbox", textbox)
    for tag, options in tags.items():
        try:
            target.tag_config(tag, **options)
        except Exception:
            pass


def insert_tagged(textbox, text, tag):
    try:
        textbox.insert("end", text, tag)
    except TypeError:
        textbox.insert("end", text)


def render_transcript(textbox, entries):
    textbox.configure(state="normal")
    textbox.delete("1.0", "end")

    for entry in entries:
        speaker_tag = "you" if entry["speaker"] == "You" else "speaker"
        speaker_label = "[YOU / MIC]" if entry["speaker"] == "You" else "[SPEAKER]"
        timestamp = entry["timestamp"].strftime("%H:%M:%S")
        translation = entry.get("translation") or entry["original"]
        translation_tag = "pending" if entry.get("translation_pending") else "translation"

        insert_tagged(textbox, speaker_label, speaker_tag)
        insert_tagged(textbox, f"  {timestamp}\n", "timestamp")
        insert_tagged(textbox, "Original:\n", "heading")
        insert_tagged(textbox, f"{entry['original']}\n\n", "original")
        insert_tagged(textbox, "Terjemahan:\n", "heading")
        insert_tagged(textbox, f"{translation}\n\n", translation_tag)
        insert_tagged(textbox, "\n", "spacer")

    textbox.see("end")
    textbox.configure(state="disabled")


def update_transcript_UI(transcriber, textbox, last_revision=None):
    if last_revision is None:
        last_revision = {"value": -1}

    revision = transcriber.get_revision()
    if revision != last_revision["value"]:
        render_transcript(textbox, transcriber.get_entries())
        last_revision["value"] = revision

    textbox.after(250, update_transcript_UI, transcriber, textbox, last_revision)


def clear_context(transcriber, speaker_queue, mic_queue):
    transcriber.clear_transcript_data()

    with speaker_queue.mutex:
        speaker_queue.queue.clear()
    with mic_queue.mutex:
        mic_queue.queue.clear()


def create_ui_components(root, transcriber, speaker_queue, mic_queue):
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")
    root.title("realtime-dual-transcriber")
    root.geometry("1040x680")
    root.minsize(760, 460)

    root.grid_columnconfigure(0, weight=1)
    root.grid_rowconfigure(0, weight=1)

    main_frame = ctk.CTkFrame(root, fg_color="#101820", corner_radius=8)
    main_frame.grid(row=0, column=0, sticky="nsew", padx=16, pady=16)

    main_frame.grid_columnconfigure(0, weight=1)
    main_frame.grid_rowconfigure(0, weight=0)
    main_frame.grid_rowconfigure(1, weight=1)
    main_frame.grid_rowconfigure(2, weight=0)

    header = ctk.CTkLabel(
        main_frame,
        text="realtime-dual-transcriber",
        font=("Segoe UI Semibold", 18),
        text_color="#f8fafc",
        anchor="w",
    )
    header.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))

    transcript_textbox = ctk.CTkTextbox(
        main_frame,
        font=("Segoe UI", 17),
        text_color="#f8fafc",
        fg_color="#0b1117",
        border_width=1,
        border_color="#243447",
        wrap="word",
        spacing1=4,
        spacing2=2,
        spacing3=8,
    )
    transcript_textbox.grid(row=1, column=0, sticky="nsew", padx=14, pady=10)
    configure_textbox_tags(transcript_textbox)
    transcript_textbox.configure(state="disabled")

    clear_button = ctk.CTkButton(
        main_frame,
        text="Clear Transcript",
        command=lambda: clear_context(transcriber, speaker_queue, mic_queue),
        height=36,
        corner_radius=6,
    )
    clear_button.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 14))

    return transcript_textbox


def find_ffmpeg():
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        return ffmpeg_path

    winget_packages = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    matches = sorted(winget_packages.glob("Gyan.FFmpeg_*/*/bin/ffmpeg.exe"))
    if matches:
        return str(matches[-1])

    return None


def main():
    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        print("ERROR: The ffmpeg library is not installed. Please install ffmpeg and try again.")
        return

    try:
        subprocess.run([ffmpeg_path, "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        print("ERROR: The ffmpeg library is not installed. Please install ffmpeg and try again.")
        return

    root = ctk.CTk()
    speaker_queue = queue.Queue()
    mic_queue = queue.Queue()

    user_audio_recorder = AudioRecorder.DefaultMicRecorder()
    user_audio_recorder.record_into_queue(mic_queue)

    time.sleep(1)

    speaker_audio_recorder = AudioRecorder.DefaultSpeakerRecorder()
    speaker_audio_recorder.record_into_queue(speaker_queue)

    model = TranscriberModels.get_model("--api" in sys.argv)

    transcriber = AudioTranscriber(user_audio_recorder.source, speaker_audio_recorder.source, model)
    transcribe = threading.Thread(target=transcriber.transcribe_audio_queue, args=(speaker_queue, mic_queue))
    transcribe.daemon = True
    transcribe.start()

    transcript_textbox = create_ui_components(root, transcriber, speaker_queue, mic_queue)

    print("READY")

    update_transcript_UI(transcriber, transcript_textbox)

    root.mainloop()


if __name__ == "__main__":
    main()
