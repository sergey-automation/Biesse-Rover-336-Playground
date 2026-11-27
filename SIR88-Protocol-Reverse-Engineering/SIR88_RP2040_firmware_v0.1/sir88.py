"""SIR88 RP2040, TZ 1.1. Python 3.9+, pyserial."""
import argparse, struct, zlib, json, time
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
def main():
    a=argparse.ArgumentParser();a.add_argument('--self-test',action='store_true');a.add_argument('--port');a.add_argument('--list',action='store_true');a.add_argument('--cmd',nargs='+',action='append',help='9-bit hex words, repeat --cmd for multiple commands');a.add_argument('--file',type=Path,help='JSON array of {tx:[integer words],expected:3,xor:256}');a.add_argument('--expected',type=int,default=3);a.add_argument('--xor',type=lambda x:int(x,16),default=256);a.add_argument('--auto',type=int,choices=[0,1],default=0);a.add_argument('--ret',type=int,choices=range(6),default=0);a.add_argument('--start-us',type=float,default=8800/120);a.add_argument('--gap-us',type=float,default=13.2);a.add_argument('--period-us',type=float,default=352);a.add_argument('--ret-us',type=float,default=8800/120);a.add_argument('--confirm-us',type=float,default=0);a.add_argument('--timeout',type=float,default=30);a.add_argument('--output',type=Path,default=Path('sir88_results.json'));a.add_argument('--repeat',type=int,default=1);args=a.parse_args()
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
    with serial.Serial(args.port,115200,timeout=args.timeout,write_timeout=args.timeout) as p:
        p.reset_input_buffer()
        def request(op,data=b''):
            p.write(packet(op,data));kind,answer=read_frame(p)
            if (kind,answer)!=(129,bytes([op])):raise RuntimeError('unexpected ACK')
        request(1,payload);all_results=[]
        for i in range(args.repeat):
            request(2 if i==0 else 3);kind,data=read_frame(p)
            if kind!=130:raise RuntimeError('unexpected result opcode')
            result=parse_results(data);all_results.append(result);args.output.write_text(json.dumps(all_results,indent=2),encoding='utf-8')
            for e in result['commands']:
                print('TX',' '.join(f'{x:03X}' for x in e['tx']))
                for r in e['attempts']:print(' RX',' '.join(f'{x:03X}' for x in r['rx']) or 'TIMEOUT',f"N={r['count']} ERR={r['error_mask']:02X} XOR={r['xor']:03X} DELAY={r['delay_us']} us CONF={r['confirm']}")
            if result['status']:break
if __name__=='__main__':main()
