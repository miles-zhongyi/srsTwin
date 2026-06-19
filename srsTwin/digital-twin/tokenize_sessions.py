# Time delta bucket boundaries in milliseconds.
# Bucket index = position in this list where value falls.
# 20-100ms previously held ~47% of all events in one bucket; split at 40ms
# (the natural break point in this dataset's dt distribution) for resolution
# where most of the data actually lives.
TIME_BUCKETS = [0, 5, 20, 40, 100, 500, 2000, 10000]

SPECIAL_TOKENS = ["<PAD>", "<BOS>", "<EOS>", "<UNK>"]


def dt_bucket(dt_ms):

  for i, boundary in enumerate(TIME_BUCKETS):
    if dt_ms <= boundary:
      return i

  return len(TIME_BUCKETS)


def session_to_tokens(session):
  """
  Convert one session to a list of string tokens:
    [CELL_{id}, <BOS>, MSG|DIR|T{bucket}, ..., <EOS>]
  """
  tokens = [f"CELL_{session['cell_id']}", "<BOS>"]
  for evt in session["events"]:
    direction = "UL" if evt["direction"] == 2 else "DL"
    bucket = dt_bucket(evt["dt_ms"])
    tokens.append(f"{evt['message_name']}|{direction}|T{bucket}")
  tokens.append("<EOS>")

  return tokens


def build_vocab(all_token_seqs):

  vocab = {tok: i for i, tok in enumerate(SPECIAL_TOKENS)}
  for seq in all_token_seqs:
    for tok in seq:
      if tok not in vocab:
        vocab[tok] = len(vocab)
  return vocab


def encode(token_seq, vocab):

  unk = vocab["<UNK>"]
  return [vocab.get(tok, unk) for tok in token_seq]
