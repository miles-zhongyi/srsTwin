# Trace stitch engine

Stitches **22_decoded** TELUS trace records into **per-UE call flows** across trace files and connection sessions.

## What it does

1. **Streams** all `*_TRC_decoded.json` files under `22_decoded/`
2. **Segments sessions** using S1AP UE ids (`enb_ue_s1ap_id`, `mme_ue_s1ap_id`) and time-correlated RRC records
3. **Identifies UEs** using, in priority order:
   - IMSI (from NAS hex in S1 messages)
   - `randomValue` from `rrcConnectionRequest`
   - S-TMSI / M-TMSI
   - S1AP UE id (per session)
4. **Merges sessions** for the same UE when IMSI or `randomValue` match across files
5. **Orders** the stitched timeline by 3GPP attach phase + timestamp

## Usage

```powershell
cd srsTwin\integration\trace_stitch

# List discovered UEs (top by record count)
python run_stitch.py --list-ues --max-files 10

# Print ordered flow for one UE
python run_stitch.py --ue enb:877 --print-flow --max-files 5

# Export all UE timelines + index
python run_stitch.py --export-dir ..\..\22_decoded_stitched --max-files 50
```

Output JSON per UE includes:
- `identity` — stable keys (random, IMSI, PLMN, cell)
- `sessions` — connection sessions with S1AP ids and time bounds
- `flow` — ordered message list with `flow_phase` for analysis

## Notes

- Anonymised traces often use a constant `m_tmsi` (e.g. `1048575`); those records are **not** used as a UE key.
- Cross-session stitching relies on **IMSI** or **RRC randomValue**; otherwise each S1AP session is a separate UE bucket (`enb:<id>`).
- Large trace dirs: use `--max-files` while iterating, then run without limit for production export.

## Tests

```powershell
python -m unittest test_stitch_engine.py -v
```
