# -*- coding: utf-8 -*-
"""V27 orchestrator v1: w0-sweep scans with a selectable scan script and word_0.

Usage:
  python sir88_stm32_V27_orchestrator_v1.py [scan_script.py] --w0-start 0x133 --w0-stop 0x13F --w0-step 0x1 [--repeats K] [--word0 0x1AA]

Differences from V26 orchestrator v2:
- new --word0 parameter (hex/dec, 0..511, default 0): forwarded to the scan
  script as --word0 (the script writes it to the firmware's word_0 once at
  scan start). word_0=0 keeps the legacy 3-word mode.
- SUMMARY reports carry the word_0 column from the per-run
  <pass_name>_ANSWERS_XOR256.tsv (the column is present in both modes of the
  V27 scanner), plus word_0 is recorded in this orchestrator's RUN_CONFIG.json
  and RUNS.tsv.

Everything else is inherited from orchestrator v2: the scan script name may be
given as the first positional argument (default:
sir88_stm32_V27_three_scan3_v13_xor_1.py, resolved next to this orchestrator,
then in --root); the w0 range is inclusive (last value <= stop); a failed run
is recorded as FAILED and the sweep continues; TX/RX BRR stay at the scan
script's default of 64; run folders are detected by before/after directory
snapshots; reports are read via each run's RUN_CONFIG.json (pass_name), so no
script-specific report names are assumed.

Output folder: orchestratorV27_<script>_word0-<hex>_w0-<start>..<stop>-<step>_r<repeats>_<timestamp>
  SUMMARY_XOR256.tsv      - all answers with answer-word XOR == 256 (any length)
  SUMMARY_XOR256_LEN3.tsv - same, only rx_count_full >= 3
  RUNS.tsv                - w0, repeat, status OK/FAILED, run folder, counters
  RUN_CONFIG.json         - sweep parameters and run registry
"""
import argparse
import csv
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys

DEFAULT_SCAN_SCRIPT = 'sir88_stm32_V27_three_scan3_v13_xor_1.py'

def parse_hex(text, what):
    try: value = int(text, 0)
    except ValueError: raise argparse.ArgumentTypeError(f'Invalid {what} value: {text!r}')
    if not 0 <= value <= 511: raise argparse.ArgumentTypeError(f'{what} must be a 9-bit word (0..511), got {value}')
    return value

def resolve_scan_script(name, root):
    p = Path(name)
    if p.is_file(): return p.resolve()
    for base in (Path(__file__).resolve().parent, root):
        cand = base / name
        if cand.is_file(): return cand.resolve()
    return None

def run_one(scan_path, root, w0, word0):
    """Launch one scan; return (returncode, new run folder or None, run stamp)."""
    before = {p.name for p in root.iterdir() if p.is_dir()}
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    proc = subprocess.run([sys.executable, str(scan_path), '--root', str(root),
                           '--w0', f'0x{w0:03X}', '--word0', f'0x{word0:03X}'])
    new = sorted(p for p in root.iterdir() if p.is_dir() and p.name not in before)
    folder = new[-1] if new else None
    return proc.returncode, folder, stamp

def read_run_config(folder):
    if folder is None: return None
    cfg = folder / 'RUN_CONFIG.json'
    if not cfg.is_file(): return None
    try: return json.loads(cfg.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError): return None

def collect_report(folder, pass_name, suffix):
    if folder is None or not pass_name: return None
    report = folder / f'{pass_name}_{suffix}.tsv'
    if not report.is_file(): return None
    with report.open(newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f, delimiter='\t'))

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('scan_script', nargs='?', default=DEFAULT_SCAN_SCRIPT,
        help=f'scan script to run (default: {DEFAULT_SCAN_SCRIPT})')
    p.add_argument('--root', type=Path, default=Path.cwd())
    p.add_argument('--w0-start', type=lambda s: parse_hex(s, '--w0-start'), required=True)
    p.add_argument('--w0-stop',  type=lambda s: parse_hex(s, '--w0-stop'),  required=True)
    p.add_argument('--w0-step',  type=lambda s: parse_hex(s, '--w0-step'),  required=True)
    p.add_argument('--repeats',  type=int, default=1)
    p.add_argument('--word0',    type=lambda s: parse_hex(s, '--word0'), default=0,
        help='V27 leading word written to firmware word_0 by the scan script; 0 = 3-word mode (default: 0)')
    a = p.parse_args()
    if a.w0_step < 1: p.error('--w0-step must be >= 1')
    if a.w0_stop < a.w0_start: p.error('--w0-stop must be >= --w0-start')
    if a.repeats < 1: p.error('--repeats must be >= 1')
    root = a.root.resolve()
    scan_path = resolve_scan_script(a.scan_script, root)
    if scan_path is None: p.error(f'Scan script not found: {a.scan_script} (looked next to orchestrator and in {root})')
    w0_list = list(range(a.w0_start, a.w0_stop + 1, a.w0_step))  # stop inclusive; last value <= stop
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    script_tag = scan_path.stem.replace('sir88_stm32_V27_', '').replace('sir88_stm32_V26_', '')
    word0_tag = f'word0-{a.word0:03X}_' if a.word0 else ''
    out = root / (f'orchestratorV27_{script_tag}_{word0_tag}w0-{a.w0_start:03X}..{a.w0_stop:03X}-{a.w0_step:02X}_r{a.repeats}_{stamp}')
    out.mkdir(parents=True, exist_ok=False)
    mode = '4-word' if a.word0 else '3-word'
    print(f'ORCHESTRATOR V27 v1 | script={scan_path.name} | word_0=0x{a.word0:03X} ({mode}) | '
          f'w0 {w0_list[0]:03X}..{w0_list[-1]:03X} step {a.w0_step:X} ({len(w0_list)} values) x{a.repeats} | {out}', flush=True)

    runs = []            # registry rows for RUNS.tsv
    all_rows = []        # SUMMARY_XOR256 rows
    for w0 in w0_list:
        for rep in range(1, a.repeats + 1):
            print(f'--- w0=0x{w0:03X} repeat {rep}/{a.repeats}', flush=True)
            rc, folder, run_stamp = run_one(scan_path, root, w0, a.word0)
            cfg = read_run_config(folder) if rc == 0 else None
            pass_name = (cfg or {}).get('pass_name')
            rows = collect_report(folder, pass_name, 'ANSWERS_XOR256') if rc == 0 else None
            mapped = collect_report(folder, pass_name, 'MAPPED') if rc == 0 else None
            status = 'OK' if rc == 0 and rows is not None else 'FAILED'
            n_answers = 'n/a'
            if mapped is not None:
                n_answers = sum(1 for r in mapped if r.get('rx_count_full', '0') not in ('0', ''))
            runs.append(dict(w0_hex=f'0x{w0:03X}', repeat=rep, status=status, returncode=rc,
                             run_folder=folder.name if folder else '', run_stamp=run_stamp,
                             answered=n_answers, xor256_count=len(rows) if rows else 0))
            if rows:
                for r in rows:
                    all_rows.append([f'0x{w0:03X}', rep, r['index2'], r['index1'], r['command_hex'],
                                     r.get('word_0', ''), r['tx_9n1'], r['rx_count_full'],
                                     r['first_rx_delay_us'], r['answer_words_hex'], r['answer_xor']])
            print(f'    -> {status} | XOR256 answers: {len(rows) if rows else 0} | {folder.name if folder else "no folder"}', flush=True)

    header = ['w0_hex', 'repeat', 'index2', 'index1', 'command_hex', 'word_0', 'tx_9n1',
              'rx_count_full', 'first_rx_delay_us', 'answer_words_hex', 'answer_xor']
    with (out / 'SUMMARY_XOR256.tsv').open('w', newline='', encoding='utf-8') as f:
        w = csv.writer(f, delimiter='\t', lineterminator='\n')
        w.writerow(header); w.writerows(all_rows); f.flush()
    len3 = [r for r in all_rows if int(r[7] or 0) >= 3]
    with (out / 'SUMMARY_XOR256_LEN3.tsv').open('w', newline='', encoding='utf-8') as f:
        w = csv.writer(f, delimiter='\t', lineterminator='\n')
        w.writerow(header); w.writerows(len3); f.flush()
    with (out / 'RUNS.tsv').open('w', newline='', encoding='utf-8') as f:
        w = csv.writer(f, delimiter='\t', lineterminator='\n')
        w.writerow(['w0_hex', 'repeat', 'status', 'returncode', 'run_stamp', 'run_folder', 'answered', 'xor256_count'])
        for r in runs:
            w.writerow([r['w0_hex'], r['repeat'], r['status'], r['returncode'], r['run_stamp'],
                        r['run_folder'], r['answered'], r['xor256_count']])
        f.flush()
    (out / 'RUN_CONFIG.json').write_text(json.dumps(dict(
        script=Path(__file__).name, scan_script=scan_path.name,
        word_0=a.word0, word_0_hex=f'0x{a.word0:03X}', command_words=(4 if a.word0 else 3),
        w0_start=f'0x{a.w0_start:03X}', w0_stop=f'0x{a.w0_stop:03X}', w0_step=f'0x{a.w0_step:02X}',
        repeats=a.repeats, w0_list=[f'0x{x:03X}' for x in w0_list],
        runs_total=len(runs), runs_ok=sum(1 for r in runs if r['status'] == 'OK'),
        runs_failed=sum(1 for r in runs if r['status'] != 'OK'),
        xor256_total=len(all_rows), xor256_len3_total=len(len3),
        runs=runs, started=stamp), indent=2))
    failed = [r for r in runs if r['status'] != 'OK']
    print(f'DONE | runs OK: {len(runs)-len(failed)}/{len(runs)} | XOR256 answers: {len(all_rows)} '
          f'(len>=3: {len(len3)})' + (f' | FAILED: {len(failed)}' if failed else ''), flush=True)

if __name__ == '__main__':
    try: main()
    except KeyboardInterrupt:
        print('Stopped; partial summary files kept as-is.', file=sys.stderr); sys.exit(130)
    except Exception as e:
        print(f'ERROR: {e}', file=sys.stderr); sys.exit(1)
