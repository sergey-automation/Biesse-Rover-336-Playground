# SIR88 V27: Four-Word Command Scanner with word_0 Prefix

Firmware V27 (`main.c`) extends V26 with an optional leading command word:
a public volatile variable `word_0` in the STM32 memory map.

- `word_0 = 0` (default after reset): commands are sent as **3 words**,
  fully V26-compatible.
- `word_0 > 0`: commands on the bus become **4 words** — `word_0` first,
  then the 27-bit packed triple `(w0, w1, w2)`.

The host scripts below program `word_0` once at scan start over SWD and run
an XOR-filtered command sweep with response capture.

Hardware: STM32F103C8 + MAX485 (5 V), 9N1 UART, OpenOCD + ST-Link.
Requires: Python 3.8+, OpenOCD, helper module `sir88_scan_v26.py`.

## Files

| File | Purpose |
|---|---|
| `main.c` | Firmware V27 source (V26 kept as `main.c.arj`). |
| `sir88_scan_v26.py` | Shared protocol module (OpenOCD, batching, blocks). |
| `sir88_stm32_V27_three_scan3_v13_xor_1.py` | Scanner: one w0 sweep with XOR filter, optional `--word0`. |
| `sir88_stm32_V27_orchestrator_v1.py` | Orchestrator: w0 range sweep, forwards `--word0`, merges reports. |

## Command model

- A command is three 9-bit words `(w0, w1, w2)` packed as `(w0<<18)|(w1<<9)|w2`;
  with `word_0 > 0` the firmware prepends `word_0` on the bus (4 words total).
- A command is **sent only if its words XOR to 256**:
  `w0 ^ w1 ^ w2 == 256` in 3-word mode,
  `word_0 ^ w0 ^ w1 ^ w2 == 256` in 4-word mode.
  Exactly 512 of the 262144 grid commands pass the filter.
- Report indices: **index2** = order among sent commands (what the firmware
  sees), **index1** = position in the full 512×512 grid. `INDEX2INDEX1.tsv`
  maps one to the other, so every response is attributed to its command.

## Usage

Single run:

```
python sir88_stm32_V27_three_scan3_v13_xor_1.py --w0 0x133 --word0 0x1AA
```

- `--w0` — first command word, hex or decimal, 0..511 (default `0x13F`).
- `--word0` — leading word written to firmware `word_0` at start;
  `0` = 3-word mode (default).
- `--self-test` — offline logic check without hardware.

Range sweep:

```
python sir88_stm32_V27_orchestrator_v1.py --w0-start 0x000 --w0-stop 0x020 --w0-step 0x1 --word0 0x133 --repeats 1
```

- Optional first positional argument: scanner script name
  (default `sir88_stm32_V27_three_scan3_v13_xor_1.py`).
- `--w0-start/--w0-stop/--w0-step` — hex w0 range, stop inclusive.
- `--repeats K` — repeat each w0 K times (default 1).
- `--word0` — forwarded to the scanner (default 0).
- A failed run is logged as `FAILED`; the sweep continues.

## Output: scanner folder

`sir88_stm32_V27_three_scan3_v13_xor_1_[word0-1AA_]w0-133_<timestamp>/`

| File | Contents |
|---|---|
| `RUN_CONFIG.json` | Run metadata: w0, `word_0`, `command_words` (3/4), filter rule, BRR, final pass statistics (`stats`). |
| `INDEX2INDEX1.tsv` | All 512 sent commands: `index2, index1, command_hex, word_0, w0, w1, w2`. |
| `W0_<HEX>_MAPPED.tsv` | Every sent command with its firmware response (count, delay µs, response words, flags). |
| `W0_<HEX>_ANSWERS_XOR256.tsv` | Only commands whose **response** words XOR to 256. Main report. |
| `PASS_SUMMARY.tsv` | One row of pass statistics. |
| `W0_<HEX>/part_N/` | Raw stage artifacts (`B_FORWARD.bin/.tsv.gz`, etc.). |

## Output: orchestrator folder

`orchestratorV27_<script>_[word0-133_]w0-000..020-01_r1_<timestamp>/`

| File | Contents |
|---|---|
| `SUMMARY_XOR256.tsv` | All responses whose words XOR to 256, across the whole sweep. Columns: `w0_hex, repeat, index2, index1, command_hex, word_0, tx_9n1, rx_count_full, first_rx_delay_us, answer_words_hex, answer_xor`. |
| `SUMMARY_XOR256_LEN3.tsv` | Same, restricted to responses of at least 3 words. |
| `RUNS.tsv` | Run registry: w0, repeat, status (OK/FAILED), run folder, counters. |
| `RUN_CONFIG.json` | Sweep parameters (range, repeats, `word_0`) and the run registry. |

## Notes

- The scanner always writes `word_0` at start (including 0), so every run
  begins from a known firmware state; the value is not changed mid-pass.
- On a V26 firmware/.map the V27 scanner stops with a clear
  "no address word_0 in .map" error; use V26 scripts for V26 firmware.
- Interrupting a run (Ctrl+C) performs no automatic device reset; partial
  results stay on disk.
- Bench-test with actuators disconnected: unknown commands may switch relays.
