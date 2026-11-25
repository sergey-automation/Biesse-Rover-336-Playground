# -*- coding: utf-8 -*-
"""V13_XOR_3 three/four-word scan for the V27 firmware, w0 given on the command line. Persistent OpenOCD; no analyzer.

New versus v13_xor_2: --word0 (0..511, default 0). The value is written to the
firmware's word_0 variable once at scan start (always, including 0, for
determinism). word_0==0 keeps the legacy 3-word mode exactly as before.
word_0>0 switches to 4-word commands: the firmware prepends word_0 on the bus,
the acceptance filter becomes word_0 ^ w0 ^ w1 ^ w2 == 256, and all reports
carry the word_0 column/value.

Filter: a command is sent only if the XOR of its command words == 256 (decimal):
w0 ^ w1 ^ w2 in 3-word mode, word_0 ^ w0 ^ w1 ^ w2 when --word0 > 0.
The first word w0 is a run parameter (--w0, default 0x13F); the pass name and all
exports carry it (W0_<HEX>_*). RUN_CONFIG.json in the result folder records w0,
BRR, the filter rule and the final pass statistics, so every folder is
self-describing.
Sent commands get a second index s (0..M-1 in order of acceptance); the mapping
s -> g (g = index in the full 512x512 grid) is exported so every firmware reply
can be attributed to the exact command that produced it.
W0_*_ANSWERS_XOR256.tsv lists only the commands that received a reply whose
answer-word XOR == XOR_TARGET.

Difference from v13_xor_1: legacy exports are removed - no W0_<HEX>.tsv, no
SUMMARY_V26.tsv, no DO_NOT_USE_LEGACY_TSV.txt. The pass statistics line is kept
in the new-report set: PASS_SUMMARY.tsv and the "stats" field of RUN_CONFIG.json
(RUN_CONFIG.json is written at the end of the run so it contains final counters).
"""
import argparse
import csv
from datetime import datetime
from pathlib import Path
import socket
import subprocess
import tempfile
import sys
import time
import zlib
import struct
import gzip
import json
from array import array
import sir88_scan_v26 as v

class PersistentOpenOCD(v.OpenOCD):
    """One OpenOCD process and one framed TCL connection for the experiment."""
    def __init__(self, root, exe, map_file):
        super().__init__(root, exe, map_file)
        # V27: the module-level FIELDS tuple predates word_0; resolve it from
        # the same .map. On a V26 .map this raises a clear "no address" error.
        if 'word_0' not in self.a:
            self.a['word_0'] = v.sym_addr(map_file.read_text(encoding='utf-8', errors='replace'), 'word_0')
        self.sock = None
        self.proc = None
        self.log = tempfile.TemporaryFile(mode='w+b')
        self.pending = b''
        try:
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            self.proc = subprocess.Popen([str(self.exe), '-f', 'interface/stlink.cfg',
                '-f', 'target/stm32f1x.cfg', '-c',
                f'bindto 127.0.0.1; tcl_port {port}; telnet_port disabled; gdb_port disabled; init'],
                cwd=str(self.root), stdout=self.log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 15
            while self.sock is None:
                if self.proc.poll() is not None or time.monotonic() >= deadline:
                    self.log.seek(0)
                    raise RuntimeError('OpenOCD startup failed: '+self.log.read().decode(errors='replace')[-3000:])
                try:
                    self.sock = socket.create_connection(('127.0.0.1',port), timeout=.2)
                except OSError:
                    # Startup only: wait for server socket, not between measurements.
                    if self.proc.poll() is None:
                        time.sleep(.01)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.run('targets')
        except BaseException:
            self.close()
            raise

    def run(self, cmd, timeout=60):
        self.sock.settimeout(timeout)
        # catch propagates TCL errors; no replay of uncertain writes.
        script = 'set __rc [catch {' + cmd + '} __result]; format "%s\\n%s" $__rc $__result'
        self.sock.sendall(script.encode('utf-8') + b'\x1a')
        while b'\x1a' not in self.pending:
            block = self.sock.recv(65536)
            if not block:
                raise RuntimeError('OpenOCD TCL disconnected')
            self.pending += block
        frame, self.pending = self.pending.split(b'\x1a', 1)
        result = frame.decode('utf-8', errors='replace')
        status, sep, value = result.partition('\n')
        if not sep or status.strip() != '0':
            raise RuntimeError('OpenOCD TCL error: '+result)
        return value

    def read(self, *keys):
        # Unlike echo, the Tcl list is returned directly over the RPC socket.
        result = self.run('list ' + ' '.join(
            f'[read_memory 0x{self.a[k]:08x} 32 1]' for k in keys))
        values = result.split()
        if len(values) != len(keys):
            raise RuntimeError('Unexpected read_memory response: '+result)
        return {k: int(x, 0) for k,x in zip(keys,values)}

    def close(self):
        if self.sock is not None:
            try:
                self.sock.sendall(b'shutdown\x1a')
            except OSError:
                pass
            self.sock.close()
            self.sock = None
        if self.proc is not None:
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try: self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill(); self.proc.wait()
            self.proc = None
        self.log.close()



STAGE=5
LIMIT=v.STAGE_TOTALS[STAGE]
TOTAL=512*512
XOR_TARGET=256
DEFAULT_W0=0x13F
DEFAULT_WORD0=0
PASSES=()   # built from --w0 in main()
WORD_0=0    # set from --word0 in main()

def words(p):
    return ((p>>18)&511,(p>>9)&511,p&511)

def accept(p):
    w0,w1,w2=words(p)
    return (WORD_0^w0^w1^w2)==XOR_TARGET if WORD_0 else (w0^w1^w2)==XOR_TARGET

def commands_mapped(pos, fixed):
    """Accepted commands only, plus mapping sent-index -> global grid index."""
    arr=array('I'); mapping=array('I'); g=0
    for i in range(512):
        for j in range(512):
            p=(fixed<<18)|(i<<9)|j if pos==0 else (i<<18)|(fixed<<9)|j
            if accept(p): arr.append(p); mapping.append(g)
            g+=1
    return arr,mapping

class OffsetDevice:
    """Translate host-local indices into the firmware's fixed stage suffix."""
    def __init__(self, dev, offset): self.dev=dev; self.offset=offset
    def read(self,*keys):
        d=self.dev.read(*keys)
        for k in ('scan_next_order','scan_chunk_start','scan_cmd_req_start'):
            if k in d: d[k]-=self.offset
        return d
    def write(self,**kw):
        if 'scan_request_start' in kw: kw['scan_request_start']+=self.offset
        return self.dev.write(**kw)
    def upload(self,raw,filename,*,stage,start,request):
        return self.dev.upload(raw,filename,stage=stage,start=start+self.offset,request=request)
    def dump(self,*args): return self.dev.dump(*args)

def run_part(dev, folder, arr, tx, rx):
    folder.mkdir()
    offset=LIMIT-len(arr)
    if not 0<=offset<LIMIT: raise ValueError('Invalid part length')
    v.write_brr_settings(folder,tx,rx,'custom_three_word_fixed_field')
    (folder/'INDEX_MAPPING.json').write_text(json.dumps(dict(firmware_stage=STAGE,firmware_start=offset,host_count=len(arr)),indent=2))
    previous=v.STAGE_TOTALS[STAGE]
    try:
        v.STAGE_TOTALS[STAGE]=len(arr)
        proxy=OffsetDevice(dev,offset)
        v.run_stage(proxy,folder,STAGE,0,{5:arr})
        n,blocks,stats,_,_=v.inspect_existing(folder,STAGE)
        if n!=len(arr): raise RuntimeError('Incomplete part')
        active=dev.read('scan_tx_brr_active','scan_rx_brr_active')
        if active!=dict(scan_tx_brr_active=tx,scan_rx_brr_active=rx): raise RuntimeError('Active BRR mismatch')
        v.export_stage(folder,STAGE,{5:arr},make_tsv=True)
        return stats,blocks
    finally: v.STAGE_TOTALS[STAGE]=previous

def scan(dev, folder, tx, rx):
    identity=dict(scan_magic=v.MAGIC,scan_protocol=26,scan_buf_capacity=v.CAPACITY,scan_cmd_capacity=v.CMD_CAPACITY)
    if dev.read(*identity)!=identity: raise RuntimeError('V26 firmware/MAP mismatch')
    if v.snapshot(dev)['scan_state'] not in (v.STATE_IDLE,v.STATE_DONE): raise RuntimeError('Previous scan unfinished')
    folder.mkdir(parents=True,exist_ok=False)
    v.write_brr_settings(folder,tx,rx,'custom_three_word_fixed_field')
    started_iso=datetime.now().isoformat(timespec='milliseconds')
    dev.write(scan_tx_brr_request=tx,scan_rx_brr_request=rx)
    # V27: always program word_0 (including 0) so every run starts deterministically.
    dev.write(word_0=WORD_0)
    if dev.read('word_0')['word_0']!=WORD_0: raise RuntimeError('word_0 readback mismatch')
    keys=list(v.blank_stats()); started=time.perf_counter()
    pass_stats={}; pass_blocks={}; pass_answered={}; pass_xor256={}
    if True:
        for name,pos,fixed in PASSES:
            arr,mapping=commands_mapped(pos,fixed)
            dest=folder/name;dest.mkdir(); agg=v.blank_stats();blocks=0
            # mapping of sent index -> global grid index (single segment: s here is global)
            with (folder/'INDEX2INDEX1.tsv').open('w',newline='',encoding='utf-8') as mf:
                mw=csv.writer(mf,delimiter='\t',lineterminator='\n')
                mw.writerow(['index2','index1','command_hex','word_0','w0','w1','w2'])
                for s,(g,p) in enumerate(zip(mapping,arr)):
                    w0,w1,w2=words(p); mw.writerow([s,g,f'0x{p:07x}',f'0x{WORD_0:03X}',w0,w1,w2])
                v.sync_file(mf)
            segments=[(0,min(LIMIT,len(arr)))]
            if len(arr)>LIMIT: segments.append((LIMIT,len(arr)))
            for part,(lo,hi) in enumerate(segments,1):
                d=dest/f'part_{part}'; print(f'{name}: {lo}..{hi-1}',flush=True)
                s,b=run_part(dev,d,arr[lo:hi],tx,rx);v.add_stats(agg,s);blocks+=b
            # Mapped export: same replies but attributed to grid index and command words.
            answers_xor256=[]; answer_col=None; total_answered=0
            with (folder/(name+'_MAPPED.tsv')).open('w',newline='',encoding='utf-8') as out:
                writer=csv.writer(out,delimiter='\t',lineterminator='\n')
                mapped_header=['stage','index2','index1','command_hex','word_0','w0','w1','w2']
                for part,lo in ((1,0),(2,LIMIT)):
                    if lo>=len(arr): break
                    with gzip.open(dest/f'part_{part}'/'B_FORWARD.tsv.gz','rt',encoding='utf-8',newline='') as inp:
                        r=csv.reader(inp,delimiter='\t');header=next(r)
                        if part==1:
                            writer.writerow(mapped_header+header[2:])
                            cols={h:i for i,h in enumerate(header)}
                            answer_col=cols.get('rx_count_full')
                        for row in r:
                            s=int(row[1])+lo; g=int(mapping[s]); p=int(arr[s])
                            w0,w1,w2=words(p)
                            writer.writerow([name,s,g,f'0x{p:07x}',f'0x{WORD_0:03X}',w0,w1,w2,*row[2:]])
                            if answer_col is not None and int(row[answer_col] or 0)>0:
                                total_answered+=1
                                aw=[int(x,16) for x in row[cols['saved_9bit_words']].split()] if row[cols['saved_9bit_words']] else []
                                ax=0
                                for val in aw: ax^=val
                                if ax==XOR_TARGET:
                                    answers_xor256.append([name,s,g,f'0x{p:07x}',f'0x{WORD_0:03X}',row[cols['tx_9n1']],
                                        row[cols['rx_count_full']],row[cols['first_rx_delay_us']],
                                        row[cols['saved_9bit_words']],ax,row[cols['saved_flags_hex']]])
                v.sync_file(out)
            # Answers with answer-word XOR == XOR_TARGET (separate table).
            with (folder/(name+'_ANSWERS_XOR256.tsv')).open('w',newline='',encoding='utf-8') as out:
                writer=csv.writer(out,delimiter='\t',lineterminator='\n')
                writer.writerow(['stage','index2','index1','command_hex','word_0','tx_9n1',
                    'rx_count_full','first_rx_delay_us','answer_words_hex','answer_xor','answer_flags_hex'])
                writer.writerows(answers_xor256)
                v.sync_file(out)
            print(f'{name}: answered={total_answered}, answers with answer-XOR=={XOR_TARGET}: {len(answers_xor256)}',flush=True)
            pass_stats[name]=dict(agg); pass_blocks[name]=blocks
            pass_answered[name]=total_answered; pass_xor256[name]=len(answers_xor256)
            print(f'{name}: {agg}',flush=True)
    # PASS_SUMMARY.tsv: the former SUMMARY_V26.tsv line(s), kept in the new-report set.
    with (folder/'PASS_SUMMARY.tsv').open('w',newline='',encoding='utf-8') as f:
        w=csv.writer(f,delimiter='\t',lineterminator='\n')
        w.writerow(['stage','blocks','answered','xor256_answers',*keys])
        for name,_,_ in PASSES:
            w.writerow([name,pass_blocks[name],pass_answered[name],pass_xor256[name],*[pass_stats[name][k] for k in keys]])
        v.sync_file(f)
    (folder/'RUN_CONFIG.json').write_text(json.dumps(dict(
        script=Path(__file__).name,
        w0=PASSES[0][2], w0_hex=f'0x{PASSES[0][2]:03X}', pass_name=PASSES[0][0],
        word_0=WORD_0, word_0_hex=f'0x{WORD_0:03X}',
        command_words=(4 if WORD_0 else 3),
        xor_target=XOR_TARGET,
        filter_rule=('word_0 ^ w0 ^ w1 ^ w2 == XOR_target' if WORD_0 else 'w0 ^ w1 ^ w2 == XOR_target'),
        tx_brr=tx, rx_brr=rx, grid_total=TOTAL,
        started=started_iso,
        stats={n: dict(blocks=pass_blocks[n], answered=pass_answered[n], xor256_answers=pass_xor256[n], **pass_stats[n]) for n,_,_ in PASSES}),
        indent=2))
    print(f'DONE: {sum(len(commands_mapped(p,fx)[0]) for _,p,fx in PASSES)} requests (filtered of {TOTAL}) | {time.perf_counter()-started:.3f} s',flush=True)

def self_test():
    assert XOR_TARGET==256
    for name,pos,fixed in PASSES:
        arr,mapping=commands_mapped(pos,fixed)
        assert len(arr)==len(mapping)
        assert len(set(arr))==len(arr) and len(set(mapping))==len(mapping)
        assert all(mapping[t]<mapping[t+1] for t in range(len(mapping)-1))
        for s,(g,p) in enumerate(zip(mapping,arr)):
            w=words(p)
            assert w[pos]==fixed
            assert ((WORD_0^w[0]^w[1]^w[2])==XOR_TARGET) if WORD_0 else ((w[0]^w[1]^w[2])==XOR_TARGET)
            assert (w[1]<<9)|w[2]==g if pos==0 else (w[0]<<9)|w[2]==g
        # w2 = (word_0 ^ w0 ^ XOR_TARGET) ^ w1 -> exactly 512 accepted commands for any w0/word_0
        assert len(arr)==512 and int(mapping[10])==((10<<9)|(((WORD_0^fixed)^XOR_TARGET)^10))
        for lo,hi in ((0,min(LIMIT,len(arr))),):
            part=arr[lo:hi]; offset=LIMIT-len(part)
            assert offset+len(part)==LIMIT
            old=v.STAGE_TOTALS[STAGE];v.STAGE_TOTALS[STAGE]=len(part)
            try:
                for i in range(0,len(part),v.CMD_CAPACITY):
                    raw=v.packed_batch(STAGE,i,{5:part}); n=len(raw)//4
                    assert struct.unpack('<'+str(n)+'I',raw)==tuple(arr[lo+i:lo+i+n])
                    assert offset+i+n<=LIMIT
                    blob=v.make_block(STAGE,i+1,i,n,b'\0'*n,zlib.crc32(b'\0'*n))
                    assert v.parse_block(blob)['count']==n
            finally:v.STAGE_TOTALS[STAGE]=old
    class Fake:
        def read(self,*keys):return {k:LIMIT-512 for k in keys}
        def write(self,**kw):assert kw['scan_request_start']==LIMIT-512
        def upload(self,raw,filename,**kw):assert kw['start']==LIMIT-512
    proxy=OffsetDevice(Fake(),LIMIT-512)
    assert proxy.read('scan_cmd_req_start')['scan_cmd_req_start']==0
    proxy.write(scan_request_start=0);proxy.upload(b'1234',None,stage=5,start=0,request=1)
    print('SELFTEST PASS: XOR==256 filter, 512 of 262144 accepted, s->g mapping, queue packing, CRC, index translation')

def parse_w0(text):
    try: w0=int(text,0)
    except ValueError: raise argparse.ArgumentTypeError(f'Invalid --w0 value: {text!r}')
    if not 0<=w0<=511: raise argparse.ArgumentTypeError(f'--w0 must be a 9-bit word (0..511), got {w0}')
    return w0

def main():
    global PASSES, WORD_0
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=v.DEFAULT_ROOT)
    p.add_argument('--openocd',type=Path,default=v.DEFAULT_OPENOCD)
    p.add_argument('--map',type=Path)
    p.add_argument('--tx-brr',type=int,default=64)
    p.add_argument('--rx-brr',type=int,default=64)
    p.add_argument('--w0',type=parse_w0,default=DEFAULT_W0,
        help='first 9-bit command word, hex or decimal (default: 0x%03X)'%DEFAULT_W0)
    p.add_argument('--word0',type=parse_w0,default=DEFAULT_WORD0,
        help='V27 leading 9-bit word written to firmware word_0 at start; 0 = 3-word mode (default: 0)')
    p.add_argument('--self-test',action='store_true')
    a=p.parse_args()
    PASSES=((f'W0_{a.w0:03X}',0,a.w0),)
    WORD_0=a.word0
    if a.self_test:self_test();return
    v.validate_brr(a.tx_brr,[a.rx_brr])
    root=a.root.resolve();mp=a.map
    if mp is None:
        mp=root/'Debug'/(root.name+'.map')
        if not mp.is_file():
            candidates=list((root/'Debug').glob('*.map'))
            if len(candidates)!=1:raise RuntimeError('Specify --map')
            mp=candidates[0]
    tag=f'word0-{a.word0:03X}_' if a.word0 else ''
    folder=root/(f'sir88_stm32_V27_three_scan3_v13_xor_1_{tag}w0-{a.w0:03X}_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    mode='4-word' if a.word0 else '3-word'
    print(f'V13_XOR_3 | word_0=0x{a.word0:03X} ({mode}) | w0=0x{a.w0:03X} | XOR==256 filter | TX BRR={a.tx_brr} | RX BRR={a.rx_brr} | {folder}',flush=True)
    dev=PersistentOpenOCD(root,a.openocd,mp)
    try:scan(dev,folder,a.tx_brr,a.rx_brr)
    finally:dev.close()

if __name__=='__main__':
    try:main()
    except KeyboardInterrupt:
        print('Stopped; no automatic retry or reset.',file=sys.stderr);sys.exit(130)
    except Exception as e:
        print(f'ERROR: {e}; no automatic retry or reset.',file=sys.stderr);sys.exit(1)
