"""SIR88 three-word scan generator v1. Python 3.9+.
Only for an isolated bench module with no actuator loads connected.
Uses the existing sir88_v0.2.py executor without modifying it.
"""
import argparse
import json
import math
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

CONTROL = {'tx': [0x130, 0x0BA, 0x1BC, 0x136], 'expected': 3, 'xor': 0x100}


def hexword(s):
    try:
        n = int(s, 16)
    except ValueError:
        raise argparse.ArgumentTypeError('Expected hexadecimal word')
    if not 0 <= n <= 511:
        raise argparse.ArgumentTypeError('Word must be 000..1FF')
    return n


def batches(first_start, first_stop, second_start, second_stop, third_start, third_stop, fourth_start, fourth_stop, expected, xor):
    packet = []
    for first in range(first_start, first_stop + 1):
        for second in range(second_start, second_stop + 1):
            for third in range(third_start, third_stop + 1):
                for fourth in range(fourth_start, fourth_stop + 1):
                    packet.append(dict(tx=[first, second, third, fourth], expected=expected, xor=xor))
                    if (len(packet) + 1) % 10 == 0:
                        packet.append(dict(CONTROL, tx=CONTROL['tx'][:]))
                    if len(packet) == 512:
                        yield packet
                        packet = []
    if packet:
        yield packet


def save(path, obj):
    with path.open('x', encoding='utf-8') as f:
        json.dump(obj, f, indent=2)
        f.write('\n')


def selftest():
    p = list(batches(0x130, 0x130, 0, 511, 0, 511, 0, 511, 1, 256))
    assert [len(x) for x in p] == [512] * 291271 + [56]
    scan = []
    controls = 0
    for packet in p:
        for i, e in enumerate(packet, 1):
            if i % 10 == 0:
                assert e == CONTROL
                controls += 1
            else:
                assert e['expected'] == 1 and e['xor'] == 256
                scan.append(tuple(e['tx']))
    assert scan == [(0x130,b,t,f) for b in range(512) for t in range(512) for f in range(512)]
    assert controls == 14913080
    print(f'SELFTEST PASS: 134217728 unique scan commands, {controls} controls, {len(p)} packets')


def main():
    a = argparse.ArgumentParser(description=__doc__)
    a.add_argument('--self-test', action='store_true')
    a.add_argument('--generate-only', action='store_true', help='Create JSON packets without opening COM port')
    a.add_argument('--port', default='COM18')
    a.add_argument('--executor', type=Path, default=Path(__file__).resolve().with_name('sir88_v0.2.py'))
    a.add_argument('--output', type=Path, default=Path('sir88_scan_3_word'), help='Base report path; unique run directory created beside it')
    a.add_argument('--first-start', type=hexword, default=0x130)
    a.add_argument('--first-stop', type=hexword, default=0x130)
    a.add_argument('--second-start', type=hexword, default=0)
    a.add_argument('--second-stop', type=hexword, default=511)
    a.add_argument('--third', type=hexword, default=0x136)
    a.add_argument('--third-start', type=hexword, default=0x136)
    a.add_argument('--third-stop', type=hexword, default=0x136)
    a.add_argument('--fourth-start', type=hexword, default=0x136)
    a.add_argument('--fourth-stop', type=hexword, default=0x136)
    a.add_argument('--expected', type=int, choices=range(1,7), default=1)
    a.add_argument('--xor', type=hexword, default=256)
    a.add_argument('--auto', type=int, choices=[0,1], default=0)
    a.add_argument('--ret', type=int, choices=range(6), default=0)
    a.add_argument('--start-us', type=float, default=8800/120)
    a.add_argument('--gap-us', type=float, default=13.2)
    a.add_argument('--period-us', type=float, default=352)
    a.add_argument('--ret-us', type=float, default=8800/120)
    a.add_argument('--confirm-us', type=float, default=0)
    a.add_argument('--timeout', type=float, default=30)
    a.add_argument('--repeat', type=int, default=1)
    args = a.parse_args()
    if args.self_test:
        selftest(); return
    if args.first_start > args.first_stop or args.second_start > args.second_stop or args.third_start > args.third_stop or args.fourth_start > args.fourth_stop:
        a.error('Reversed scan range')
    vals = [args.start_us,args.gap_us,args.period_us,args.ret_us,args.confirm_us,args.timeout]
    if not all(math.isfinite(x) and x >= 0 for x in vals) or args.timeout <= 0 or args.repeat < 1:
        a.error('Invalid timing/repeat')
    t = [int(x*120+0.5) for x in vals[:5]]
    if not (all(1<=t[i]<=65535 for i in (0,1,3)) and 38720<=t[2]<=0xffffffff and 0<=t[4]<=65535):
        a.error('Timing outside executor limits')
    executor = args.executor.resolve()
    if not args.generate_only and not executor.is_file():
        a.error(f'Executor not found: {executor}')
    packets = list(batches(args.first_start,args.first_stop,args.second_start,args.second_stop,args.third_start,args.third_stop,args.fourth_start,args.fourth_stop,args.expected,args.xor))
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    base = args.output.resolve()
    root = base.parent / f'{base.stem}_{stamp}'
    root.mkdir(parents=True, exist_ok=False)
    paths = []
    for i, packet in enumerate(packets, 1):
        path = root / f'commands_{stamp}_{i:03d}.json'
        save(path, packet)
        paths.append(path)
    total = sum(map(len, packets))
    scan = (args.first_stop-args.first_start+1)*(args.second_stop-args.second_start+1)*(args.third_stop-args.third_start+1)*(args.fourth_stop-args.fourth_start+1)
    save(root/'scan_settings.json', dict(parameters={k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()}, scan_commands=scan, control_commands=total-scan, total_commands=total, packets=[dict(file=p.name,count=len(q)) for p,q in zip(paths,packets)]))
    print(f'Scan={scan}; control={total-scan}; total={total}; packets={len(paths)}', flush=True)
    print(f'Directory: {root}', flush=True)
    if args.generate_only:
        return
    flags = ['port','auto','ret','start_us','gap_us','period_us','ret_us','confirm_us','timeout','repeat']
    for i, path in enumerate(paths,1):
        report = root / f'report_{i:03d}.json'
        cmd = [sys.executable,str(executor),'--file',str(path),'--output',str(report)]
        for key in flags:
            cmd.extend(['--'+key.replace('_','-'),str(getattr(args,key))])
        print(f'\n=== PACKET {i}/{len(paths)} ({len(packets[i-1])} commands) ===',flush=True)
        subprocess.run(cmd,check=True)
        archives = list(root.glob(f'{report.stem}_*.zip'))
        if len(archives)!=1:
            raise RuntimeError(f'Expected one report archive for packet {i}')
        with zipfile.ZipFile(archives[0]) as z:
            if z.testzip() is not None:
                raise RuntimeError(f'Corrupt archive for packet {i}')
            stats=json.loads(z.read('statistics.json'))
        if not stats.get('complete') or stats.get('commands') != len(packets[i-1])*args.repeat:
            raise RuntimeError(f'Incomplete packet {i}; scan stopped')
    print(f'\nSCAN COMPLETE: {len(paths)} packets; {total*args.repeat} executed commands\n{root}',flush=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError, KeyboardInterrupt) as e:
        print(f'SCAN STOPPED: {e}',file=sys.stderr)
        sys.exit(1)
