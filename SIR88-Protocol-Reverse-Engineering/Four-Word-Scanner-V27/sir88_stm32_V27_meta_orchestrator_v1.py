# -*- coding: utf-8 -*-
"""V27 meta-orchestrator v1: sweep of word_0 values over orchestrator v1.

For each word_0 in [--word0-start .. --word0-stop] step --word0-step the
existing orchestrator (default: sir88_stm32_V27_orchestrator_v1.py) is launched
as a subprocess with the user-supplied argument string plus '--word0 <value>'.
New orchestrator output folders are detected by before/after directory
snapshots (same approach as orchestrator v1).

Statistics per word_0 cover both VALID and INVALID answers:
  valid   = rows of the per-run <pass>_ANSWERS_XOR256.tsv (answer XOR == 256)
  invalid = answered rows of the per-run <pass>_MAPPED.tsv whose saved answer
            has >= 2 words and does NOT pass the XOR==256 filter
Answers shorter than 2 words are ignored for the invalid category.
UART error counters (fe/ne/ore) are taken from each run's RUN_CONFIG.json.

Output folder:
  metaV27_<orch>_word0-<start>..<stop>-<step>_<timestamp>/
    META_ANSWERS_VALID.tsv    - one row per valid answer (all runs)
    META_ANSWERS_INVALID.tsv  - one row per invalid answer (>=2 words)
    META_WORD0_SUMMARY.tsv    - one row per word_0 (totals, devices, top answers)
    META_RUNS.tsv             - run registry incl. resume status
    META_CONFIG.json          - meta sweep parameters + registry

Resume: re-run the same command pointing --out at an existing meta folder:
word_0 values already registered in META_RUNS.tsv are skipped.
Two phases: (1) all word_0 sweeps run back-to-back with no processing delay
(only the META_RUNS.tsv registry checkpoint is written after each, for
resume); (2) statistics and all full reports are built in a single pass at
the end, when every orchestrator folder is completely written.

Usage:
  python sir88_stm32_V27_meta_orchestrator_v1.py \
      --word0-start 0x130 --word0-stop 0x140 --word0-step 0x1 \
      --orch-args "--w0-start 0x02 --w0-stop 0x08 --w0-step 0x2 --repeats 1"
"""
import argparse
import csv
from datetime import datetime
import json
from pathlib import Path
import shlex
import subprocess
import sys

DEFAULT_ORCHESTRATOR = 'sir88_stm32_V27_orchestrator_v1.py'
XOR_TARGET = 256
MIN_INVALID_WORDS = 2

VALID_HEADER = ['word_0', 'w0_hex', 'repeat', 'index2', 'index1', 'command_hex',
                'tx_9n1', 'rx_count_full', 'first_rx_delay_us',
                'answer_words_hex', 'answer_xor']
INVALID_HEADER = ['word_0', 'w0_hex', 'index2', 'index1', 'command_hex',
                  'tx_9n1', 'rx_count_full', 'first_rx_delay_us',
                  'answer_words_hex', 'answer_words_count', 'answer_xor',
                  'answer_flags_hex']
SUMMARY_HEADER = ['word_0', 'runs', 'runs_ok', 'answered_total', 'valid_total',
                  'invalid_total', 'fe_total', 'ne_total', 'ore_total',
                  'devices', 'valid_top', 'invalid_top', 'orch_folder', 'status']
RUNS_HEADER = ['word_0', 'status', 'returncode', 'orch_folder', 'answered_total',
               'valid_total', 'invalid_total', 'fe_total', 'ne_total',
               'ore_total', 'stamp']


def parse_word(text, what):
    try:
        value = int(text, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(f'Invalid {what} value: {text!r}')
    if not 0 <= value <= 511:
        raise argparse.ArgumentTypeError(f'{what} must be a 9-bit word (0..511), got {value}')
    return value


def resolve_orchestrator(name, root):
    p = Path(name)
    if p.is_file():
        return p.resolve()
    for base in (Path(__file__).resolve().parent, root):
        cand = base / name
        if cand.is_file():
            return cand.resolve()
    return None


def xor_words(words):
    x = 0
    for w in words:
        x ^= w
    return x


def read_csv(path):
    if not path.is_file():
        return None
    with path.open(newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f, delimiter='\t'))


def collect_orch_folder(folder, word0_hex):
    """Harvest one orchestrator output folder; return (valid_rows, invalid_rows,
    totals dict, devices counter, valid_counter, invalid_counter)."""
    import collections
    valid, invalid = [], []
    totals = dict(answered=0, fe=0, ne=0, ore=0, runs=0, runs_ok=0)
    devices = collections.Counter()
    valid_cnt = collections.Counter()
    invalid_cnt = collections.Counter()

    orch_cfg_path = folder / 'RUN_CONFIG.json'
    if not orch_cfg_path.is_file():
        return valid, invalid, totals, devices, valid_cnt, invalid_cnt
    try:
        orch_cfg = json.loads(orch_cfg_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return valid, invalid, totals, devices, valid_cnt, invalid_cnt

    root = folder.parent
    for run in orch_cfg.get('runs', []):
        totals['runs'] += 1
        if run.get('status') != 'OK':
            continue
        run_folder = root / run.get('run_folder', '')
        cfg_path = run_folder / 'RUN_CONFIG.json'
        if not cfg_path.is_file():
            continue
        try:
            cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        totals['runs_ok'] += 1
        pass_name = cfg.get('pass_name', '')
        w0_hex = cfg.get('w0_hex', '')
        repeat = run.get('repeat', '')
        stats = (cfg.get('stats') or {}).get(pass_name, {})
        totals['fe'] += int(stats.get('fe', 0) or 0)
        totals['ne'] += int(stats.get('ne', 0) or 0)
        totals['ore'] += int(stats.get('ore', 0) or 0)

        # Valid answers (already XOR-filtered by the scan script).
        for r in read_csv(run_folder / f'{pass_name}_ANSWERS_XOR256.tsv') or []:
            ans = r.get('answer_words_hex', '')
            valid.append([word0_hex, w0_hex, repeat, r.get('index2', ''),
                          r.get('index1', ''), r.get('command_hex', ''),
                          r.get('tx_9n1', ''), r.get('rx_count_full', ''),
                          r.get('first_rx_delay_us', ''), ans,
                          r.get('answer_xor', '')])
            valid_cnt[ans] += 1
            if ans:
                devices[ans.split()[0]] += 1

        # Invalid answers: mapped rows with >=3 saved words, not XOR==256.
        for r in read_csv(run_folder / f'{pass_name}_MAPPED.tsv') or []:
            if r.get('rx_count_full', '0') in ('0', ''):
                continue
            totals['answered'] += 1
            aw = r.get('saved_9bit_words', '')
            words = aw.split() if aw else []
            if len(words) < MIN_INVALID_WORDS:
                continue
            try:
                vals = [int(x, 16) for x in words]
            except ValueError:
                continue
            ax = xor_words(vals)
            if ax == XOR_TARGET:
                continue  # already in the valid table
            invalid.append([word0_hex, w0_hex, r.get('index2', ''),
                            r.get('index1', ''), r.get('command_hex', ''),
                            r.get('tx_9n1', ''), r.get('rx_count_full', ''),
                            r.get('first_rx_delay_us', ''), aw,
                            str(len(words)), str(ax),
                            r.get('saved_flags_hex', '')])
            invalid_cnt[aw] += 1
    return valid, invalid, totals, devices, valid_cnt, invalid_cnt


def write_reports(out, summary_rows, valid_rows, invalid_rows, runs_rows):
    def dump(name, header, rows):
        with (out / name).open('w', newline='', encoding='utf-8') as f:
            w = csv.writer(f, delimiter='\t', lineterminator='\n')
            w.writerow(header)
            w.writerows(rows)
            f.flush()
    dump('META_ANSWERS_VALID.tsv', VALID_HEADER, valid_rows)
    dump('META_ANSWERS_INVALID.tsv', INVALID_HEADER, invalid_rows)
    dump('META_WORD0_SUMMARY.tsv', SUMMARY_HEADER, summary_rows)
    dump('META_RUNS.tsv', RUNS_HEADER, runs_rows)


def load_done_word0s(out):
    """Resume support: word_0 values already processed (from META_RUNS.tsv)."""
    path = out / 'META_RUNS.tsv'
    done = set()
    if path.is_file():
        for r in read_csv(path) or []:
            try:
                done.add(int(r['word_0'], 0))
            except (ValueError, KeyError):
                pass
    return done


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--orchestrator', default=DEFAULT_ORCHESTRATOR,
                   help=f'orchestrator script (default: {DEFAULT_ORCHESTRATOR})')
    p.add_argument('--orch-args', required=True,
                   help='argument string passed to the orchestrator, e.g. '
                        '"--w0-start 0x02 --w0-stop 0x08 --w0-step 0x2 --repeats 1"')
    p.add_argument('--word0-start', type=lambda s: parse_word(s, '--word0-start'), required=True)
    p.add_argument('--word0-stop', type=lambda s: parse_word(s, '--word0-stop'), required=True)
    p.add_argument('--word0-step', type=lambda s: parse_word(s, '--word0-step'), default=1)
    p.add_argument('--root', type=Path, default=Path.cwd(),
                   help='workspace root where orchestrator creates its folders')
    p.add_argument('--out', type=Path,
                   help='meta output folder (default: new timestamped); pass an existing '
                        'folder to resume')
    a = p.parse_args()
    if a.word0_step < 1:
        p.error('--word0-step must be >= 1')
    if a.word0_stop < a.word0_start:
        p.error('--word0-stop must be >= --word0-start')

    root = a.root.resolve()
    orch_path = resolve_orchestrator(a.orchestrator, root)
    if orch_path is None:
        p.error(f'Orchestrator not found: {a.orchestrator}')

    orch_args = shlex.split(a.orch_args)
    if any(x.startswith('--word0') for x in orch_args):
        p.error('Do not put --word0 into --orch-args; it is set by the meta sweep')
    # Prevent a nested meta sweep and conflicting output roots.
    if any(x in ('--root', '--out') for x in orch_args):
        p.error('--root/--out must not appear in --orch-args (root is inherited)')

    word0_list = list(range(a.word0_start, a.word0_stop + 1, a.word0_step))
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    if a.out is not None:
        out = a.out.resolve()
        out.mkdir(parents=True, exist_ok=True)
    else:
        tag = orch_path.stem.replace('sir88_stm32_V27_', '').replace('_orchestrator_v1', '')
        out = root / (f'metaV27_{tag}_word0-{a.word0_start:03X}..{a.word0_stop:03X}'
                      f'-{a.word0_step:02X}_{stamp}')
        out.mkdir(parents=True, exist_ok=False)

    done = load_done_word0s(out)
    todo = [w for w in word0_list if w not in done]
    print(f'META-ORCHESTRATOR V27 v1 | orchestrator={orch_path.name} | '
          f'word_0 {a.word0_start:03X}..{a.word0_stop:03X} step {a.word0_step:X} '
          f'({len(word0_list)} values, {len(todo)} to do, {len(done)} resumed) | {out}',
          flush=True)
    print(f'orch-args: {a.orch_args}', flush=True)

    summary_rows, valid_rows, invalid_rows, runs_rows = [], [], [], []
    # Reload existing rows when resuming so reports stay complete.
    if done:
        for r in read_csv(out / 'META_WORD0_SUMMARY.tsv') or []:
            summary_rows.append([r.get(h, '') for h in SUMMARY_HEADER])
        for r in read_csv(out / 'META_ANSWERS_VALID.tsv') or []:
            valid_rows.append([r.get(h, '') for h in VALID_HEADER])
        for r in read_csv(out / 'META_ANSWERS_INVALID.tsv') or []:
            invalid_rows.append([r.get(h, '') for h in INVALID_HEADER])
        for r in read_csv(out / 'META_RUNS.tsv') or []:
            runs_rows.append([r.get(h, '') for h in RUNS_HEADER])

    # Phase 1: run all word_0 sweeps back-to-back, no waiting, no processing.
    # Only the run registry (RUNS) is updated after each word_0 for resume.
    pending = []  # (word_0, returncode, folder, stamp)
    for word0 in todo:
        w0h = f'0x{word0:03X}'
        print(f'=== word_0={w0h} ({len(pending)+1}/{len(todo)})', flush=True)
        before = {x.name for x in root.iterdir() if x.is_dir()}
        stamp_now = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        proc = subprocess.run(
            [sys.executable, str(orch_path), *orch_args, '--word0', w0h],
            cwd=str(root))
        new = sorted(x for x in root.iterdir()
                     if x.is_dir() and x.name not in before)
        # The orchestrator run creates its own folder (orchestratorV27_*) plus
        # one folder per scan run (sir88_*); the orchestrator folder is the
        # one whose RUN_CONFIG.json carries the runs registry.
        orch_new = [x for x in new if x.name.startswith('orchestrator')]
        folder = orch_new[-1] if orch_new else (new[-1] if new else None)
        status = 'OK' if (folder is not None and proc.returncode == 0) else 'FAILED'
        pending.append((word0, proc.returncode, folder, stamp_now))
        print(f'    -> {status} | {folder.name if folder else "no folder"}', flush=True)
        # Registry checkpoint (statistics are filled in phase 2).
        done.add(word0)
        (out / 'META_RUNS.tsv').write_text(
            '\t'.join(RUNS_HEADER) + '\n' +
            ''.join('\t'.join(map(str, r)) + '\n' for r in
                    [rr for rr in runs_rows] +
                    [[f'0x{w:03X}', 'OK' if (f is not None and rc == 0) else 'FAILED',
                      rc, f.name if f else '', '', '', '', '', '', '', st]
                     for w, rc, f, st in pending]), encoding='utf-8')

    # Phase 2: process all orchestrator folders (they are fully written by now).
    print('=== processing results', flush=True)
    for word0, rc, folder, stamp_now in pending:
        w0h = f'0x{word0:03X}'
        if folder is None or rc != 0:
            status = 'FAILED'
            v_rows, i_rows = [], []
            totals = dict(answered=0, fe=0, ne=0, ore=0, runs=0, runs_ok=0)
            devices, valid_cnt, invalid_cnt = {}, {}, {}
        else:
            status = 'OK'
            (v_rows, i_rows, totals, devices,
             valid_cnt, invalid_cnt) = collect_orch_folder(folder, w0h)

        valid_rows.extend(v_rows)
        invalid_rows.extend(i_rows)
        top = lambda c, n=5: '; '.join(f'[{k}] x{v}' for k, v in
                                       sorted(c.items(), key=lambda kv: -kv[1])[:n])
        summary_rows.append([w0h, totals['runs'], totals['runs_ok'],
                             totals['answered'], len(v_rows), len(i_rows),
                             totals['fe'], totals['ne'], totals['ore'],
                             '; '.join(f'0x{k} x{v}' for k, v in
                                       sorted(devices.items(), key=lambda kv: -kv[1])),
                             top(valid_cnt), top(invalid_cnt),
                             folder.name if folder else '', status])
        runs_rows.append([w0h, status, rc,
                          folder.name if folder else '', totals['answered'],
                          len(v_rows), len(i_rows), totals['fe'], totals['ne'],
                          totals['ore'], stamp_now])
        print(f'    word_0={w0h}: {status} | answered={totals["answered"]} '
              f'valid={len(v_rows)} invalid={len(i_rows)}', flush=True)

    # Final reports (single write at the end).
    write_reports(out, summary_rows, valid_rows, invalid_rows, runs_rows)
    (out / 'META_CONFIG.json').write_text(json.dumps(dict(
        script=Path(__file__).name, orchestrator=orch_path.name,
        orch_args=a.orch_args,
        word0_start=f'0x{a.word0_start:03X}', word0_stop=f'0x{a.word0_stop:03X}',
        word0_step=f'0x{a.word0_step:02X}', word0_list=[f'0x{x:03X}' for x in word0_list],
        done=sorted(f'0x{x:03X}' for x in done),
        valid_total=len(valid_rows), invalid_total=len(invalid_rows),
        started=stamp,
        updated=datetime.now().strftime('%Y%m%d_%H%M%S_%f')), indent=2))

    failed = [r for r in runs_rows if r[1] != 'OK']
    print(f'META DONE | word_0 OK: {len(runs_rows)-len(failed)}/{len(runs_rows)} | '
          f'valid answers: {len(valid_rows)} | invalid answers (>=2 words): '
          f'{len(invalid_rows)}' + (f' | FAILED: {len(failed)}' if failed else ''),
          flush=True)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Stopped; intermediate reports are up to date.', file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f'ERROR: {e}', file=sys.stderr)
        sys.exit(1)
