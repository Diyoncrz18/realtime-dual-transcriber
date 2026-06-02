# Security Policy

## Supported Versions

Security fixes are handled on the `main` branch.

## Reporting a Vulnerability

Please do not publicly post API keys, transcripts, audio recordings, or exploit details in an issue.

If you find a security problem:

1. Open a minimal issue that says a security report is available, without secrets or exploit details.
2. Contact the repository owner through GitHub profile information.
3. Include reproduction steps, affected files, and impact when sharing details privately.

## Sensitive Data

This project can process microphone audio, speaker audio, transcripts, and API credentials. Contributors should avoid committing:

- `.env`
- `keys.py`
- API keys
- audio recordings
- transcript logs
- generated model files
- screenshots that reveal private conversations

The repository includes secret-scan automation, but contributors are still responsible for checking their changes before opening a pull request.
