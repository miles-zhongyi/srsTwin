# S1AP states
S_IDLE           = "IDLE"
S_CONNECTING     = "CONNECTING"       # after INITIAL_UE_MESSAGE
S_CONTEXT_ACTIVE = "CONTEXT_ACTIVE"   # after INITIAL_CONTEXT_SETUP_RESPONSE
S_RELEASING      = "RELEASING"        # after UE_CONTEXT_RELEASE_COMMAND
S_HO_PENDING     = "HO_PENDING"       # after HANDOVER_REQUIRED
S_DONE           = "DONE"             # terminal

# Message name fragments used for pattern matching
_TRANSITIONS = {
  S_IDLE: [
    ("S1_INITIAL_UE_MESSAGE",                S_CONNECTING),
    ("S1_UE_CONTEXT_RELEASE_COMMAND",         S_RELEASING),  # partial session
  ],
  S_CONNECTING: [
    ("S1_UPLINK_NAS_TRANSPORT",               S_CONNECTING),
    ("S1_DOWNLINK_NAS_TRANSPORT",             S_CONNECTING),
    ("S1_INITIAL_CONTEXT_SETUP_REQUEST",      S_CONNECTING),
    ("S1_INITIAL_CONTEXT_SETUP_RESPONSE",     S_CONTEXT_ACTIVE),
    ("S1_UE_CONTEXT_RELEASE_COMMAND",         S_RELEASING),
    ("S1_LOCATION_REPORTING_CONTROL",         S_CONNECTING),
    ("S1_LOCATION_REPORT",                    S_CONNECTING),
    ("S1_UE_CAPABILITY_INDICATION",           S_CONNECTING),
    ("S1_CELL_TRAFFIC_TRACE",                 S_CONNECTING),
    ("S1_MME_STATUS_TRANSFER",                S_CONNECTING),
  ],
  S_CONTEXT_ACTIVE: [
    ("S1_UPLINK_NAS_TRANSPORT",               S_CONTEXT_ACTIVE),
    ("S1_DOWNLINK_NAS_TRANSPORT",             S_CONTEXT_ACTIVE),
    ("S1_ERAB_SETUP_REQUEST",                 S_CONTEXT_ACTIVE),
    ("S1_ERAB_SETUP_RESPONSE",                S_CONTEXT_ACTIVE),
    ("S1_ERAB_RELEASE_COMMAND",               S_CONTEXT_ACTIVE),
    ("S1_ERAB_RELEASE_RESPONSE",              S_CONTEXT_ACTIVE),
    ("S1_UE_CAPABILITY_INDICATION",           S_CONTEXT_ACTIVE),
    ("S1_LOCATION_REPORTING_CONTROL",         S_CONTEXT_ACTIVE),
    ("S1_LOCATION_REPORT",                    S_CONTEXT_ACTIVE),
    ("S1_CELL_TRAFFIC_TRACE",                 S_CONTEXT_ACTIVE),
    ("S1_HANDOVER_REQUIRED",                  S_HO_PENDING),
    ("S1_PATH_SWITCH_REQUEST",                S_CONTEXT_ACTIVE),
    ("S1_PATH_SWITCH_REQUEST_ACKNOWLEDGE",    S_CONTEXT_ACTIVE),
    ("S1_UE_CONTEXT_RELEASE_REQUEST",         S_RELEASING),
    ("S1_UE_CONTEXT_RELEASE_COMMAND",         S_RELEASING),
    ("S1_MME_STATUS_TRANSFER",                S_CONTEXT_ACTIVE),
  ],
  S_HO_PENDING: [
    ("S1_HANDOVER_PREPARATION_FAILURE",       S_CONTEXT_ACTIVE),
    ("S1_UE_CONTEXT_RELEASE_COMMAND",         S_RELEASING),
    ("S1_DOWNLINK_NAS_TRANSPORT",             S_HO_PENDING),
  ],
  S_RELEASING: [
    ("S1_UE_CONTEXT_RELEASE_COMPLETE",        S_DONE),
    # Allow stray messages that arrive before release completes
    ("S1_UPLINK_NAS_TRANSPORT",               S_RELEASING),
    ("S1_LOCATION_REPORT",                    S_RELEASING),
  ],
  S_DONE: [],
}

# Pre-index: state -> list of valid next message names
VALID_NEXT = {state: [msg for msg, _ in txns] for state, txns in _TRANSITIONS.items()}

# Pre-index: (state, message_fragment) -> next_state
_STATE_MAP = {}
for state, txns in _TRANSITIONS.items():
  for msg, next_state in txns:
    _STATE_MAP[(state, msg)] = next_state

def next_state(current_state, message_name):

  """Return the next state given current state and observed message name."""
  # Exact match first
  key = (current_state, message_name)
  if key in _STATE_MAP:
      return _STATE_MAP[key]
  # Partial match (message_name may have prefix/suffix noise)
  for (state, msg), nxt in _STATE_MAP.items():
      if state == current_state and (msg in message_name or message_name in msg):
          return nxt
  # Unknown transition: stay in current state rather than crash
  return current_state

def valid_next_messages(current_state):

  """Return list of valid next message names from current state."""
  return VALID_NEXT.get(current_state, [])

def session_state_trace(session):

  """
  Walk a session's events and return the state at each step.
  Useful for validating reconstructed sessions and diagnosing issues.
  """
  state = S_IDLE
  trace = [state]
  for evt in session["events"]:
    state = next_state(state, evt["message_name"])
    trace.append(state)
  return trace


def validate_session(session):

  """Returns True if all transitions in the session are valid."""
  state = S_IDLE
  for evt in session["events"]:
    valid = valid_next_messages(state)
    msg = evt["message_name"]
    matched = any(v in msg or msg in v for v in valid)
    if not matched:
        return False, f"Invalid: {msg} from state {state}"
    state = next_state(state, msg)
  return True, "ok"
