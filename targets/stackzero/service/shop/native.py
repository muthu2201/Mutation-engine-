"""ctypes binding to libshopnative (fuzzy BM25 ranking written in C)."""

import ctypes
import os

from shop.config import settings

_lib = None


def _load():
    global _lib
    if _lib is None:
        path = settings.native_lib or os.path.join(os.path.dirname(__file__), "..", "..", "native", "build", "libshopnative.so")
        lib = ctypes.CDLL(os.path.abspath(path))
        lib.shop_score_batch.argtypes = [
            ctypes.POINTER(ctypes.c_char_p),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_char_p),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_double),
        ]
        lib.shop_score_batch.restype = ctypes.c_int
        _lib = lib
    return _lib


def score(terms, docs):
    """BM25 relevance of each document for the query terms, with typo-tolerant matching."""
    lib = _load()
    n = len(docs)
    if n == 0:
        return []
    term_array = (ctypes.c_char_p * len(terms))(*[t.encode() for t in terms])
    doc_array = (ctypes.c_char_p * n)(*[d.encode() for d in docs])
    out = (ctypes.c_double * n)()
    rc = lib.shop_score_batch(term_array, len(terms), doc_array, n, out)
    if rc != 0:
        raise RuntimeError(f"shop_score_batch failed with code {rc}")
    return [out[i] for i in range(n)]
