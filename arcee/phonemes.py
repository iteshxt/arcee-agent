"""Text to phonemes for Arcee's voice (decision 0019), by Ubuntu's own libespeak-ng.

sherpa-onnx 2.0 no longer carries espeak-ng: a voice that speaks in phonemes, as Kitten
does, is handed them. libespeak-ng1 is on every Ubuntu desktop (speech-dispatcher's
voice uses it), and the .deb depends on it; it is called here through ctypes, as
KittenTTS's own `phonemizer` does. Punctuation is kept between the phonemes, so the
voice pauses where the text does ("Done. I set it" lost its pause without it).
"""

import ctypes
import ctypes.util
import re
import threading
from typing import Final

from arcee.voice import VoiceError

LANGUAGE: Final = b"en-us"
_CHARS_UTF8: Final = 1
_IPA: Final = 0x02
_SYNCHRONOUS: Final = 2
# Kept as they are between the phonemes of what is around them.
_PUNCTUATION: Final = re.compile(r'(\s*[;:,.!?¡¿—…"«»“”()]+\s*)')
_WORDS: Final = re.compile(r"\w+|[^\w\s]")

_lock = threading.Lock()  # espeak-ng is not safe from two threads at once
_lib: ctypes.CDLL | None = None


def _espeak() -> ctypes.CDLL:
    global _lib
    if _lib is not None:
        return _lib
    name = ctypes.util.find_library("espeak-ng") or "libespeak-ng.so.1"
    try:
        lib = ctypes.CDLL(name)
    except OSError as exc:
        raise VoiceError(
            "Arcee's voice needs libespeak-ng1 (sudo apt install libespeak-ng1)"
        ) from exc
    lib.espeak_Initialize.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    lib.espeak_SetVoiceByName.argtypes = [ctypes.c_char_p]
    lib.espeak_TextToPhonemes.argtypes = [
        ctypes.POINTER(ctypes.c_char_p),
        ctypes.c_int,
        ctypes.c_int,
    ]
    lib.espeak_TextToPhonemes.restype = ctypes.c_char_p
    if lib.espeak_Initialize(_SYNCHRONOUS, 0, None, 0) <= 0:
        raise VoiceError("libespeak-ng would not start (is espeak-ng-data installed?)")
    if lib.espeak_SetVoiceByName(LANGUAGE) != 0:
        raise VoiceError("libespeak-ng has no English voice")
    _lib = lib
    return lib


def _ipa(text: str) -> str:
    lib = _espeak()
    buffer = ctypes.c_char_p(text.encode())
    pointer = ctypes.pointer(buffer)
    clauses: list[str] = []
    while buffer.value:  # one clause a call; the pointer moves on, NULL at the end
        if phonemes := lib.espeak_TextToPhonemes(pointer, _CHARS_UTF8, _IPA):
            clauses.append(phonemes.decode())
    return " ".join(clauses)


def phonemes(text: str) -> str:
    """`text` in IPA with stress marks, its punctuation kept where it was."""
    out: list[str] = []
    with _lock:
        for piece in _PUNCTUATION.split(text):
            if not piece:
                continue
            if _PUNCTUATION.fullmatch(piece):
                out.append(piece.strip())
            elif piece.strip():
                out.append(_ipa(piece.strip()))
    return " ".join(out)


def symbols(text: str) -> list[str]:
    """The symbols Kitten reads for `text`: its phonemes, words and punctuation apart
    by single spaces, as KittenTTS gives them."""
    return list(" ".join(_WORDS.findall(phonemes(text))))
