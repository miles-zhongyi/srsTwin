from pycrate_asn1dir import RRCLTE
import sys

# See what PDU types are available
types = [x for x in dir(RRCLTE) if not x.startswith('_')]
print('Types in RRCLTE (first 40):', types[:40])

# Try to get UL-CCCH-Message
if 'UL_CCCH_Message' in dir(RRCLTE):
    print('UL_CCCH_Message found')
elif 'ULCCCH_Message' in dir(RRCLTE):
    print('ULCCCH_Message found')
else:
    ccch = [t for t in types if 'CCCH' in t]
    print('CCCH types:', ccch)

ul = [t for t in types if 'UL' in t or 'Ul' in t]
print('UL types:', ul[:10])
