/**
 * M0 scaffolding — interface skeleton only. NOT included by any build
 * target. NOT yet inside srsRAN_4G/srsue/hdr/. M1 implements this for real
 * inside (or adjacent to) srsue/hdr/stack/ue_stack_lte.h, once the dispatch
 * layer and shared uplink grid construction land — see PLAN.md section 2.
 *
 * Purpose now: pin down the exact shape M1 builds against, reviewed before
 * any srsue source is touched, per the agreed process ("ask before any
 * large refactor of srsRAN internals").
 *
 * Confirmed in PLAN.md section 1 (read alongside this file):
 *   - srsue::ue_rnti (mac_common.h) is already a clean per-UE RNTI bundle
 *     (crnti/rar_rnti/temp_rnti/tpc_rnti/sps_rnti + its own mutex) — reused
 *     as-is, one instance per UeContext instead of one process-wide
 *     singleton.
 *   - mux/demux are LCID-keyed internally, which is fine as long as each
 *     UeContext owns its own mux/demux instance — LCID 0 for UE A and
 *     LCID 0 for UE B never share a table.
 *   - rrc.cc/nas.cc/usim.cc need no internal changes — they only ever
 *     touch "my own state," which stays true with N instances.
 */

#ifndef SRSTWIN_REALIZER_UE_CONTEXT_H
#define SRSTWIN_REALIZER_UE_CONTEXT_H

#include <cstdint>
#include <memory>
#include <string>

// Forward declarations only — this header does not pull in the real
// srsue/ headers yet, so it cannot be accidentally included by anything
// and silently change build behavior before M1.
namespace srsue {
class mac;
class rrc;
class nas;
class ue_rnti;
class usim_base;
namespace lte {
class proc_ra;
}
} // namespace srsue
namespace srsran {
class rlc;
class pdcp;
} // namespace srsran

namespace srstwin {
namespace realizer {

/**
 * One logical UE's L2/L3 state, bundled the way ue_stack_lte owns a single
 * UE's worth of state today (srsue/hdr/stack/ue_stack_lte.h:250-260) — this
 * struct is what changes ue_stack_lte's ownership model from "one of each"
 * to "N of these."
 *
 * ue_id is a STABLE logical identifier assigned at provisioning time and
 * held for the UE's whole lifecycle. It is deliberately NOT the RNTI:
 * RNTI changes across random access (TC-RNTI -> C-RNTI) and across
 * re-establishment, but ue_id must keep referring to the same logical UE
 * (and the same row in gen_user_db.py's subscriber set) throughout.
 */
struct UeContext {
  std::string ue_id;

  // RNTI state — one ue_rnti instance per UE (see header comment above).
  std::unique_ptr<srsue::ue_rnti> rnti;

  // L2/L3 — existing classes, unchanged internally, one instance per UE.
  std::unique_ptr<srsue::mac>  mac;
  std::unique_ptr<srsran::rlc> rlc;
  std::unique_ptr<srsran::pdcp> pdcp;
  std::unique_ptr<srsue::rrc>  rrc;
  std::unique_ptr<srsue::nas>  nas;
  std::unique_ptr<srsue::usim_base> usim;   // own IMSI/Ki/OPc — see gen_user_db.py

  // Random access state — own preamble choice, power-ramp counter,
  // backoff/contention-resolution timers. Per PLAN.md risk #2: this
  // context's timers must come from its own timer_handler allocation,
  // never a pool shared/indexed only by timer-type across UEs.
  std::unique_ptr<srsue::lte::proc_ra> ra_state;

  // HARQ — PLAN.md assumption #2: CC count = 1 for this twin, so this is
  // a flat per-UE pair, not yet indexed by component carrier. If carrier
  // aggregation is ever needed, generalize to per-(ue, cc) at that point,
  // not before — the existing dl_harq_entity(cc_idx_) constructor already
  // supports that axis, it's just unused here.
  // (Concrete types intentionally omitted from this skeleton — dl_harq_entity
  // and ul_harq_entity are constructed with phy/demux pointers that don't
  // exist until the shared PHY worker wiring lands in M1.)
};

} // namespace realizer
} // namespace srstwin

#endif // SRSTWIN_REALIZER_UE_CONTEXT_H
