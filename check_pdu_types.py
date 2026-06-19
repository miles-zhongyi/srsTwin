import pycrate_asn1dir, os
d = os.path.dirname(pycrate_asn1dir.__file__)
files = [f for f in sorted(os.listdir(d)) if 'S1' in f or 'RAN' in f or 'NG' in f.upper() or 'NR' in f]
print('S1/NG/NR modules:', files)
