import pycrate_asn1rt.utils as u
import pycrate_asn1dir.RRCLTE as R

# Find all ASN.1 SEQUENCE/CHOICE objects loaded in the module
# pycrate compiled modules expose their objects via _get_mod() or the global dict
import sys
mod = sys.modules.get('pycrate_asn1dir.RRCLTE')

# Objects are directly in the module namespace; filter by pycrate ASN1 type
from pycrate_asn1rt.asnobj import ASN1Obj
objs = {k: v for k, v in vars(mod).items()
        if isinstance(v, ASN1Obj) and not k.startswith('_')}
print(f"Found {len(objs)} ASN1 objects")
# Look for UL-CCCH
ccch = {k: v for k, v in objs.items() if 'CCCH' in k or 'ccch' in k.lower()}
print("CCCH objects:", list(ccch.keys())[:20])
# All top-level message types
msgs = {k: v for k, v in objs.items() if 'Message' in k}
print("Message objects:", list(msgs.keys())[:20])
print("All object names (first 30):", list(objs.keys())[:30])
