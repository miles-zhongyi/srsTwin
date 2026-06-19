import math

import torch
import torch.nn as nn
import torch.nn.functional as F

from state_machine import S_IDLE, next_state, valid_next_messages


class CausalSelfAttention(nn.Module):

  def __init__(self, d_model, n_heads, max_len, dropout):

    super().__init__()
    assert d_model % n_heads == 0

    self.n_heads = n_heads
    self.head_dim = d_model // n_heads
    self.qkv = nn.Linear(d_model , 3 * d_model, bias=False)
    self.proj = nn.Linear(d_model, d_model, bias=False)
    self.dropout = nn.Dropout(dropout)
    mask = torch.triu(torch.ones(max_len, max_len), diagonal=1).bool()
    self.register_buffer("causal_mask", mask)

  def forward(self, x):

    B, T, C = x.shape
    q, k, v = self.qkv(x).split(C, dim=2)
    q = q.view(B, T, self.n_heads, self.head_dim).transpose(1, 2)
    k = k.view(B, T, self.n_heads, self.head_dim).transpose(1, 2)
    v = v.view(B, T, self.n_heads, self.head_dim).transpose(1, 2)

    scale = math.sqrt(self.head_dim)
    attn = (q @ k.transpose(-2, -1)) / scale
    attn = attn.masked_fill(self.causal_mask[:T, :T], float("-inf"))
    attn = self.dropout(F.softmax(attn, dim=-1))
    out = (attn @ v).transpose(1, 2).contiguous().view(B, T, C)

    return self.proj(out)


class TransformerBlock(nn.Module):

  def __init__(self, d_model, n_heads, max_len, dropout):

    super().__init__()

    self.attn = CausalSelfAttention(d_model, n_heads, max_len, dropout)
    self.ff = nn.Sequential(
        nn.Linear(d_model, 4 * d_model),
        nn.GELU(),
        nn.Linear(4 * d_model, d_model),
        nn.Dropout(dropout),
    )
    self.ln1 = nn.LayerNorm(d_model)
    self.ln2 = nn.LayerNorm(d_model)

  def forward(self, x):

        x = x + self.attn(self.ln1(x))
        x = x + self.ff(self.ln2(x))

        return x


class SessionTransformer(nn.Module):

  def __init__(self, vocab_size, d_model=128, n_heads=4, n_layers=4, max_len=64, dropout=0.1):

    super().__init__()

    self.max_len = max_len
    self.tok_emb = nn.Embedding(vocab_size, d_model, padding_idx=0)
    self.pos_emb = nn.Embedding(max_len, d_model)
    self.drop = nn.Dropout(dropout)
    self.blocks = nn.ModuleList(
        [TransformerBlock(d_model, n_heads, max_len, dropout) for _ in range(n_layers)]
    )
    self.ln_f = nn.LayerNorm(d_model)
    self.head = nn.Linear(d_model, vocab_size, bias=False)
    self._init_weights()

  def _init_weights(self):

    for module in self.modules():
      if isinstance(module, nn.Linear):
        nn.init.normal_(module.weight, std=0.02)
      elif isinstance(module, nn.Embedding):
        nn.init.normal_(module.weight, std=0.02)

  def forward(self, idx):

    B, T = idx.shape
    assert T <= self.max_len, f"Sequence length {T} exceeds max_len {self.max_len}"
    pos = torch.arange(T, device=idx.device).unsqueeze(0)
    x = self.drop(self.tok_emb(idx) + self.pos_emb(pos))
    for block in self.blocks:
        x = block(x)
    x = self.ln_f(x)
    return self.head(x)

  @torch.no_grad()
  def generate(self, context, vocab, inv_vocab, state_machine=None, max_new_tokens=30, temperature=1.0, top_k=10):

    self.eval()
    device = next(self.parameters()).device
    pad = vocab.get("<PAD>", 0)
    eos = vocab.get("<EOS>", -1)

    idx = torch.tensor([[vocab.get(t, vocab["<UNK>"]) for t in context]], dtype=torch.long, device=device)

    # Walk context tokens to derive current SM state before generating
    sm_state = None
    if state_machine is not None:
      sm_state = S_IDLE
      for ctx_tok in context:
        # Skip special/cell tokens — only protocol messages advance the SM
        if ctx_tok.startswith("CELL_") or ctx_tok in ("<BOS>", "<EOS>", "<PAD>", "<UNK>"):
            continue
        msg = ctx_tok.split("|")[0]
        sm_state = next_state(sm_state, msg)

    generated = list(context)
    for _ in range(max_new_tokens):
      idx_cond = idx[:, -self.max_len:]
      logits = self(idx_cond)[:, -1, :]  # (1, vocab_size)

      # Apply state machine mask if provided
      if state_machine is not None and sm_state is not None:
        valid_msgs = valid_next_messages(sm_state)
        # Always allow EOS so generation can terminate cleanly
        mask = torch.full((logits.shape[-1],), float("-inf"), device=device)
        mask[eos] = 0.0
        for msg in valid_msgs:
          for tok, idx_val in vocab.items():
            # Match by substring (tokens have |DIR|T{n} suffix)
            if msg in tok or tok.split("|")[0] in msg:
              mask[idx_val] = 0.0
        logits = logits + mask

      logits = logits / temperature
      if top_k > 0:
        topk_vals, _ = torch.topk(logits, min(top_k, logits.size(-1)))
        logits[logits < topk_vals[:, -1:]] = float("-inf")

      probs = F.softmax(logits, dim=-1)
      next_idx = torch.multinomial(probs, 1)
      next_tok = inv_vocab.get(next_idx.item(), "<UNK>")

      generated.append(next_tok)
      idx = torch.cat([idx, next_idx], dim=1)

      if state_machine is not None and sm_state is not None:
        msg = next_tok.split("|")[0]
        sm_state = next_state(sm_state, msg)

      if next_idx.item() == eos:
        break

    return generated


def count_parameters(model):

  return sum(p.numel() for p in model.parameters() if p.requires_grad)
