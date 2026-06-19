import pycrate_asn1dir.RRCLTE as R
import inspect, os

# Get file location
print("RRCLTE file:", R.__file__)
# Print first 100 lines
with open(R.__file__, encoding='utf-8') as f:
    for i, line in enumerate(f):
        if i >= 50: break
        print(f"{i+1:3}: {line}", end='')
