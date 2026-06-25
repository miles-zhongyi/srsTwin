# How to Run the 3-UE Demo

## 1. Start the stack (from scratch)

```bash
cd integration
docker compose -f docker-compose.4g.yml -f docker-compose.3ue.yml up -d --build
```

`--build` is only needed the first time or after editing `Dockerfile.enb`/`Dockerfile.ue`
or anything under `srsRAN_4G/` they `COPY`/`ADD` — Docker skips it via cache
otherwise. Startup order is handled automatically (`srsepc` has a
healthcheck; `srsenb`/`srsenb2`/`srsenb3` wait for it).

Wait ~25-30s, then confirm all 3 pairs attached:

```bash
docker logs srstwin_ue4g  --tail 3   # expect "Network attach successful. IP: 172.16.0.2"
docker logs srstwin_ue4g2 --tail 3   # IP: 172.16.0.x (auto-assigned, dynamic)
docker logs srstwin_ue4g3 --tail 3
```

If a pair doesn't attach, check `docker logs srstwin_epc --tail 20` first —
the most common cause is srsEPC not having loaded a subscriber yet (see
"Gotchas" below).

## 2. Just want it running for a live demo?

That's it — the 3 cells are independent and will sit attached indefinitely
(no idle timeout disconnects them automatically; the eNB only releases
*that* attach after ~30s of inactivity, then the UE itself doesn't
necessarily reattach on its own — see `run_cycles.py` if you want to force
fresh attach cycles on demand).

To show distinct UEs live: `docker logs srstwin_enb --tail 20` /
`srstwin_enb2` / `srstwin_enb3` each show their own independent RACH +
RNTI assignment + attach sequence — that's the "3 distinct UEs" evidence.

## 3. Reproduce the measured results (histogram + stats)

This part **does** cycle the containers (force-recreate, repeatedly) to
generate enough samples — don't run it if you need the stack to stay up
for something else at the same time.

```bash
cd integration

# Baseline: pair 1 alone (stop the other two first for a clean measurement)
docker compose -f docker-compose.4g.yml -f docker-compose.3ue.yml stop srsenb2 srsue4g2 srsenb3 srsue4g3
python3 demo3ue/run_cycles.py --pairs 1 --cycles 5 --wait-release --out demo3ue/logs_1pair

# Concurrent: bring pairs 2/3 back, then cycle all 3 together
docker compose -f docker-compose.4g.yml -f docker-compose.3ue.yml start srsenb2 srsue4g2 srsenb3 srsue4g3
# wait ~30s for them to reattach, then:
python3 demo3ue/run_cycles.py --pairs 1,2,3 --cycles 5 --wait-release --out demo3ue/logs_3pair

python3 demo3ue/analyze.py            # prints + saves results.json
python3 demo3ue/plot_histograms.py    # writes histograms.png
```

Each cycle takes ~35-70s (attach + wait for the eNB's 30s inactivity
release), so 5 cycles × 2 scenarios is roughly 8-12 minutes total.

## 4. Tear down

```bash
docker compose -f docker-compose.4g.yml -f docker-compose.3ue.yml down
```

Add `-v` only if you want to wipe the log volumes too (loses any logs you
haven't pulled out yet).

## Gotchas specific to this 3-pair setup

- **srsEPC loads `subscribers.csv` once at container startup.** If you ever
  add more subscribers to that file, recreate `srsepc`
  (`docker compose ... up -d --force-recreate srsepc`), not just the UEs —
  otherwise the new IMSIs get "Attach failed" with no useful error.
- **Never recreate a UE alone against an already-running eNB.** It causes
  a RACH retry storm that doesn't settle (confirmed empirically — see
  `RESULTS.md`). Always recreate the eNB and UE of a pair together:
  `docker compose ... up -d --force-recreate srsenbN srsue4gN`.
- All 3 pairs share one CPU/host. If something else CPU-heavy is also
  running (check `docker stats`), expect the delay numbers in `RESULTS.md`
  to shift — that's not a bug, it's the same contention effect the demo is
  measuring, just with a different baseline.
