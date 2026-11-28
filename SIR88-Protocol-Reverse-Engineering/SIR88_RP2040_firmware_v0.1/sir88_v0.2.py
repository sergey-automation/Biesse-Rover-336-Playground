"""SIR88 RP2040 host v0.2, TZ 1.1. Python 3.9+, pyserial.
Wire protocol unchanged from v0.1. Reports use unique filenames.
"""
import argparse, struct, zlib, json, time
import os, sys, zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
MAGIC=b'\xa5\x5a'; SETTINGS=struct.Struct('<HBB5I'); CMD=struct.Struct('<BBH7H'); RS=struct.Struct('<6H4B2H')
def packet(op,p=b''):
    body=struct.pack('<BI',op,len(p))+p
    return MAGIC+body+struct.pack('<I',zlib.crc32(body))
def decode_frame(b):
    if len(b)<11 or b[:2]!=MAGIC: raise ValueError('MAGIC/length')
    op,n=struct.unpack_from('<BI',b,2)
    if n>70660 or len(b)!=11+n or zlib.crc32(b[2:-4])!=struct.unpack_from('<I',b,len(b)-4)[0]: raise ValueError('length/CRC')
    return op,b[7:-4]
def read_frame(port):
    def exact(n):
        out=bytearray(); deadline=time.monotonic()+port.timeout
        while len(out)<n:
            chunk=port.read(min(n-len(out),4096));out.extend(chunk)
            if time.monotonic()>deadline: raise TimeoutError(f'USB: {len(out)}/{n} bytes')
        return bytes(out)
    prev=b''
    while True:
        b=exact(1)
        if prev+b==MAGIC:break
        prev=b
    h=exact(5);n=struct.unpack_from('<I',h,1)[0]
    if n>70660:raise ValueError('oversize payload')
    op,p=decode_frame(MAGIC+h+exact(n+4))
    if op==255:raise RuntimeError(f'Firmware error: opcode={p[0]:02X}, code={p[1]}')
    return op,p
def cmd_record(words,expected,xor):
    if not 1<=len(words)<=7 or not 1<=expected<=6 or not 0<=xor<=511 or any(not 0<=w<=511 for w in words):raise ValueError('command range')
    return CMD.pack(len(words),expected,xor,*(words+[0]*(7-len(words))))
def parse_results(p):
    status,reserved,n=struct.unpack_from('<BBH',p)
    if len(p)!=4+n*138 or n>512 or reserved:raise ValueError('result length')
    result=[];off=4
    for _ in range(n):
        ln,expected,xor,*words=CMD.unpack_from(p,off);off+=18
        entry={'tx':words[:ln],'expected':expected,'expected_xor':xor,'attempts':[]}
        for j in range(6):
            *w,count,err,confirm,valid,actual,delay=RS.unpack_from(p,off);off+=20
            if valid:entry['attempts'].append(dict(attempt=j,rx=w[:min(count,6)],count=count,error_mask=err,confirm=confirm,xor=actual,delay_ticks=delay,delay_us=delay/120 if count else None))
        result.append(entry)
    return {'status':status,'commands':result}
def ticks(us):return int(us*120+0.5)
def selftest():
    assert (SETTINGS.size,CMD.size,RS.size)==(24,18,20)
    assert zlib.crc32(b'123456789')==0xcbf43926
    p=packet(1,b'abc');assert decode_frame(p)==(1,b'abc')
    c=cmd_record([0x130,4,0x30],3,256); r=RS.pack(0x130,4,0x34,0,0,0,3,0,0,1,256,1920)
    x=parse_results(struct.pack('<BBH',0,0,1)+c+r+bytes(100));assert x['commands'][0]['attempts'][0]['delay_us']==16
    assert len(packet(130,bytes(70660)))==70671
    print('SELFTEST PASS (Python framing, CRC, record sizes, result decoding)')
def enable_color():
    # Windows 10 CMD supports ANSI after enabling virtual terminal processing.
    if not sys.stdout.isatty() or os.environ.get('NO_COLOR') is not None:
        return False
    if os.name == 'nt':
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        if not k.SetConsoleMode(h, mode.value | 4):
            return False
    return True

def color_text(text, code, enabled):
    return f'\x1b[{code}m{text}\x1b[0m' if enabled else text

def show_result(result, color):
    for e in result['commands']:
        print(color_text('TX ' + ' '.join(f'{x:03X}' for x in e['tx']), '94', color))
        for r in e['attempts']:
            frame = 'RX ' + ' '.join(f'{x:03X}' for x in r['rx'])
            if not r['count']:
                frame = 'RX TIMEOUT'
            delay = 'None' if r['delay_us'] is None else f"{r['delay_us']:.3f}"
            print(' ' + (color_text(frame, '92', color) if r['count'] else frame),
                  f"N={r['count']} ERR={r['error_mask']:02X} XOR={r['xor']:03X} DELAY={delay} us CONF={r['confirm']}")

def statistics(results, elapsed):
    lengths = Counter(); confirms = Counter(); unique = Counter()
    initial_unique = Counter(); delays = []; counts = Counter()
    for result in results:
        for e in result['commands']:
            counts['commands'] += 1
            for r in e['attempts']:
                initial = r['attempt'] == 0
                counts['attempts'] += 1
                counts['initial_attempts' if initial else 'retry_attempts'] += 1
                confirms[r['confirm']] += 1
                n = r['count']
                if not n:
                    counts['timeouts'] += 1
                    counts['initial_timeouts' if initial else 'retry_timeouts'] += 1
                    continue
                counts['responses'] += 1
                counts['initial_responses' if initial else 'retry_responses'] += 1
                lengths[str(n) if n <= 6 else '7+'] += 1
                counts['xor_match' if r['xor'] == e['expected_xor'] else 'xor_mismatch'] += 1
                counts['short_responses'] += int(n < e['expected'])
                counts['frames_with_stop_errors'] += bool(r['error_mask'])
                counts['stop_errors'] += bin(r['error_mask']).count('1')
                counts['responses_255'] += int(n == 255)
                key = ' '.join(f'{x:03X}' for x in r['rx'])
                if n > 6:
                    key += f' ... (N={n}; prefix only)'
                unique[key] += 1
                if initial:
                    initial_unique[key] += 1
                if r['delay_us'] is not None:
                    delays.append(r['delay_us'])
    def distribution(counter):
        total = sum(counter.values())
        return [{'rx': k, 'count': v, 'percent': 100*v/total} for k,v in sorted(counter.items(), key=lambda kv:(-kv[1],kv[0]))]
    keys = ('commands attempts initial_attempts retry_attempts responses initial_responses retry_responses timeouts initial_timeouts retry_timeouts xor_match xor_mismatch short_responses frames_with_stop_errors stop_errors responses_255').split()
    return dict(packets=len(results), packet_statuses=[r['status'] for r in results],
                elapsed_seconds=elapsed, **{k:counts[k] for k in keys},
                response_lengths={k:lengths[k] for k in ['1','2','3','4','5','6','7+']},
                confirmations={name:confirms[i] for i,name in enumerate(['NONE','ACK','RET','NAK'])},
                delay_us={'min':min(delays) if delays else None,'mean':sum(delays)/len(delays) if delays else None,'max':max(delays) if delays else None},
                unique_initial_responses=distribution(initial_unique), unique_responses=distribution(unique))

def print_statistics(s):
    print('\n=== TOTAL STATISTICS ===')
    for key in ('packets','packet_statuses','commands','attempts','initial_attempts','retry_attempts','responses','initial_responses','retry_responses','timeouts','initial_timeouts','retry_timeouts','xor_match','xor_mismatch','short_responses','frames_with_stop_errors','stop_errors','responses_255'):
        print(f'{key}: {s[key]}')
    print('RS lengths:', ', '.join(f'{k}: {v}' for k,v in s['response_lengths'].items()))
    print('Confirmations:', ', '.join(f'{k}: {v}' for k,v in s['confirmations'].items()))
    print('Delay us (min / mean / max):', ' / '.join('N/A' if v is None else f'{v:.3f}' for v in s['delay_us'].values()))
    print(f"Elapsed seconds: {s['elapsed_seconds']:.3f}")
    for title,key in [('Initial RS','unique_initial_responses'),('All RS','unique_responses')]:
        print(title + ' (percent of nonempty RS):')
        for item in s[key]:
            print(f"  {item['rx']}: {item['count']} ({item['percent']:.3f}%)")

def report_path(base):
    base = Path(base)
    base.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    stem = base.stem if base.suffix else base.name
    for i in range(10000):
        suffix = '' if i == 0 else f'_{i}'
        path = base.parent / f'{stem}_{stamp}{suffix}.json'
        if path.with_suffix('.zip').exists():
            continue
        try:
            with path.open('x', encoding='utf-8') as f:
                f.write('[]\n')
            return path
        except FileExistsError:
            continue
    raise RuntimeError('Cannot reserve unique report name')

def save_json(path, data):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    temp.replace(path)

def archive_report(path, entries, settings, summary):
    archive = path.with_suffix('.zip')
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as z:
        z.write(path, arcname=path.name)
        for name, obj in [('commands.json',entries),('settings.json',settings),('statistics.json',summary)]:
            z.writestr(name, json.dumps(obj, indent=2, ensure_ascii=False) + '\n')
    return archive

def main():
    a=argparse.ArgumentParser();a.add_argument('--self-test',action='store_true');a.add_argument('--port');a.add_argument('--list',action='store_true');a.add_argument('--cmd',nargs='+',action='append',help='9-bit hex words, repeat --cmd for multiple commands');a.add_argument('--file',type=Path,help='JSON array of {tx:[integer words],expected:3,xor:256}');a.add_argument('--expected',type=int,default=3);a.add_argument('--xor',type=lambda x:int(x,16),default=256);a.add_argument('--auto',type=int,choices=[0,1],default=0);a.add_argument('--ret',type=int,choices=range(6),default=0);a.add_argument('--start-us',type=float,default=8800/120);a.add_argument('--gap-us',type=float,default=13.2);a.add_argument('--period-us',type=float,default=352);a.add_argument('--ret-us',type=float,default=8800/120);a.add_argument('--confirm-us',type=float,default=0);a.add_argument('--timeout',type=float,default=30);a.add_argument('--output',type=Path,default=Path('sir88_v0.2.json'),help='report base name/path; timestamp always appended');a.add_argument('--repeat',type=int,default=1);args=a.parse_args()
    if args.self_test:selftest();return
    import serial
    if args.list:
        from serial.tools.list_ports import comports
        for p in comports():print(p.device,p.description)
        return
    if not args.port or not (args.cmd or args.file):a.error('--port and --cmd or --file required')
    if args.file and args.cmd:a.error('choose --file or --cmd')
    if args.file:entries=json.loads(args.file.read_text())
    else:entries=[dict(tx=[int(w,16) for w in words],expected=args.expected,xor=args.xor) for words in args.cmd]
    if not 1<=len(entries)<=512 or args.repeat<1:a.error('packet/repeat range')
    times=[ticks(x) for x in (args.start_us,args.gap_us,args.period_us,args.ret_us,args.confirm_us)]
    if not all(1<=times[i]<=65535 for i in (0,1,3)) or not 38720<=times[2]<=0xffffffff or not 0<=times[4]<=65535:a.error('timing range')
    payload=SETTINGS.pack(len(entries),args.auto,args.ret,*times)+b''.join(cmd_record(e['tx'],e['expected'],e['xor']) for e in entries)
    color=enable_color()
    output=report_path(args.output)
    settings={k:(str(v) if isinstance(v,Path) else v) for k,v in vars(args).items()}
    settings.update(version='0.2', command_count=len(entries), timing_ticks=dict(zip(['start','gap','period','ret_start','confirm_delay'],times)), argv=sys.argv[1:])
    all_results=[]; failure=None; started=time.monotonic()
    try:
        with serial.Serial(args.port,115200,timeout=args.timeout,write_timeout=args.timeout) as p:
            p.reset_input_buffer()
            def request(op,data=b''):
                p.write(packet(op,data));kind,answer=read_frame(p)
                if (kind,answer)!=(129,bytes([op])):raise RuntimeError('unexpected ACK')
            request(1,payload)
            for i in range(args.repeat):
                request(2 if i==0 else 3);kind,data=read_frame(p)
                if kind!=130:raise RuntimeError('unexpected result opcode')
                result=parse_results(data);all_results.append(result)
                save_json(output,all_results)
                show_result(result,color)
                if result['status']:break
    except (Exception, KeyboardInterrupt) as exc:
        failure=f'{type(exc).__name__}: {exc}'
        raise
    finally:
        summary=statistics(all_results,time.monotonic()-started)
        summary['requested_packets']=args.repeat
        summary['requested_commands']=len(entries)*args.repeat
        summary['failure']=failure
        summary['complete']=(failure is None and len(all_results)==args.repeat and all(r['status']==0 and len(r['commands'])==len(entries) for r in all_results))
        print_statistics(summary)
        print('Complete:',summary['complete'])
        if failure: print('Failure:',failure)
        archive=archive_report(output,entries,settings,summary)
        print('JSON:',output.resolve())
        print('ZIP: ',archive.resolve())
if __name__=='__main__':main()
