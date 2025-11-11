# Streaming Speech-to-Order (Flutter + FastAPI)

This repository implements a real-time speech-to-order workflow:
- Frontend: Flutter tab StreamingTab ([lib/tabs/tab8.dart](cci:7://file:///c:/Users/itintern2/Desktop/one/one/lib/tabs/tab8.dart:0:0-0:0)) streams audio and renders live transcription with extracted product code (`pcode`) and quantity (`qty`).
- Backend: FastAPI ([serve.py](cci:7://file:///c:/Users/itintern2/Desktop/one/one/serve.py:0:0-0:0)) streams audio to Google Cloud Speech-to-Text, normalizes text (double/triple, number words, hundred/thousand), extracts `pcode` and `qty`, maintains per-connection session state for quantity follow-ups, and streams results back over WebSocket.

The app entry point is `lib/main.dart`. The relevant tab is the last tab labeled “Stream”.

---

## Features

- Product code + quantity extraction, validated against `productlist.csv`.
- Session-aware quantity follow-up:
  - If a `pcode` is finalized without `qty`, the server enters waiting state.
  - The next utterance is interpreted as a quantity (normalized) and paired with the last `pcode`.
- Doubles/Triples:
  - “double 2”, “double2”, “double, two” → 22
  - “triple one”, “triple1” → 111
  - Applies to the immediate next number (digit or word), even across spaces/punctuation.
- Number words and mishearings:
  - Words like one/two/three… converted to digits.
  - Variants handled (e.g., won → one, to/too → two, tree → three, fife → five, etc.).
- Large units:
  - “three hundred” → 300
  - “three hundred forty five” → 345
  - “ten thousand” → 10000
  - “two thousand sixteen” → 2016
  - Works even when separated by punctuation/spaces and optional “and”.
- Stop Word Mode:
  - Client can enable mode to finalize on saying “stop”, “done”, or “next”.
  - Saying cancel words like “cancel/again/retry/repeat/redo/restart” clears the buffer and waiting state.
- Partial/final message stream:
  - Partial live text, Final results include pcode/qty/confidence.
- Numeric product code detection:
  - Pure numbers, if matching a product code, are treated as product codes (not quantities).

---

## Architecture

- WebSocket flow:
  - Flutter ([StreamingTab](cci:2://file:///c:/Users/itintern2/Desktop/one/one/lib/tabs/tab8.dart:8:0-13:1)) records mono PCM16 at 16 kHz and sends fixed-size chunks to `ws://<SERVER_IP>:5000/stream_google`.
  - FastAPI (`/stream_google`) streams to Google STT and emits:
    - `{"type":"partial","text":"..."}` for interim results.
    - `{"type":"final","text":"...", "data":{"pcode":"...","qty":"...","confidence":0.0-1.0}}` for finalized results.
    - `{"type":"validation_error","error_code":"...","message":"..."}` for immediate feedback (e.g., invalid pcode).
    - `{"type":"cancelled","message":"...","cancel_word":"..."}` when cancel words are spoken.
    - `{"type":"config_ack","use_stop_word":true|false}` after client config.
  - Client updates partial text and inserts an entry for each final result. If a `pcode` is received without `qty`, the UI highlights that entry and waits for the next qty-only final.

- Session state (server):
  - Each WebSocket connection maintains a simple session:
    - waiting_for_qty + last_pcode.
  - Waiting state is set when a `pcode` is finalized without `qty`.
  - Waiting state is cleared on:
    - qty follow-up completion,
    - cancel words,
    - validation errors,
    - or next successful `pcode` with `qty`.

- Normalization pipeline (server):
  - Clean text (preserve spaces initially).
  - Doubles/Triples conversion (handles immediate-next number across separators and compact forms).
  - Number words → digits (with common mishearing fixes).
  - Large unit phrases (hundred/thousand) → numeric expansion.
  - Remove spaces.
  - This same pipeline is applied in waiting-for-qty mode before extracting digits, so phrases like “won”, “triple one”, “three hundred” become 1, 111, 300 respectively.

---

## Backend setup (FastAPI)

All paths below are relative to: `c:\Users\itintern2\Desktop\one\one`.

1) Create and activate a virtual environment (recommended)
```powershell
python -m venv .venv
.\.venv\Scripts\activate