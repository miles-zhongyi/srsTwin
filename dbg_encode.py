import sys, copy, traceback
sys.path.insert(0, r'd:\DodonaData\DigitalTwins\srsTwin\srsRAN_4G\rrc_trace')
from encode_templates import _minimal_pycrate_val, _RRC_MOD

for choice in ['rrcConnectionSetupComplete', 'rrcConnectionRelease']:
    print(f"\n--- {choice} ---")
    val = _minimal_pycrate_val(choice)
    if val is None:
        print("No minimal template defined")
        continue
    pdu_type = "UL-DCCH-Message" if "complete" in choice.lower() or "report" in choice.lower() else "DL-DCCH-Message"
    pdu_type = "DL-DCCH-Message" if "release" in choice.lower() else pdu_type
    asn_obj = copy.deepcopy(_RRC_MOD[pdu_type])
    try:
        asn_obj.set_val({"message": val})
        per = asn_obj.to_uper()
        print(f"OK: {per.hex()} ({len(per)} bytes)")
    except Exception as e:
        traceback.print_exc()

# Test rrcConnectionRelease minimal
print("\n--- rrcConnectionRelease DL ---")
# DL-DCCH-Message rrcConnectionRelease
val2 = ("c1", ("rrcConnectionRelease", {
    "rrc-TransactionIdentifier": 0,
    "criticalExtensions": ("c1", ("rrcConnectionRelease-r8", {
        "releaseCause": "other",
    }))
}))
asn_obj = copy.deepcopy(_RRC_MOD["DL-DCCH-Message"])
try:
    asn_obj.set_val({"message": val2})
    per = asn_obj.to_uper()
    print(f"OK: {per.hex()} ({len(per)} bytes)")
except Exception as e:
    traceback.print_exc()
