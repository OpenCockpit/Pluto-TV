# Copyright (C) 2026 by xcentaurix
# License: GNU General Public License v3.0

import subprocess
import threading

try:
    from Crypto.Cipher import AES as _pycryptodome_AES
except ImportError:
    _pycryptodome_AES = None


class AES128:
    """AES-128-CBC utilities for segment decryption.

    Decrypts with pycryptodome (python3-pycryptodome) when it's installed;
    otherwise falls back to shelling out to openssl. The openssl path forks
    this process from whichever of several concurrent fetch-worker threads
    needs a segment decrypted - CPython can't release the GIL around fork(),
    so every such call stalls every other Python thread, including the
    reactor, for as long as the fork takes.
    """

    _key_cache: dict = {}
    _key_cache_lock = threading.Lock()
    _key_fetch_locks: dict = {}
    _key_fetch_locks_lock = threading.Lock()

    @staticmethod
    def iv(iv_explicit: 'bytes | None', seq: int) -> bytes:
        """Return the IV: explicit bytes if provided, otherwise the segment
        sequence number encoded as a big-endian 128-bit integer (HLS rule)."""
        return iv_explicit if iv_explicit is not None else seq.to_bytes(16, 'big')

    @staticmethod
    def decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
        """Decrypt AES-128-CBC *data* with *key* and *iv*, strip PKCS#7 padding.

        Returns empty bytes on failure.
        """
        raw = AES128._decrypt_raw(data, key, iv)
        if not raw:
            return b''
        pad = raw[-1]
        if 1 <= pad <= 16:
            raw = raw[:-pad]
        return raw

    @staticmethod
    def _decrypt_raw(data: bytes, key: bytes, iv: bytes) -> bytes:
        if _pycryptodome_AES is not None:
            try:
                return _pycryptodome_AES.new(key, _pycryptodome_AES.MODE_CBC, iv).decrypt(data)
            except Exception:
                return b''
        try:
            r = subprocess.run(
                ['openssl', 'enc', '-d', '-aes-128-cbc',
                 '-K', key.hex(), '-iv', iv.hex(), '-nosalt', '-nopad'],
                input=data, capture_output=True, timeout=30, check=False,
            )
            if r.returncode != 0 or not r.stdout:
                return b''
            return r.stdout
        except Exception:
            return b''

    @classmethod
    def fetch_key(cls, key_url: str, fetcher) -> 'bytes | None':
        """Fetch and cache a 16-byte AES-128 key.

        *fetcher* is a callable ``fetcher(url, binary=True) -> bytes | None``
        used to retrieve the key bytes (e.g. the proxy's ``_fetch`` helper).

        A per-key lock serializes concurrent misses so that N parallel
        segment-fetch workers hitting a cold key (e.g. the first playlist
        request on channel start, fetched by _AUDIO_FETCH_WORKERS threads at
        once) issue one HTTP request instead of N redundant ones - the extra
        latency on that first, synchronous request is exactly what can push
        GStreamer's manifest fetch past its timeout.
        """
        with cls._key_cache_lock:
            cached = cls._key_cache.get(key_url)
        if cached:
            return cached

        with cls._key_fetch_locks_lock:
            lock = cls._key_fetch_locks.setdefault(key_url, threading.Lock())

        with lock:
            with cls._key_cache_lock:
                cached = cls._key_cache.get(key_url)
            if cached:
                return cached
            for _attempt in range(2):
                data = fetcher(key_url, binary=True)
                if data and len(data) == 16:
                    with cls._key_cache_lock:
                        cls._key_cache[key_url] = data
                    return data
            return None
