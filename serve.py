from fastapi import FastAPI, UploadFile, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
import uvicorn
import tempfile
import os
import re
import json
import asyncio
import requests
from datetime import datetime
from typing import List, Tuple, Optional, Dict
from difflib import SequenceMatcher
from collections import deque
import csv
import wave
import google.cloud.speech as gspeech
import base64
import threading
from queue import Queue
import contextlib
from concurrent.futures import ThreadPoolExecutor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

if os.environ.get("GOOGLE_CREDENTIALS_JSON"):
    print("🔐 Using GOOGLE_CREDENTIALS_JSON from Render")
    with open("gcloud_key.json", "w") as f:
        f.write(os.environ["GOOGLE_CREDENTIALS_JSON"])
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "gcloud_key.json"
else:
    print("🖥️ Using local service/keys/google_credentials.json")
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.join(
        BASE_DIR, "service", "keys", "google_credentials.json"
    )


app = FastAPI()


# ----------------------------------------------------------------------------
# Simple per-connection session state to handle qty follow-up pairing
# ----------------------------------------------------------------------------
class SessionState:
    def __init__(self):
        self.waiting_for_qty = False
        self.last_pcode: str = ""
        self.last_options: List[str] = []  # 🔥 NEW: Store options when waiting
        self.first_letter_lock: str = ""

    def wait_for_qty(self, pcode: str, options: List[str] = None):
        """Set waiting state with optional ambiguous options"""
        self.waiting_for_qty = True
        self.last_pcode = pcode
        self.last_options = options if options else []

    def clear(self):
        self.waiting_for_qty = False
        self.last_pcode = ""
        self.last_options = []

    def reset_letter_lock(self):
        """Reset first letter lock for new transcription"""
        self.first_letter_lock = ""

    def is_waiting(self) -> bool:
        return self.waiting_for_qty and bool(self.last_pcode)

    def has_options(self) -> bool:
        return len(self.last_options) > 0


# ============================================================================
# WEBSOCKET ENDPOINT: /stream_google
# ============================================================================
@app.websocket("/stream_google")
async def stream_google(websocket: WebSocket):
    await websocket.accept()
    print("🔌 WebSocket connection established")

    # Session configuration
    use_stop_word = False
    buffered_text = ""
    STOP_WORDS = ["stop", "done", "next"]
    CANCEL_WORDS = ["cancel", "again", "retry", "repeat", "redo", "restart"]

    client = gspeech.SpeechClient()

    # 🔥 ENHANCED RECOGNITION CONFIG WITH SPEECH CONTEXTS
    recognition_config = gspeech.RecognitionConfig(
        encoding=gspeech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=16000,
        language_code="en-US",
        enable_automatic_punctuation=True,
        profanity_filter=False,
        use_enhanced=False,
        model="command_and_search",
        adaptation=gspeech.SpeechAdaptation(
            phrase_sets=[
                gspeech.PhraseSet(
                    phrases=[
                        gspeech.PhraseSet.Phrase(value="double", boost=25),
                        gspeech.PhraseSet.Phrase(value="triple", boost=25),
                        *[
                            gspeech.PhraseSet.Phrase(value=f"double {i}", boost=25)
                            for i in range(10)
                        ],
                        *[
                            gspeech.PhraseSet.Phrase(value=f"triple {i}", boost=25)
                            for i in range(10)
                        ],
                    ]
                )
            ]
        ),
        speech_contexts=[
            gspeech.SpeechContext(
                phrases=[
                    "A", "A0", "A03", "A zero", "A oh", "A O", "E", "E0", "E0", "E zero", "E oh",
                    "E O",
                    "13ED", "13 E D",
                    "double", "triple",
                    "double zero", "double one", "double two", "double three",
                    "double four", "double five", "double six", "double seven",
                    "double eight", "double nine",
                    "triple zero", "triple one", "triple two", "triple three",
                    "triple four", "triple five", "triple six", "triple seven",
                    "triple eight", "triple nine",
                    *list("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
                    "piece", "quantity", "product code", "item code", "SKU",
                ],
                boost=25
            )
        ],
    )

    streaming_config = gspeech.StreamingRecognitionConfig(
        config=recognition_config,
        interim_results=True,
        single_utterance=False,
    )

    q: Queue = Queue()
    stop_event = threading.Event()

    def request_generator_sync():
        while not stop_event.is_set():
            data = q.get()
            if data is None:
                break
            yield gspeech.StreamingRecognizeRequest(audio_content=data)

    loop = asyncio.get_running_loop()

    def safe_thread_send(data):
        """Safely send data through websocket from thread"""
        try:
            if websocket.client_state.name == "CONNECTED":
                asyncio.run_coroutine_threadsafe(websocket.send_json(data), loop)
        except Exception as e:
            print(f"⚠️ Failed to send message: {e}")

    def run_recognition():
        nonlocal buffered_text
        session_state = SessionState()

        try:
            responses = client.streaming_recognize(
                streaming_config, requests=request_generator_sync()
            )
            for response in responses:
                if not response.results:
                    continue
                result = response.results[0]
                if not result.alternatives:
                    continue
                transcript = result.alternatives[0].transcript.strip()

                # Skip empty or invalid transcripts
                if not transcript or transcript in ['.', ',', '!', '?', '...', '..', '…']:
                    print(f"⏭️ Skipping empty/invalid transcript: '{transcript}'")
                    continue

                # Remove trailing punctuation
                transcript_clean = transcript.rstrip('.!?,;:')
                if not transcript_clean:
                    print(f"⏭️ Skipping transcript with only punctuation: '{transcript}'")
                    continue

                # 🔒 FIRST LETTER LOCK - PARTIAL HANDLER
                if not result.is_final:
                    transcript_minimal = transcript_clean.lower().strip()
                    if (transcript_minimal and
                            len(transcript_minimal) == 1 and
                            transcript_minimal[0].isalpha() and
                            not session_state.first_letter_lock):
                        common_words_to_convert = ['and', 'an', 'high','hi','is', 'he', 'ay', 'bee', 'see', 'dee',
                                                   'ef', 'gee', 'jay', 'kay', 'ell', 'em', 'en', 'oh', 'pea',
                                                   'queue', 'ar', 'ess', 'tea', 'you', 'vee', 'ex', 'why', 'zee']
                        if transcript_minimal not in common_words_to_convert:
                            session_state.first_letter_lock = transcript_minimal[0].upper()
                            print(f"🔒 FIRST LETTER LOCKED: {session_state.first_letter_lock}")

                if use_stop_word:
                    # ========== STOP WORD MODE ==========
                    transcript_lower = transcript_clean.lower()

                    # Check for CANCEL words
                    cancel_word_found = None
                    for cancel_word in CANCEL_WORDS:
                        if cancel_word in transcript_lower:
                            cancel_word_found = cancel_word
                            break

                    if cancel_word_found:
                        if result.is_final:
                            print(f"🔄 CANCEL WORD DETECTED ('{cancel_word_found}'): Discarding buffer and resetting")
                            buffered_text = ""
                            safe_thread_send({
                                "type": "cancelled",
                                "text": "",
                                "message": f"Cancelled. Listening again...",
                                "cancel_word": cancel_word_found
                            })
                            session_state.clear()
                            session_state.reset_letter_lock()
                        continue

                    # Check for STOP words
                    stop_word_found = None
                    for stop_word in STOP_WORDS:
                        if stop_word in transcript_lower:
                            stop_word_found = stop_word
                            break

                    if stop_word_found:
                        if not result.is_final:
                            continue

                        # Remove stop word from transcript
                        cleaned_transcript = re.sub(
                            r'\b' + stop_word_found + r'\b',
                            '',
                            transcript_clean,
                            flags=re.IGNORECASE
                        ).strip()

                        if cleaned_transcript and len(cleaned_transcript) > 1:
                            if buffered_text:
                                buffered_text += " " + cleaned_transcript
                            else:
                                buffered_text = cleaned_transcript

                        # Apply first letter lock before finalizing
                        if buffered_text:
                            buffered_text, possible_codes = apply_first_letter_lock(buffered_text, session_state)

                        # Finalize the buffered text
                        if buffered_text and len(buffered_text.strip()) >= 3:
                            print(f"🛑 STOP WORD DETECTED ('{stop_word_found}'): Finalizing buffer: {buffered_text}")

                            # 🔥 CRITICAL FIX: Process with session state
                            processed = TranscriptionPipeline.process(
                                buffered_text,
                                raw_original_text=buffered_text,
                                session_state=session_state,
                                possible_codes=possible_codes  # 🔥 NEW: Pass possible codes
                            )

                            # 🔥 CRITICAL FIX: Handle response properly
                            handle_processed_result(processed, session_state, safe_thread_send, buffered_text)
                            buffered_text = ""
                    else:
                        # No stop/cancel word: accumulate in buffer
                        if result.is_final:
                            if buffered_text:
                                buffered_text += " " + transcript_clean
                            else:
                                buffered_text = transcript_clean
                            print(f"📝 Accumulated (final chunk): {buffered_text}")
                            safe_thread_send({
                                "type": "partial",
                                "text": buffered_text
                            })
                        else:
                            buffered_text = transcript_clean
                            print(f"💬 Partial (current): {buffered_text}")
                            safe_thread_send({
                                "type": "partial",
                                "text": buffered_text
                            })
                else:
                    # ========== NORMAL MODE ==========
                    transcript_lower = transcript_clean.lower()
                    cancel_word_found = None
                    for cancel_word in CANCEL_WORDS:
                        if cancel_word in transcript_lower:
                            cancel_word_found = cancel_word
                            break

                    if cancel_word_found:
                        print(f"🔄 CANCEL WORD DETECTED ('{cancel_word_found}'): Ignoring this transcription")
                        safe_thread_send({
                            "type": "cancelled",
                            "message": f"Transcription cancelled. Say the product code again.",
                            "cancel_word": cancel_word_found
                        })
                        session_state.clear()
                        session_state.reset_letter_lock()
                        continue

                    # Send partial results
                    if not result.is_final:
                        print(f"💬 PARTIAL: {transcript_clean}")
                        safe_thread_send({
                            "type": "partial",
                            "text": transcript_clean
                        })
                        continue

                    # Apply first letter lock to final transcript
                    transcript_clean, possible_codes = apply_first_letter_lock(transcript_clean, session_state)

                    # Process final result
                    if result.is_final:
                        print(f"🗣 FINAL: {transcript_clean}")

                        # 🔥 CRITICAL FIX: Process with possible codes
                        processed = TranscriptionPipeline.process(
                            transcript_clean,
                            raw_original_text=transcript_clean,
                            session_state=session_state,
                            possible_codes=possible_codes  # 🔥 NEW: Pass possible codes
                        )

                        # 🔥 CRITICAL FIX: Handle response properly
                        handle_processed_result(processed, session_state, safe_thread_send, transcript_clean)

        except Exception as e:
            print(f"❌ Error in process_responses: {e}")
            safe_thread_send({
                "type": "error",
                "message": str(e)
            })

    executor = ThreadPoolExecutor(max_workers=1)
    _future = loop.run_in_executor(executor, run_recognition)

    print("🧠 Google STT streaming started")

    try:
        while True:
            message = await websocket.receive()
            data_bytes = message.get("bytes")
            text = message.get("text")

            if data_bytes is not None:
                q.put(data_bytes)
                print(f"📥 Received {len(data_bytes)} bytes of audio")
            elif text is not None:
                try:
                    payload = json.loads(text)
                except Exception:
                    payload = {}

                if isinstance(payload, dict):
                    if payload.get("type") == "config":
                        use_stop_word = payload.get("use_stop_word", False)
                        print(f"⚙️ Configuration updated: use_stop_word={use_stop_word}")
                        await websocket.send_json({
                            "type": "config_ack",
                            "use_stop_word": use_stop_word
                        })
                    elif payload.get("type") == "stop":
                        print("🛑 Stop received")
                        stop_event.set()
                        q.put(None)
                        break
            else:
                pass

    except WebSocketDisconnect:
        print("🔌 WebSocket disconnected")
    except Exception as e:
        print(f"WebSocket error: {e}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except:
            pass
    finally:
        stop_event.set()
        q.put(None)
        print("🧹 Shutting down executor...")
        try:
            executor.shutdown(wait=True, cancel_futures=True)
        except Exception as e:
            print(f"⚠️ Executor shutdown warning: {e}")
        print("🧠 Google STT streaming ended")
        try:
            await websocket.close()
        except Exception as e:
            print(f"⚠️ WebSocket close warning: {e}")


def handle_processed_result(processed: Dict, session_state: SessionState, safe_thread_send, transcript: str):
    """
    🔥 NEW FUNCTION: Centralized handler for all processing results
    Implements the 5 rules for ambiguous code handling
    """
    pcode = processed.get("pcode", "")
    qty = processed.get("qty", "")
    confidence = processed.get("confidence", 0.0)
    is_qty_followup = processed.get("is_qty_followup", False)
    possible_codes = processed.get("possible_codes", [])  # 🔥 NEW

    # RULE: Handle quantity follow-up (existing logic)
    if is_qty_followup:
        print(f"🔢 QUANTITY FOLLOW-UP: qty='{qty}'")
        safe_thread_send({
            "type": "final",
            "text": transcript,
            "data": {
                "pcode": "",
                "qty": qty,
                "confidence": 1.0,
            }
        })
        session_state.clear()
        session_state.reset_letter_lock()
        return

    # 🔥 RULE 1 & 2: AMBIGUOUS PCODE - ALWAYS CREATE ENTRY IMMEDIATELY
    if possible_codes and len(possible_codes) > 1:
        print(f"📋 MULTIPLE OPTIONS: {' or '.join(possible_codes)} (qty: {qty if qty else 'N/A'})")

        safe_thread_send({
            "type": "multiple_options",
            "text": transcript,
            "message": "Multiple product codes found. Please select:",
            "options": possible_codes,
            "qty": qty,
            "confidence": confidence
        })

        # 🔥 RULE 2: If no qty, WAIT FOR QTY (link next quantity to this entry)
        if not qty:
            session_state.wait_for_qty(possible_codes[0], options=possible_codes)
            print(f"⏳ WAITING FOR QTY (ambiguous): {possible_codes}")
        else:
            # 🔥 RULE 3: If qty present, finalize immediately (no waiting)
            session_state.clear()
            print(f"✅ AMBIGUOUS + QTY: Complete entry created")

        session_state.reset_letter_lock()
        return

    # Validate pcode if present (single match case)
    if pcode:
        # Length validation
        if len(pcode) < 5:
            print(f"❌ VALIDATION FAIL: Product code too short ('{pcode}')")
            safe_thread_send({
                "type": "validation_error",
                "error_code": "INVALID_PCODE_LENGTH",
                "message": f"Invalid product code '{pcode}' (minimum 5 characters required)",
                "pcode": pcode,
                "qty": qty
            })
            session_state.reset_letter_lock()
            return

        # Product existence validation
        if not product_manager.exists(pcode):
            print(f"❌ VALIDATION FAIL: Product '{pcode}' not found")
            safe_thread_send({
                "type": "validation_error",
                "error_code": "NO_MATCH",
                "message": f"Product code '{pcode}' not found. Please try again.",
                "pcode": pcode,
                "qty": qty
            })
            session_state.reset_letter_lock()
            return

        # Success
        print(f"✅ VALIDATION PASSED: '{pcode}' + qty '{qty if qty else '(empty)'}'")
        safe_thread_send({
            "type": "final",
            "text": transcript,
            "data": {
                "pcode": pcode,
                "qty": qty,
                "confidence": confidence,
            }
        })
        if qty:
            session_state.clear()
            session_state.reset_letter_lock()
        else:
            session_state.wait_for_qty(pcode)
            session_state.reset_letter_lock()
    else:
        # No pcode extracted
        print(f"❌ NO PCODE EXTRACTED from '{transcript}'")
        safe_thread_send({
            "type": "validation_error",
            "error_code": "NO_PCODE_FOUND",
            "message": "Could not extract product code. Please speak clearly.",
            "pcode": "",
            "qty": ""
        })
        session_state.clear()
        session_state.reset_letter_lock()


def apply_first_letter_lock(transcript: str, session_state: SessionState) -> tuple:
    """
    🔒 Apply first letter lock to the FIRST WORD before joining
    Returns: (corrected_transcript, list_of_possible_codes or None)
    """
    if not transcript:
        return transcript, None

    transcript_stripped = transcript.strip()
    if not transcript_stripped:
        return transcript, None

    # Remove special characters
    transcript_cleaned = re.sub(r'[-,./\\()\[\]{}|;:\'"@#$%^&*+=~`<>?]', '', transcript_stripped)
    transcript_cleaned = transcript_cleaned.strip()

    print(f"🧹 Cleaned transcript: '{transcript_stripped}' → '{transcript_cleaned}'")

    if not transcript_cleaned:
        return transcript, None

    # Apply first letter lock
    if session_state.first_letter_lock:
        words = transcript_cleaned.split()

        if words:
            first_word = words[0]

            def is_pure_letters(word):
                return word.isalpha()

            if len(words) > 1 and is_pure_letters(first_word):
                words[0] = session_state.first_letter_lock
                print(f"🔧 REPLACED FIRST WORD: '{first_word}' → '{session_state.first_letter_lock}' (pure letters word)")
            else:
                if len(first_word) > 0:
                    words[0] = session_state.first_letter_lock + first_word[1:]
                    print(f"🔧 REPLACED FIRST CHAR: '{first_word}' → '{words[0]}' (contains digits or single word)")

            transcript_cleaned = ' '.join(words)
            print(f"🔒 APPLIED FIRST LETTER LOCK: '{transcript_stripped}' → '{transcript_cleaned}'")

    # Check for '8' or '3' prefix fallback
    if transcript_cleaned and (transcript_cleaned[0] == '8' or transcript_cleaned[0] == '3'):
        transcript_no_space = transcript_cleaned.replace(' ', '')

        if len(transcript_no_space) >= 5:
            prefix = transcript_no_space[0]
            suffix = transcript_no_space[1:5]        # 4-digit part
            remaining = transcript_no_space[5:]      # qty or extra text

            # -----------------------------
            # CASE 1: Prefix is '3' → try real product 3XXXX first
            # -----------------------------
            if prefix == '3':
                real_candidate = "3" + suffix
                if product_manager.exists(real_candidate):
                    print(f"✅ REAL MATCH DETECTED: {real_candidate}")
                    return real_candidate + remaining, None
                # If not found → fall-through to letter search

            # -----------------------------
            # CASE 2: Prefix is '8' OR fallback for '3'
            # → Try letter-based correction
            # -----------------------------
            print(f"🔍 Searching for letter matches with suffix '{suffix}'")

            possible = []
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                candidate = letter + suffix
                if product_manager.exists(candidate):
                    possible.append(candidate)
                    print(f"  ✓ Match: {candidate}")

            if possible:
                if len(possible) == 1:
                    final_code = possible[0] + remaining
                    print(f"🔁 PREFIX FIXED USING LETTER DICTIONARY: {final_code}")
                    return final_code, None
                else:
                    print(f"🔀 Multiple matches: {possible}")
                    return possible[0], possible  # default & list

    # No correction applied
    return transcript_cleaned, None



class ProductListManager:
    def __init__(self, csv_file: str = "productlist.csv"):
        self.pcode_list = []
        self.pcode_set = set()
        self.pcode_dict = {}
        self.load_product_list(csv_file)

    def load_product_list(self, csv_file: str):
        if not os.path.exists(csv_file):
            print(f"⚠️  Warning: {csv_file} not found. Running without product validation.")
            return
        try:
            with open(csv_file, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    pcode = str(row['productid']).strip()
                    if len(pcode) >= 3:
                        self.pcode_list.append(pcode)
                        self.pcode_set.add(pcode.lower())
                        self.pcode_dict[pcode.lower()] = pcode
            print(f"✅ Loaded {len(self.pcode_list)} product codes")
        except Exception as e:
            print(f"❌ Error loading CSV: {e}")
            import traceback
            traceback.print_exc()

    def exists(self, pcode: str) -> bool:
        return pcode.lower() in self.pcode_set

    def get_exact_case(self, pcode: str) -> str:
        return self.pcode_dict.get(pcode.lower(), pcode)

    def find_best_match(self, pcode: str, min_similarity: float = 0.80) -> Optional[str]:
        best_match = None
        best_score = 0.0
        pcode_clean = pcode.lower().strip()

        corrected_codes = CharacterConfusionCorrector.find_similar_pcodes(pcode_clean, self)
        if corrected_codes and corrected_codes[0] != pcode_clean:
            best_match = self.get_exact_case(corrected_codes[0])
            print(f"  🔧 CHARACTER CONFUSION CORRECTED: '{pcode}' → '{best_match}'")
            return best_match

        for prod_code in self.pcode_list[:5000]:
            prod_clean = str(prod_code).lower().strip()
            score = SequenceMatcher(None, pcode_clean, prod_clean).ratio()
            if score > best_score and score >= min_similarity:
                best_score = score
                best_match = prod_code
        if best_match:
            print(f"  🔍 Fuzzy matched '{pcode}' -> '{best_match}' (score: {best_score:.2f})")
            return best_match
        return None


class UltraTextProcessor:
    NUMBERS = {
        'zero': '0', 'oh': '0', 'o': '0',
        'one': '1', 'two': '2', 'to': '2', 'too': '2',
        'three': '3', 'four': '4', 'five': '5',
        'six': '6', 'seven': '7', 'eight': '8', 'nine': '9',
        'ten': '10', 'eleven': '11', 'twelve': '12', 'thirteen': '13',
        'fourteen': '14', 'fifteen': '15', 'sixteen': '16', 'seventeen': '17',
        'eighteen': '18', 'nineteen': '19',
        'twenty': '20', 'thirty': '30', 'forty': '40', 'fifty': '50',
        'sixty': '60', 'seventy': '70', 'eighty': '80', 'ninety': '90'
    }

    SIMILAR_CHARS = {
        'a': ['e', 'o', 'j'], 'b': ['d', 'p', 'v'], 'c': ['s', 'k', 'g'],
        'd': ['b', 't'], 'e': ['a', 'i', 'p'], 'f': ['v', 'p', 's'],
        'g': ['j', 'k', 'c'], 'h': ['n'], 'i': ['e', 'y'],
        'j': ['g', 'a'], 'k': ['c', 'g', 'q'], 'l': ['r', 'i'],
        'm': ['n'], 'n': ['m'], 'o': ['a', 'u'],
        'p': ['b', 'f', 'e'], 'q': ['k'], 'r': ['l'],
        's': ['f', 'c', 'z'], 't': ['d'], 'u': ['o'],
        'v': ['f', 'w'], 'w': ['v', 'double'], 'x': ['z'],
        'y': ['i'], 'z': ['s', 'x'],
    }

    @staticmethod
    def clean_text(text: str, product_manager: ProductListManager = None) -> str:
        """STEP 1: Initial text cleaning - PRESERVES SPACES for double/triple conversion"""
        text = text.lower().strip()

        # Handle compound numbers
        tens_pattern = r'\b(twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)\s+(one|two|three|four|five|six|seven|eight|nine)\b'

        def replace_compound(match):
            tens_map = {'twenty': '20', 'thirty': '30', 'forty': '40', 'fifty': '50',
                        'sixty': '60', 'seventy': '70', 'eighty': '80', 'ninety': '90'}
            ones_map = {'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5',
                        'six': '6', 'seven': '7', 'eight': '8', 'nine': '9'}
            return str(int(tens_map[match.group(1)]) + int(ones_map[match.group(2)]))

        text = re.sub(tens_pattern, replace_compound, text)

        # Replace piece variants
        piece_variants = [
            (r'\bplease\b', 'piece'), (r'\bpleas\b', 'piece'), (r'\bpeace\b', 'piece'),
            (r'\bpeas\b', 'piece'), (r'\bpees\b', 'piece'), (r'\bpease\b', 'piece'),
            (r'\bpeice\b', 'piece'), (r'\bpeese\b', 'piece'), (r'\bpies\b', 'piece'),
            (r'\bpis\b', 'piece'), (r'\bp\s+s\b', 'piece'), (r'\bps\b', 'piece'),
            (r'\bp\s+c\b', 'piece'), (r'\bpc\b', 'piece'), (r'\bpcs\b', 'piece'),
            (r'\bpeaces\b', 'piece'), (r'\bpeeses\b', 'piece'), (r'\bpiecies\b', 'piece'),
            (r'\bpieces\b', 'piece'),
            (r'\bquantity\b', 'piece'), (r'\bquantities\b', 'piece'), (r'\bqty\b', 'piece'),
        ]
        for pattern, replacement in piece_variants:
            text = re.sub(pattern, replacement, text)

        # Replace letter spellings
        letter_variants = [
            (r'\bhe is\b', 'e'),(r'\bhe\b', 'e'),(r"\bhe's\b", 'e'),(r'\bis\b', 'e'), (r'\bay\b', 'a'), (r'\bee\b', 'e'),
            (r'\bsee\b', 'c'), (r'\bdee\b', 'd'), (r'\bef\b', 'f'),
            (r'\bgee\b', 'g'), (r'\baych\b', 'h'), (r'\bjay\b', 'j'),
            (r'\bkay\b', 'k'), (r'\bell\b', 'l'), (r'\bem\b', 'm'),
            (r'\ben\b', 'n'), (r'\boh\b', 'o'), (r'\bpea\b', 'p'),
            (r'\bqueue\b', 'q'), (r'\bar\b', 'r'), (r'\bess\b', 's'),
            (r'\btea\b', 't'), (r'\byou\b', 'u'), (r'\bvee\b', 'v'),
            (r'\bex\b', 'x'), (r'\bwhy\b', 'y'),
            (r'\bzed\b', 'z'), (r'\bzee\b', 'z'),
            (r'\band\b', 'n'), (r'\ban\b', 'n'), (r'\bhigh\b', 'i'),
            (r'\bwhen\b', 'one'), (r'\bwon\b', 'one'), (r'\bwun\b', 'one'),
            (r'\btree\b', 'three'), (r'\bfife\b', 'five'),
            (r'\bsicks\b', 'six'), (r'\bsevun\b', 'seven'),
            (r'\bate\b', 'eight'), (r'\bait\b', 'eight'),
            (r'\bnein\b', 'nine'), (r'\btin\b', 'ten'), (r'\beven\b', 'E1'),
            (r'\bsaid\b', 'z'), (r'\btrip\b', 'triple'),
            (r'\b1380\b', '13ed'), (r'\bdege\b', 'h'),(r'\bhi\b', 'i'),(r'\bnew\b', 'u'),
        ]

        # First pass: replace with word boundaries
        for pattern, replacement in letter_variants:
            text = re.sub(pattern, replacement, text)

        # Second pass: handle joined words
        joined_word_replacements = {
            'and': 'n', 'an': 'n', 'high': 'i', 'he': 'e', 'ay': 'a',
            'bee': 'e', 'see': 'c', 'dee': 'd', 'ef': 'f', 'gee': 'g',
            'jay': 'j', 'kay': 'k', 'ell': 'l', 'em': 'm', 'en': 'n',
            'oh': 'o', 'pea': 'p', 'queue': 'q', 'ar': 'r', 'ess': 's',
            'tea': 't', 'you': 'u', 'vee': 'v', 'ex': 'x', 'why': 'y',
            'zed': 'z', 'zee': 'z'
        }

        for word, replacement in joined_word_replacements.items():
            if text.startswith(word) and len(text) > len(word):
                next_char = text[len(word)]
                if next_char.isdigit():
                    text = replacement + text[len(word):]
                    print(f"  🔧 Fixed joined word: '{word}' → '{replacement}' in '{text}'")
                    break

        # Remove filler words
        text = re.sub(r'\b(um|uh|like|you know|okay|ok|the|or|of|in|at|with|for|on)\b', ' ', text)

        # Remove punctuation (but keep spaces!)
        text = re.sub(r'[,!?;:\'"()-]', ' ', text)

        # Normalize multiple spaces
        text = re.sub(r'\s+', ' ', text).strip()

        return text

    @staticmethod
    def convert_doubles_triples(text: str) -> str:
        """Convert double/triple patterns"""
        print(f"    🔄 DOUBLE/TRIPLE INPUT: '{text}'")

        number_word_replacements = {
            'to': '2', 'too': '2', 'for': '4', 'ate': '8'
        }

        for word, replacement in number_word_replacements.items():
            text = re.sub(r'\b' + word + r'\b', replacement, text, flags=re.IGNORECASE)

        number_words = r"(?:zero|oh|o|one|two|2|three|four|4|five|six|seven|eight|8|nine)"
        max_iterations = 20

        for iteration in range(max_iterations):
            original = text

            text = re.sub(
                r'\bdouble\s+' + number_words + r'\b',
                lambda m: m.group(1) + ' ' + m.group(1),
                text,
                flags=re.IGNORECASE
            )
            text = re.sub(
                r'\btriple\s+' + number_words + r'\b',
                lambda m: m.group(1) + ' ' + m.group(1) + ' ' + m.group(1),
                text,
                flags=re.IGNORECASE
            )

            text = re.sub(r'([a-z0-9])(double|triple)', r'\1 \2', text, flags=re.IGNORECASE)
            text = re.sub(r'\bdouble\s*(\d)', lambda m: m.group(1) * 2, text, flags=re.IGNORECASE)
            text = re.sub(r'\btriple\s*(\d)', lambda m: m.group(1) * 3, text, flags=re.IGNORECASE)
            text = re.sub(r'\bdouble\s*([a-z])', lambda m: m.group(1).lower() * 2, text, flags=re.IGNORECASE)
            text = re.sub(r'\btriple\s*([a-z])', lambda m: m.group(1).lower() * 3, text, flags=re.IGNORECASE)
            text = re.sub(r'\bdouble(\d{2,})', lambda m: m.group(1)[0] * 2 + m.group(1)[1:], text, flags=re.IGNORECASE)
            text = re.sub(r'\btriple(\d{2,})', lambda m: m.group(1)[0] * 3 + m.group(1)[1:], text, flags=re.IGNORECASE)

            if 'double' in text.lower():
                text = re.sub(r'double\s*([a-z0-9])', lambda m: m.group(1) * 2, text, flags=re.IGNORECASE)
            if 'triple' in text.lower():
                text = re.sub(r'triple\s*([a-z0-9])', lambda m: m.group(1) * 3, text, flags=re.IGNORECASE)

            if text == original:
                break

            print(f"    🔄 Iteration {iteration + 1}: '{text}'")

        print(f"    ✅ DOUBLE/TRIPLE OUTPUT: '{text}'")
        return text

    @staticmethod
    def convert_number_words(text: str) -> str:
        """Convert number words to digits"""
        for word, digit in UltraTextProcessor.NUMBERS.items():
            text = re.sub(r'\b' + word + r'\b', digit, text, flags=re.IGNORECASE)
        return text

    @staticmethod
    def convert_large_units(text: str) -> str:
        """Convert phrases like 'three hundred'/'ten thousand' into digits"""

        def repl_thousand(m):
            a = int(m.group(1))
            b = m.group(2)
            tail = int(b) if b and b.isdigit() else 0
            return str(a * 1000 + tail)

        def repl_hundred(m):
            a = int(m.group(1))
            b = m.group(2)
            tail = int(b) if b and b.isdigit() else 0
            return str(a * 100 + tail)

        for _ in range(10):
            prev = text
            text = re.sub(
                r"\b(\d+)\b[\s,.;:-]*(?:and\s+)?thousand\b[\s,.;:-]*(?:and\s+)?(\d{1,3})?\b",
                repl_thousand, text, flags=re.IGNORECASE)
            text = re.sub(
                r"\b(\d+)\b[\s,.;:-]*(?:and\s+)?hundred\b[\s,.;:-]*(?:and\s+)?(\d{1,2})?\b",
                repl_hundred, text, flags=re.IGNORECASE)
            if text == prev:
                break
        return text

    @staticmethod
    def process(text: str, product_manager=None) -> str:
        """Processing pipeline"""
        print(f"  📥 RAW INPUT: '{text}'")

        text = UltraTextProcessor.clean_text(text, product_manager)
        print(f"  🧹 After clean_text: '{text}'")

        text = UltraTextProcessor.convert_doubles_triples(text)
        print(f"  2️⃣ After doubles/triples: '{text}'")

        text = UltraTextProcessor.convert_number_words(text)
        print(f"  🔢 After number words: '{text}'")

        text = UltraTextProcessor.convert_large_units(text)
        print(f"  💯 After large units: '{text}'")

        text = re.sub(r'\s+', '', text)
        text = text.replace('.', '')
        print(f"  ✅ FINAL OUTPUT: '{text}'")

        return text


class TranscriptionPipeline:
    """Complete processing pipeline with proper qty follow-up logic"""

    @staticmethod
    def process(raw_text: str, raw_original_text: Optional[str] = None, session_state=None, possible_codes: List[str] = None) -> Dict:
        """
        🔥 ENHANCED: Now accepts possible_codes parameter for ambiguous matches
        """
        print(f"\n{'=' * 70}")

        if not raw_text or not isinstance(raw_text, str):
            print(f"⚠️  Invalid input type, skipping")
            print(f"{'=' * 70}\n")
            return {
                "raw_text": "",
                "normalized": "",
                "candidates": [],
                "pcode": "",
                "qty": "",
                "confidence": 0.0,
                "is_qty_followup": False,
                "possible_codes": []  # 🔥 NEW
            }

        if not raw_text.strip() or raw_text.strip() in ['.', ',', '!', '?', '...']:
            print(f"⚠️  Empty or invalid input, skipping")
            print(f"{'=' * 70}\n")
            return {
                "raw_text": raw_text.strip(),
                "normalized": "",
                "candidates": [],
                "pcode": "",
                "qty": "",
                "confidence": 0.0,
                "is_qty_followup": False,
                "possible_codes": []  # 🔥 NEW
            }

        try:
            # Check for quantity follow-up
            if session_state and session_state.is_waiting():
                normalized_for_qty = UltraTextProcessor.process(raw_text, product_manager=product_manager)
                digits_only = re.sub(r'\D', '', normalized_for_qty)

                if digits_only:
                    try:
                        qty_val = int(digits_only)
                        if 1 <= qty_val <= 9999:
                            print(f"🔢 QUANTITY FOLLOW-UP: '{digits_only}' (waiting for {session_state.last_pcode})")
                            return {
                                "raw_text": raw_text.strip(),
                                "normalized": normalized_for_qty,
                                "candidates": [],
                                "pcode": "",
                                "qty": digits_only,
                                "confidence": 1.0,
                                "is_qty_followup": True,
                                "possible_codes": []  # 🔥 NEW
                            }
                    except:
                        pass

            # Normal processing pipeline
            normalized = UltraTextProcessor.process(raw_text, product_manager)
            original_for_processing = raw_text if raw_original_text is None else raw_original_text

            # 🔥 CRITICAL: Pass possible_codes to extractor
            candidates = SmartExtractor.extract(normalized, product_manager, possible_codes=possible_codes)

            if candidates:
                best_pcode, best_qty, conf = candidates[0]
                final_pcode, final_qty, final_conf = FinalValidator.validate(
                    best_pcode, best_qty, product_manager
                )

                # 🔥 NEW: Check if we have multiple options from possible_codes
                final_possible_codes = []
                if possible_codes and len(possible_codes) > 1:
                    final_possible_codes = possible_codes
                    print(f"🔀 RETURNING MULTIPLE OPTIONS: {final_possible_codes}")

                print(f"✅ FINAL -> Product: {final_pcode or 'NO MATCH'} | Qty: {final_qty or 'N/A'}")
                print(f"📊 Confidence: {final_conf}")
                print(f"{'=' * 70}\n")

                return {
                    "raw_text": raw_text.strip(),
                    "normalized": normalized,
                    "candidates": [(c[0], c[1]) for c in candidates[:3]],
                    "pcode": final_pcode,
                    "qty": final_qty,
                    "confidence": round(final_conf, 2),
                    "is_qty_followup": False,
                    "possible_codes": final_possible_codes  # 🔥 NEW
                }
            else:
                print(f"❌ NO MATCH FOUND")
                print(f"{'=' * 70}\n")
                return {
                    "raw_text": raw_text.strip(),
                    "normalized": normalized,
                    "candidates": [],
                    "pcode": "",
                    "qty": "",
                    "confidence": 0.0,
                    "is_qty_followup": False,
                    "possible_codes": []  # 🔥 NEW
                }

        except Exception as e:
            print(f"❌ ERROR in TranscriptionPipeline: {e}")
            import traceback
            traceback.print_exc()
            print(f"{'=' * 70}\n")
            return {
                "error": str(e),
                "raw_text": raw_text.strip(),
                "normalized": "",
                "candidates": [],
                "pcode": "",
                "qty": "",
                "confidence": 0.0,
                "is_qty_followup": False,
                "possible_codes": []  # 🔥 NEW
            }


class SmartExtractor:
    @staticmethod
    def extract(text: str, product_manager: ProductListManager, possible_codes: List[str] = None) -> List[Tuple[str, str, float]]:
        """
        🔥 ENHANCED: Now accepts possible_codes for ambiguous matches
        """
        text_no_space = text.replace(' ', '').lower()
        print(f"  🔧 Text (spaces removed): '{text_no_space}'")

        # 🔥 NEW: If we already have possible_codes from prefix correction, extract qty and return
        if possible_codes and len(possible_codes) > 1:
            print(f"  🔀 USING PRE-DETERMINED OPTIONS: {possible_codes}")

            # Try to extract quantity from the text
            # First, try to find where the pcode ends by checking against the first option
            first_option = possible_codes[0].lower()
            qty = ""

            if text_no_space.startswith(first_option):
                remaining = text_no_space[len(first_option):]
                digits_only = re.sub(r'\D', '', remaining)
                if digits_only and 1 <= int(digits_only) <= 9999:
                    qty = digits_only
                    print(f"  ✅ EXTRACTED QTY FROM AMBIGUOUS: '{qty}'")

            # Return the first option as the "primary" but frontend will handle all options
            pcode = product_manager.get_exact_case(possible_codes[0])
            return [(pcode, qty, 1.0 if qty else 0.95)]

        # Try direct extraction
        for end_pos in range(5, min(len(text_no_space) + 1, 20)):
            pcode_candidate = text_no_space[:end_pos]

            if product_manager.exists(pcode_candidate):
                remaining = text_no_space[end_pos:]
                qty = ""

                if remaining:
                    digits_only = re.sub(r'\D', '', remaining)
                    if digits_only and 1 <= int(digits_only) <= 9999:
                        qty = digits_only

                pcode = product_manager.get_exact_case(pcode_candidate)
                print(f"  ✅ DIRECT MATCH: '{pcode}' + qty '{qty if qty else '(none)'}'")
                return [(pcode, qty, 1.0 if qty else 0.95)]

        # Look for "piece" keyword
        piece_pos = -1
        piece_variants = ['piece', 'pieces']
        text_lower = text.lower()

        for variant in piece_variants:
            if variant in text_lower:
                piece_pos = text_lower.index(variant)
                print(f"  📍 Found '{variant}' at position {piece_pos}")
                break

        if piece_pos > 0:
            before_piece = text[:piece_pos].strip()
            after_piece = text[piece_pos:].strip()

            for variant in piece_variants:
                after_piece = re.sub(r'\b' + variant + r'\b', '', after_piece, flags=re.IGNORECASE)
            after_piece = after_piece.strip()

            print(f"  📦 Before piece: '{before_piece}'")
            print(f"  📊 After piece: '{after_piece}'")

            before_no_space = before_piece.replace(' ', '').lower()
            print(f"  🔧 Before (no spaces): '{before_no_space}'")

            qty = ""
            best_match = None
            best_match_type = None
            best_match_end_pos = 0

            candidates = []

            for end_pos in range(3, len(before_no_space) + 1):
                candidate = before_no_space[:end_pos]

                if not candidate[0].isalpha():
                    continue

                if not any(c.isdigit() for c in candidate):
                    continue

                if product_manager.exists(candidate):
                    remaining = before_no_space[end_pos:]
                    candidates.append({
                        'pcode': candidate,
                        'remaining': remaining,
                        'end_pos': end_pos,
                        'type': 'exact',
                        'priority': 1
                    })
                    print(f"  🎯 Found exact match candidate: '{candidate}' with remaining: '{remaining}'")

            if candidates:
                candidates.sort(key=lambda x: len(x['pcode']))
                best_candidate = candidates[0]
                best_match = best_candidate['pcode']
                best_match_type = 'exact'
                best_match_end_pos = best_candidate['end_pos']
                remaining = best_candidate['remaining']

                print(f"  ✅ Selected shortest exact match: '{best_match}' (from {len(candidates)} candidates)")

                if remaining:
                    digits_only = re.sub(r'\D', '', remaining)
                    if digits_only:
                        qty_val = int(digits_only)
                        if 1 <= qty_val <= 9999:
                            qty = digits_only
                            print(f"  ✅ Quantity from remaining: '{qty}'")

                pcode = product_manager.get_exact_case(best_match)
                print(f"  ✅ EXACT: '{pcode}' + qty '{qty}'")
                return [(pcode, qty, 1.0 if qty else 0.95)]

            for end_pos in range(3, len(before_no_space) + 1):
                candidate = before_no_space[:end_pos]

                if not candidate[0].isalpha():
                    continue

                if not any(c.isdigit() for c in candidate):
                    continue

                fuzzy = product_manager.find_best_match(candidate)
                if fuzzy:
                    best_match = candidate
                    best_match_type = 'fuzzy'
                    best_match_end_pos = end_pos
                    print(f"  🔍 Found fuzzy match: '{candidate}' -> '{fuzzy}'")
                    break

            if best_match and best_match_type == 'fuzzy':
                remaining = before_no_space[best_match_end_pos:]

                if remaining:
                    digits_only = re.sub(r'\D', '', remaining)
                    if digits_only:
                        qty_val = int(digits_only)
                        if 1 <= qty_val <= 9999:
                            qty = digits_only
                            print(f"  ✅ Quantity from remaining: '{qty}'")

                fuzzy_pcode = product_manager.find_best_match(best_match)
                if fuzzy_pcode:
                    print(f"  ✅ FUZZY: '{best_match}' -> '{fuzzy_pcode}' + qty '{qty}'")
                    return [(fuzzy_pcode, qty, 0.9 if qty else 0.85)]

            if not qty and after_piece:
                qty_match = re.search(r'\b(\d{1,4})\b', after_piece)
                if qty_match:
                    qty = qty_match.group(1)
                    print(f"  ✅ Quantity from after piece: '{qty}'")

            if before_no_space and len(before_no_space) >= 3:
                if before_no_space[0].isalpha() and any(c.isdigit() for c in before_no_space):
                    print(f"  ⚠️  Unverified: '{before_no_space}' + qty '{qty}'")
                    return [(before_no_space, qty, 0.5)]

        print(f"  🔍 No 'piece' found, trying direct extraction from: '{text_no_space}'")

        candidates = []
        for end_pos in range(5, len(text_no_space) + 1):
            candidate = text_no_space[:end_pos]

            if not candidate[0].isalpha():
                continue

            if not any(c.isdigit() for c in candidate):
                continue

            if product_manager.exists(candidate):
                remaining = text_no_space[end_pos:]
                digits_only = re.sub(r'\D', '', remaining)

                candidates.append({
                    'pcode': candidate,
                    'remaining': digits_only,
                    'end_pos': end_pos,
                    'type': 'exact'
                })
                print(f"  🎯 Direct match candidate: '{candidate}' with remaining: '{digits_only}'")

        if candidates:
            candidates.sort(key=lambda x: len(x['pcode']))
            best_candidate = candidates[0]

            pcode = product_manager.get_exact_case(best_candidate['pcode'])
            qty = best_candidate['remaining'] if best_candidate['remaining'] else ""

            if qty:
                try:
                    qty_val = int(qty)
                    if qty_val < 1 or qty_val > 9999:
                        qty = ""
                except:
                    qty = ""

            print(f"  ✅ Direct EXACT: '{pcode}' + qty '{qty}'")
            return [(pcode, qty, 1.0 if qty else 0.95)]

        for end_pos in range(3, len(text_no_space) + 1):
            candidate = text_no_space[:end_pos]

            if not candidate[0].isalpha():
                continue

            if not any(c.isdigit() for c in candidate):
                continue

            fuzzy = product_manager.find_best_match(candidate)
            if fuzzy:
                remaining = text_no_space[end_pos:]
                digits_only = re.sub(r'\D', '', remaining)

                qty = ""
                if digits_only:
                    try:
                        qty_val = int(digits_only)
                        if 1 <= qty_val <= 9999:
                            qty = digits_only
                    except:
                        pass

                print(f"  ✅ Direct FUZZY: '{candidate}' -> '{fuzzy}' + qty '{qty}'")
                return [(fuzzy, qty, 0.88 if qty else 0.83)]

        for pcode in product_manager.pcode_list[:5000]:
            pcode_lower = str(pcode).lower()
            if len(pcode_lower) < 3:
                continue

            if pcode_lower in text_no_space:
                idx = text_no_space.index(pcode_lower)
                after = text_no_space[idx + len(pcode_lower):]

                digits_only = re.sub(r'\D', '', after)
                qty_match = re.match(r'^(\d{1,4})', digits_only)
                qty = qty_match.group(1) if qty_match else ""

                print(f"  ✅ Found in text: '{pcode}' + qty '{qty}'")
                return [(pcode, qty, 0.95)]

        matches = re.findall(r'[a-z]+\d+[a-z0-9]*', text_no_space)
        for match in matches:
            if len(match) >= 3:
                fuzzy = product_manager.find_best_match(match)
                if fuzzy:
                    idx = text_no_space.index(match)
                    after = text_no_space[idx + len(match):]

                    digits_only = re.sub(r'\D', '', after)
                    qty_match = re.match(r'^(\d{1,4})', digits_only)
                    qty = qty_match.group(1) if qty_match else ""

                    print(f"  ✅ Pattern Fuzzy: '{match}' -> '{fuzzy}' + qty '{qty}'")
                    return [(fuzzy, qty, 0.88)]

        print(f"  ❌ No match found")
        return []


class CharacterConfusionCorrector:
    @staticmethod
    def find_similar_pcodes(pcode: str, product_manager: ProductListManager) -> List[str]:
        """Find product codes by trying sound-alike character substitutions"""
        if not pcode:
            return []

        if product_manager.exists(pcode):
            return [pcode]

        print(f"  🔍 Trying character confusion correction for: '{pcode}'")

        candidates = []
        tried_combinations = set()

        for i, char in enumerate(pcode.lower()):
            if char in UltraTextProcessor.SIMILAR_CHARS:
                for similar in UltraTextProcessor.SIMILAR_CHARS[char]:
                    variant = pcode[:i].lower() + similar + pcode[i + 1:].lower()

                    if variant not in tried_combinations:
                        tried_combinations.add(variant)
                        if product_manager.exists(variant):
                            candidates.append(variant)
                            print(f"  🔧 Single char correction: '{pcode}' → '{variant}' (position {i}: '{char}'→'{similar}')")

        if not candidates and len(pcode) >= 3:
            for i in range(min(3, len(pcode))):
                char1 = pcode[i].lower()
                if char1 in UltraTextProcessor.SIMILAR_CHARS:
                    for similar1 in UltraTextProcessor.SIMILAR_CHARS[char1]:
                        variant1 = pcode[:i].lower() + similar1 + pcode[i + 1:].lower()

                        for j in range(i + 1, min(4, len(pcode))):
                            char2 = variant1[j].lower()
                            if char2 in UltraTextProcessor.SIMILAR_CHARS:
                                for similar2 in UltraTextProcessor.SIMILAR_CHARS[char2]:
                                    variant2 = variant1[:j].lower() + similar2 + variant1[j + 1:].lower()

                                    if variant2 not in tried_combinations:
                                        tried_combinations.add(variant2)
                                        if product_manager.exists(variant2):
                                            candidates.append(variant2)
                                            print(f"  🔧 Double char correction: '{pcode}' → '{variant2}' (positions {i},{j})")

        return candidates if candidates else []


class FinalValidator:
    @staticmethod
    def validate(pcode: str, qty: str, product_manager: ProductListManager) -> Tuple[str, str, float]:
        qty_clean = re.sub(r'\D', '', qty) if qty else ""

        if qty_clean:
            try:
                qty_int = int(qty_clean)
                if qty_int < 1 or qty_int > 9999:
                    qty_clean = ""
            except:
                qty_clean = ""

        if product_manager.exists(pcode):
            pcode_exact = product_manager.get_exact_case(pcode)
            confidence = 1.0 if qty_clean else 0.95
            return pcode_exact, qty_clean, confidence

        similar = CharacterConfusionCorrector.find_similar_pcodes(pcode, product_manager)
        if similar and similar[0] != pcode.lower():
            pcode_exact = product_manager.get_exact_case(similar[0])
            confidence = 0.92 if qty_clean else 0.88
            print(f"  ✅ CHARACTER CONFUSION RESOLVED: '{pcode}' → '{pcode_exact}'")
            return pcode_exact, qty_clean, confidence

        fuzzy = product_manager.find_best_match(pcode)
        if fuzzy:
            confidence = 0.85 if qty_clean else 0.80
            return fuzzy, qty_clean, confidence

        return "", "", 0.0


# Initialize product manager
product_manager = ProductListManager("productlist.csv")

request_queue = asyncio.Queue(maxsize=100)
processing_lock = asyncio.Lock()
is_processing = False
model = None


async def process_audio_worker():
    """Background worker for processing audio with enhanced error handling"""
    global is_processing
    while True:
        try:
            audio_path, result_container = await request_queue.get()

            async with processing_lock:
                is_processing = True

            print(f"🔄 Processing queued audio: {audio_path}")

            try:
                result = model.transcribe(audio_path, language='en')
                raw_text = result["text"]

                processed = TranscriptionPipeline.process(raw_text, raw_original_text=raw_text)
                result_container['result'] = processed
                result_container['done'] = True

            except Exception as e:
                import traceback
                print(f"❌ Error processing audio: {e}")
                traceback.print_exc()
                result_container['result'] = {
                    "error": str(e),
                    "raw_text": "",
                    "pcode": "",
                    "qty": "",
                    "confidence": 0.0
                }
                result_container['done'] = True

            finally:
                try:
                    if os.path.exists(audio_path):
                        os.remove(audio_path)
                except Exception as cleanup_error:
                    print(f"⚠️  Cleanup warning: {cleanup_error}")

                async with processing_lock:
                    is_processing = False

                request_queue.task_done()

        except Exception as e:
            print(f"❌ Worker error: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(0.1)


@app.get("/queue_status")
async def queue_status():
    """Get current queue status"""
    return {
        "queue_size": request_queue.qsize(),
        "is_processing": is_processing,
        "max_queue_size": request_queue.maxsize
    }


@app.get("/")
async def health_check():
    """Health check endpoint with feature list"""
    return {
        "status": "online",
        "products_loaded": len(product_manager.pcode_list),
        "queue_size": request_queue.qsize(),
        "is_processing": is_processing,
        "features": [
            "🔥 FIXED: Ambiguous code handling with quantity follow-up",
            "✅ RULE 1: Ambiguous pcode = ALWAYS create entry immediately",
            "✅ RULE 2: Ambiguous + no qty = WAIT FOR QTY",
            "✅ RULE 3: Ambiguous + qty = finalize entry immediately",
            "✅ RULE 4: Selecting correct pcode later does NOT ask for qty again",
            "✅ RULE 5: Normal single pcode flow remains unchanged",
            "🔒 FIXED FIRST LETTER LOCK: Replaces entire convertible words",
            "🔧 SOUND-ALIKE: Character confusion correction for fuzzy matching",
            "🔁 A-Z FALLBACK: Dictionary-based prefix correction",
            "🔀 MULTIPLE OPTIONS: Shows all possible matches for ambiguous codes",
            "🔥 FIXED: Double/Triple recognition for ALL numbers (not just 0)",
            "✅ Complete space removal after processing",
            "✅ Quantity follow-up support",
            "✅ Real-time streaming via WebSocket",
            "✅ Invalid pcode detection"
        ]
    }


@app.on_event("startup")
async def startup_event():
    """Start background worker on server startup"""
    global model, request_queue, processing_lock, is_processing

    asyncio.create_task(process_audio_worker())
    print("🚀 Background audio processing worker started")


if __name__ == "__main__":
    print("🚀 Starting Speech-to-Text Server - AMBIGUOUS CODE FIX")
    print(f"📦 Loaded {len(product_manager.pcode_list)} product codes")
    print("\n🔧 KEY FIXES:")
    print("   ✅ RULE 1: Ambiguous pcode = ALWAYS create entry immediately")
    print("   ✅ RULE 2: Ambiguous + no qty = WAIT FOR QTY (link next quantity)")
    print("   ✅ RULE 3: Ambiguous + qty = finalize entry immediately (no waiting)")
    print("   ✅ RULE 4: Selecting correct pcode later does NOT ask for qty again")
    print("   ✅ RULE 5: Normal single pcode flow remains unchanged")
    print("\n🔒 EXISTING FEATURES:")
    print("   ✅ First letter lock replaces ENTIRE convertible words like 'is'")
    print("   ✅ A-Z dictionary fallback for unknown '8' prefixes")
    print("   ✅ Character confusion correction (P→E/B/F sound-alike substitution)")
    print("   ✅ Double/Triple works for ALL numbers (1-9), not just 0")
    print("   ✅ Handles 'K0 double 5 9' correctly -> 'K0559'")
    print("\n✨ Server ready on http://0.0.0.0:5000\n")
    uvicorn.run(app, host="0.0.0.0", port=5000)