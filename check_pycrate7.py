from pycrate_asn1rt.init import init_modules
import pycrate_asn1dir.RRCLTE as R

init_modules(R.EUTRA_RRC_Definitions)
GLOBAL = R.GLOBAL

# Check the actual internal format by decoding a real RRC packet
# Known RRC Connection Request minimal UPER bytes
# (mo-Data cause, all-zero random value, from srsRAN test output)
# Typical test packet: 40 00 00 00 00 00
known_rrc_req = bytes.fromhex('400000000000')  # 48 bits = 6 bytes

UL_CCCH = GLOBAL.MOD['EUTRA-RRC-Definitions']['UL-CCCH-Message']
try:
    UL_CCCH.from_uper(known_rrc_req)
    v = UL_CCCH.get_val()
    print("Decoded get_val():", repr(v))
    print()
    # Re-encode to get same bytes
    per_out = UL_CCCH.to_uper()
    print("Re-encoded:", per_out.hex())
    print("Match:", per_out == known_rrc_req[:len(per_out)])
except Exception as e:
    import traceback; traceback.print_exc()
