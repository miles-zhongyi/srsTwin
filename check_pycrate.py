import pycrate_asn1dir, os
d = os.path.dirname(pycrate_asn1dir.__file__)
files = [f for f in sorted(os.listdir(d)) if not f.startswith('_')]
lte_files = [f for f in files if 'LTE' in f or 'EUTR' in f or '36' in f]
print('pycrate_asn1dir LTE/36 modules:', lte_files)
print('All modules (first 30):', files[:30])
