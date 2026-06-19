from pycrate_asn1dir import RRCLTE

# Check what modules / namespaces are defined in RRCLTE
# RRCLTE should expose ASN.1 compiled modules
import pycrate_asn1rt.asnobj as ao

# Try getting the ASN1 objects by name
mods = RRCLTE.RRCLTE_PDU_DESCRIPTIONS
print(type(mods))
print(list(mods)[:20] if hasattr(mods, '__iter__') else mods)
