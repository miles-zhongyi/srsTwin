from pycrate_asn1rt.init import init_modules
import pycrate_asn1dir.RRCLTE as R
import inspect

# Find classes (ASN1 module containers)
classes = [(name, cls) for name, cls in inspect.getmembers(R, inspect.isclass)
           if hasattr(cls, '_obj_') and hasattr(cls, '_name_')]
print(f"Found {len(classes)} ASN1 module classes:")
for name, cls in classes[:10]:
    print(f"  {name}: {cls._name_}")

# Find EUTRA-RRC-Definitions
rrc_cls = next((cls for _, cls in classes if 'RRC-Definitions' in cls._name_), None)
if rrc_cls:
    print(f"\nRRC Definitions class: {rrc_cls._name_}")
    init_modules(rrc_cls)
    # Now access UL-CCCH-Message
    ulccch = rrc_cls._obj_  # list of object names
    ccch_objs = [o for o in ulccch if 'CCCH' in o or 'ccch' in o.lower()]
    print("CCCH objects:", ccch_objs)
