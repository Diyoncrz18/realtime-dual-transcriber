# Contributing

Thanks for wanting to improve realtime-dual-transcriber. This project is open to forks, pull requests, bug reports, experiments, and documentation improvements.

## Ways to Contribute

- Fix bugs in microphone, speaker, transcription, translation, or UI behavior.
- Improve realtime performance and transcription accuracy.
- Add tests for transcript state, API configuration, and UI-safe behavior.
- Improve setup documentation for Windows users.
- Suggest better defaults for Groq, OpenAI-compatible providers, or local Whisper mode.

## Before You Start

1. Fork the repository.
2. Create a feature branch from `main`.
3. Keep changes focused and easy to review.
4. Never commit `.env`, `keys.py`, API keys, logs, recordings, model files, or personal data.

```powershell
git clone https://github.com/YOUR_USERNAME/realtime-dual-transcriber.git
cd realtime-dual-transcriber
git checkout -b feature/your-change
```

## Local Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Fill `.env` with your own provider key if you want to test API mode. Keep it private.

## Running

Local mode:

```powershell
.\.venv\Scripts\python.exe main.py
```

API mode:

```powershell
.\.venv\Scripts\python.exe main.py --api
```

## Tests

Run these before opening a pull request:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m py_compile main.py AudioRecorder.py AudioTranscriber.py TranscriberModels.py
```

## Pull Request Checklist

- The change is scoped to one clear problem or feature.
- Tests were added or updated when behavior changed.
- Existing microphone and speaker transcription still work.
- Translation remains manual through the `Selesai` button unless the PR explicitly discusses changing that flow.
- No secrets, recordings, generated model files, or local machine paths were committed.
- The README or examples were updated if configuration changed.

## Good First Issues

Good first contributions include:

- Better setup notes for FFmpeg and Windows audio devices.
- Small UI polish that does not rewrite the app.
- More unit tests for transcript merging and translation state.
- Safer defaults for noisy rooms or low-volume speaker audio.

## Review Style

Reviews focus on correctness, realtime behavior, security, privacy, and keeping the app easy to run. Clear screenshots, terminal logs without secrets, and short reproduction steps make a PR much easier to merge.
