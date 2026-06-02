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


UI_REFRESH_MS = int(AudioRecorder.get_config("RTDT_UI_REFRESH_MS", "150"))


def set_textbox_text(textbox, text):
    text = text or ""
    current = textbox.get("1.0", "end-1c")
    if current == text:
        return

    textbox.configure(state="normal")
    textbox.delete("1.0", "end")
    textbox.insert("1.0", text)
    textbox.configure(state="disabled")


def render_transcript_view(widgets, entries, current_index):
    total = len(entries)
    if total == 0:
        widgets["speaker_label"].configure(text="BELUM ADA TRANSCRIPT", text_color="#94a3b8")
        widgets["time_label"].configure(text="--:--:--")
        widgets["status_label"].configure(text="Menunggu suara masuk", text_color="#94a3b8")
        widgets["counter_label"].configure(text="0 / 0")
        set_textbox_text(widgets["original_textbox"], "Mulai bicara untuk membuat transcript pertama.")
        widgets["translation_textbox"].configure(text_color="#94a3b8")
        set_textbox_text(widgets["translation_textbox"], "")
        widgets["previous_button"].configure(state="disabled")
        widgets["next_button"].configure(state="disabled")
        widgets["finish_button"].configure(state="disabled")
        return

    current_index = max(0, min(current_index, total - 1))
    active_transcript = entries[current_index]
    is_you = active_transcript["speaker"] == "You"
    speaker_label = "YOU / MIC" if is_you else "SPEAKER"
    speaker_color = "#38bdf8" if is_you else "#a78bfa"
    status = active_transcript.get("status") or "Teks siap diterjemahkan"
    status_colors = {
        "Sedang berbicara": "#fbbf24",
        "Sedang memproses teks": "#fbbf24",
        "Teks siap diterjemahkan": "#93c5fd",
        "Menerjemahkan": "#fbbf24",
        "Selesai": "#86efac",
    }
    if status == "Sedang berbicara":
        status_text = f"[{speaker_label}] sedang berbicara..."
    elif status == "Sedang memproses teks":
        status_text = "Sedang memproses teks..."
    else:
        status_text = status
    translation = active_transcript.get("translation") or ""
    original = active_transcript.get("original", "")

    widgets["speaker_label"].configure(text=speaker_label, text_color=speaker_color)
    widgets["time_label"].configure(text=active_transcript["timestamp"].strftime("%H:%M:%S"))
    widgets["status_label"].configure(text=status_text, text_color=status_colors.get(status, "#94a3b8"))
    widgets["counter_label"].configure(text=f"{current_index + 1} / {total}")
    set_textbox_text(widgets["original_textbox"], original or "Mendengarkan audio...")
    widgets["translation_textbox"].configure(
        text_color="#94a3b8" if active_transcript.get("translation_pending") else "#bbf7d0"
    )
    set_textbox_text(widgets["translation_textbox"], translation)

    widgets["previous_button"].configure(state="normal" if current_index > 0 else "disabled")
    widgets["next_button"].configure(state="normal" if current_index < total - 1 else "disabled")
    can_finish = bool(original.strip()) and status not in {"Sedang memproses teks", "Menerjemahkan", "Selesai"}
    widgets["finish_button"].configure(state="normal" if can_finish else "disabled")


def update_transcript_UI(transcriber, widgets, state):
    revision = transcriber.get_revision()
    if revision != state["last_revision"]:
        entries = transcriber.get_entries()
        if not entries:
            state["current_index"] = 0
        elif state["current_index"] >= len(entries):
            state["current_index"] = len(entries) - 1
        render_transcript_view(widgets, entries, state["current_index"])
        state["last_revision"] = revision

    widgets["container"].after(UI_REFRESH_MS, update_transcript_UI, transcriber, widgets, state)


def handle_previous(transcriber, widgets, state):
    if state["current_index"] <= 0:
        return

    state["current_index"] -= 1
    render_transcript_view(widgets, transcriber.get_entries(), state["current_index"])


def handle_next(transcriber, widgets, state):
    entries = transcriber.get_entries()
    if state["current_index"] >= len(entries) - 1:
        return

    state["current_index"] += 1
    render_transcript_view(widgets, entries, state["current_index"])


def handle_finish(transcriber, widgets, state):
    entries = transcriber.get_entries()
    if not entries:
        render_transcript_view(widgets, entries, state["current_index"])
        return

    current_index = max(0, min(state["current_index"], len(entries) - 1))
    state["current_index"] = current_index
    active_transcript = entries[current_index]
    transcriber.finish_entry(active_transcript["id"])
    render_transcript_view(widgets, transcriber.get_entries(), state["current_index"])


def clear_context(transcriber, speaker_queue, mic_queue, widgets=None, state=None):
    transcriber.clear_transcript_data()

    with speaker_queue.mutex:
        speaker_queue.queue.clear()
    with mic_queue.mutex:
        mic_queue.queue.clear()

    if state is not None:
        state["current_index"] = 0
        state["last_revision"] = -1
    if widgets is not None:
        render_transcript_view(widgets, [], 0)


def create_ui_components(root, transcriber, speaker_queue, mic_queue):
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")
    root.title("realtime-dual-transcriber")
    root.geometry("1040x680")
    root.minsize(760, 460)

    root.grid_columnconfigure(0, weight=1)
    root.grid_rowconfigure(0, weight=1)

    state = {"current_index": 0, "last_revision": -1}

    main_frame = ctk.CTkFrame(root, fg_color="#0f172a", corner_radius=0)
    main_frame.grid(row=0, column=0, sticky="nsew")
    main_frame.grid_columnconfigure(0, weight=1)
    main_frame.grid_rowconfigure(0, weight=0)
    main_frame.grid_rowconfigure(1, weight=1)
    main_frame.grid_rowconfigure(2, weight=0)

    header = ctk.CTkLabel(
        main_frame,
        text="realtime-dual-transcriber",
        font=("Segoe UI Semibold", 18),
        text_color="#f8fafc",
        anchor="center",
    )
    header.grid(row=0, column=0, sticky="ew", padx=18, pady=(18, 8))

    card = ctk.CTkFrame(main_frame, fg_color="#111827", border_width=1, border_color="#263244", corner_radius=10)
    card.grid(row=1, column=0, sticky="nsew", padx=18, pady=10)
    card.grid_columnconfigure(0, weight=1)
    card.grid_rowconfigure(0, weight=0)
    card.grid_rowconfigure(1, weight=0)
    card.grid_rowconfigure(2, weight=0)
    card.grid_rowconfigure(3, weight=0)
    card.grid_rowconfigure(4, weight=1)
    card.grid_rowconfigure(5, weight=0)
    card.grid_rowconfigure(6, weight=1)

    meta_frame = ctk.CTkFrame(card, fg_color="transparent")
    meta_frame.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 4))
    meta_frame.grid_columnconfigure(0, weight=1)
    meta_frame.grid_columnconfigure(1, weight=0)

    speaker_label = ctk.CTkLabel(
        meta_frame,
        text="BELUM ADA TRANSCRIPT",
        font=("Segoe UI Semibold", 20),
        text_color="#94a3b8",
        anchor="w",
    )
    speaker_label.grid(row=0, column=0, sticky="ew")

    counter_label = ctk.CTkLabel(
        meta_frame,
        text="0 / 0",
        font=("Segoe UI", 13),
        text_color="#94a3b8",
    )
    counter_label.grid(row=0, column=1, sticky="e", padx=(12, 0))

    time_label = ctk.CTkLabel(
        card,
        text="--:--:--",
        font=("Segoe UI", 13),
        text_color="#94a3b8",
        anchor="w",
    )
    time_label.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 14))

    status_label = ctk.CTkLabel(
        card,
        text="Menunggu suara masuk",
        font=("Segoe UI Semibold", 14),
        text_color="#94a3b8",
        anchor="w",
    )
    status_label.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 16))

    original_label = ctk.CTkLabel(
        card,
        text="Original",
        font=("Segoe UI Semibold", 14),
        text_color="#cbd5e1",
        anchor="w",
    )
    original_label.grid(row=3, column=0, sticky="ew", padx=20, pady=(0, 6))

    original_textbox = ctk.CTkTextbox(
        card,
        font=("Segoe UI", 17),
        text_color="#f8fafc",
        fg_color="#0b1120",
        border_width=1,
        border_color="#263244",
        wrap="word",
    )
    original_textbox.grid(row=4, column=0, sticky="nsew", padx=20, pady=(0, 14))

    translation_label = ctk.CTkLabel(
        card,
        text="Terjemahan",
        font=("Segoe UI Semibold", 14),
        text_color="#cbd5e1",
        anchor="w",
    )
    translation_label.grid(row=5, column=0, sticky="ew", padx=20, pady=(0, 6))

    translation_textbox = ctk.CTkTextbox(
        card,
        font=("Segoe UI", 16),
        text_color="#bbf7d0",
        fg_color="#07140f",
        border_width=1,
        border_color="#1f3d32",
        wrap="word",
    )
    translation_textbox.grid(row=6, column=0, sticky="nsew", padx=20, pady=(0, 20))
    original_textbox.configure(state="disabled")
    translation_textbox.configure(state="disabled")

    footer_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
    footer_frame.grid(row=2, column=0, sticky="ew", padx=18, pady=(4, 18))
    footer_frame.grid_columnconfigure(0, weight=1)
    footer_frame.grid_columnconfigure(1, weight=1)
    footer_frame.grid_columnconfigure(2, weight=1)
    footer_frame.grid_columnconfigure(3, weight=1)

    widgets = {
        "container": main_frame,
        "speaker_label": speaker_label,
        "time_label": time_label,
        "status_label": status_label,
        "counter_label": counter_label,
        "original_textbox": original_textbox,
        "translation_textbox": translation_textbox,
    }

    previous_button = ctk.CTkButton(
        footer_frame,
        text="Previous",
        command=lambda: handle_previous(transcriber, widgets, state),
        height=38,
        corner_radius=6,
    )
    previous_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))

    next_button = ctk.CTkButton(
        footer_frame,
        text="Next",
        command=lambda: handle_next(transcriber, widgets, state),
        height=38,
        corner_radius=6,
    )
    next_button.grid(row=0, column=1, sticky="ew", padx=6)

    finish_button = ctk.CTkButton(
        footer_frame,
        text="Selesai",
        command=lambda: handle_finish(transcriber, widgets, state),
        height=38,
        corner_radius=6,
        fg_color="#15803d",
        hover_color="#166534",
    )
    finish_button.grid(row=0, column=2, sticky="ew", padx=6)

    clear_button = ctk.CTkButton(
        footer_frame,
        text="Clear Transcript",
        command=lambda: clear_context(transcriber, speaker_queue, mic_queue, widgets, state),
        height=38,
        corner_radius=6,
    )
    clear_button.grid(row=0, column=3, sticky="ew", padx=(6, 0))

    widgets.update(
        {
            "previous_button": previous_button,
            "next_button": next_button,
            "finish_button": finish_button,
            "clear_button": clear_button,
        }
    )

    render_transcript_view(widgets, [], 0)

    return widgets, state


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

    transcript_widgets, transcript_state = create_ui_components(root, transcriber, speaker_queue, mic_queue)

    print("READY")

    update_transcript_UI(transcriber, transcript_widgets, transcript_state)

    root.mainloop()


if __name__ == "__main__":
    main()
