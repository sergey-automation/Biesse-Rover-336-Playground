# STM32 Three-Word Command Scanner (V26) with XOR Filtering and Orchestrator

A set of scripts for investigating device responses on an RS-485 bus through
the STM32 debug interface (OpenOCD + ST-Link). Each command consists of 27 bits:
three 9-bit words `(w0, w1, w2)` packed into a 32-bit word `(w0<<18)|(w1<<9)|w2`.
The scripts generate commands, send them to the firmware in batches, collect
responses, and generate reports that uniquely associate each response with the
command that triggered it.

## Package contents

| File | Purpose |
|---|---|
| `sir88_stm32_V26_three_scan3_v13_xor_2.py` | Three-word command scanner with `PASS_SUMMARY.tsv` and a `stats` field in `RUN_CONFIG.json`. |
| `sir88_stm32_V26_orchestrator_v2.py` | Scan orchestrator; independent of report filenames. |

The scanning scripts require the helper module `sir88_scan_v26.py`
(shared V26 protocol code: OpenOCD, batch packing, and block parsing),
as well as OpenOCD and Python 3.8+.

## Key concepts

### Command XOR filter

A command `(w0, w1, w2)` is transmitted only if `w0 ^ w1 ^ w2 == 256`
(decimal). For a fixed `w0`, there are exactly 512 commands: for each `w1`,
there is a unique `w2 = (w0 ^ 256) ^ w1`. Filtered-out commands are not
transmitted, so no time is spent on them.

### Two indices

- **index1** — command position in the full 512×512 grid (0..262143): `index1 = (w1<<9)|w2`.
- **index2** — sequential index among transmitted commands (0..511): this is the index used by the firmware to identify responses.

The `INDEX2INDEX1.tsv` file stores the `index2 → index1` mapping, together with
the hexadecimal command and its individual words, allowing each response to be
uniquely attributed to its command.

## Usage

### Single run (v13_xor_2)

```
python sir88_stm32_V26_three_scan3_v13_xor_2.py --w0 0x133
```

Parameters:
- `--w0` — first command word, hexadecimal (`0x133`) or decimal (`307`); 0..511; default: `0x13F`.
- `--tx-brr`, `--rx-brr` — UART baud rate divisors (default: 64).
- `--root` — firmware project root (default from the `sir88_scan_v26` module).
- `--map` — path to the firmware `.map` file (usually detected automatically).
- `--self-test` — standalone logic test without hardware.

Output: a new directory alongside the project:
`sir88_stm32_V26_three_scan3_v13_xor_2_w0-133_<YYYYMMDD_HHMMSS_us>/`

### Scanning a range of w0 values (orchestrator v2)

```
python sir88_stm32_V26_orchestrator_v2.py sir88_stm32_V26_three_scan3_v13_xor_2.py --w0-start 0x133 --w0-stop 0x13F --w0-step 0x1 --repeats 1
```

- The first positional argument is the scanner script name (if omitted, `sir88_stm32_V26_three_scan3_v13_xor_2.py` is used).
- `--w0-start`, `--w0-stop`, `--w0-step` — range of hexadecimal w0 values; both bounds are inclusive (the last value does not exceed stop).
- `--repeats K` — number of consecutive repetitions for each w0 (default: 1).
- Failure of an individual run does not stop the scan; `FAILED` is recorded in the run log.
- TX/RX BRR parameters are not passed; the default values (64) are always used.

Output directory:
`orchestrator2_<script>_w0-133..13F-01_r1_<timestamp>/`

## Scanner output (v13_xor_2)

| File | Contents |
|---|---|
| `RUN_CONFIG.json` | Run metadata: script name, w0 (dec/hex), filter rule, BRR, grid size, start time, and the `stats` field containing final statistics (blocks, answered, xor256_answers, n, rx, rx0, words, fe, ne, ore, pe, long, approx). |
| `INDEX2INDEX1.tsv` | Mapping of all transmitted commands: `index2, index1, command_hex, w0, w1, w2` (512 rows). |
| `W0_<HEX>_MAPPED.tsv` | All transmitted commands with firmware responses: command words, `rx_count_full`, first response delay (µs), stored response words, and flags. |
| `W0_<HEX>_ANSWERS_XOR256.tsv` | **Only commands that received a response whose words XOR to 256**: index2, index1, hexadecimal command, response length, delay, response words, their XOR, and flags. Main report. |
| `PASS_SUMMARY.tsv` | One row of scan pass statistics, including the `answered` and `xor256_answers` columns. |
| `BRR_SETTINGS.tsv` | Confirmed baud rate settings. |
| `W0_<HEX>/part_N/` | Intermediate scan artifacts: `B_FORWARD.bin/.blk/.tsv.gz` (raw responses), `INDEX_MAPPING.json`, and temporary upload files. |

## Orchestrator output (v2)

| File | Contents |
|---|---|
| `SUMMARY_XOR256.tsv` | Combined report for all w0 values and repetitions: all responses whose words XOR to 256 (any length). Columns: `w0_hex, repeat, index2, index1, command_hex, tx_9n1, rx_count_full, first_rx_delay_us, answer_words_hex, answer_xor`. |
| `SUMMARY_XOR256_LEN3.tsv` | Same report, restricted to responses of at least 3 words (`rx_count_full >= 3`). |
| `RUNS.tsv` | Run log: `w0_hex, repeat, status (OK/FAILED), returncode, run_stamp, run_folder, answered, xor256_count`. |
| `RUN_CONFIG.json` | Scan parameters (range, step, repeats, script), counters, and the same run log. |

## Reading the response report

A row in `*_ANSWERS_XOR256.tsv` / `SUMMARY_XOR256.tsv` means that during a run
with the first word `w0_hex`, the command with sequential index `index2`
(position `index1` in the full grid, words `tx_9n1`, hexadecimal value
`command_hex`) received a response consisting of `rx_count_full` words
`answer_words_hex` after `first_rx_delay_us` µs; the XOR of the response words
is `answer_xor` (=256).

Example: `0x133  1  144  73891  0x4cd20a3  133 090 0A3  3  11.792  133 003 030  256`
— with first word 0x133, the 145th transmitted command `(0x133, 0x090, 0x0A3)`
received a three-word response `133 003 030` after 11.8 µs.

## Notes

- Interrupting the script (Ctrl+C) does not automatically reset the device;
  partial results remain on disk as they are.
