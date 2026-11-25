#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SIR88 V26: six passes per RX divider, independent TX/RX BRR, 9N1 STM32F103.
Host stages triple commands from V23 response-only TSV using OpenOCD/ST-LINK.
All stages have CRC32, durable output logs, no-reset automatic transitions.
"""
from __future__ import annotations
import argparse
from array import array
import hashlib
import json
import csv
from datetime import datetime
import gzip
import io
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import time
import zlib

MAGIC=0x91B02691
A_COUNT=80722
B_COUNT=256733
TWO_COUNT=512*512
STAGE_TOTALS={1:TWO_COUNT,2:TWO_COUNT,3:A_COUNT,4:A_COUNT,5:B_COUNT,6:B_COUNT}
ALL_TOTAL=sum(STAGE_TOTALS.values())
CAPACITY=8192
CMD_CAPACITY=512
CYCLES_PER_US=48
HEADER=struct.Struct('<4sB3xIIIII')
BLOCK_MAGIC=b'S25B'
STATE_IDLE,STATE_RUN,STATE_BLOCK,STATE_DONE,STATE_ERROR,STATE_NEED_CMDS=range(6)
DEFAULT_ROOT=Path(__file__).resolve().parent
DEFAULT_OPENOCD=Path(r'C:\ST\ArduinoData\Arduino15\packages\STMicroelectronics\tools\xpack-openocd\0.12.0-6\bin\openocd.exe')
NAME={1:'TWO_FORWARD',2:'TWO_REVERSE',3:'A_FORWARD',4:'A_REVERSE',5:'B_FORWARD',6:'B_REVERSE'}
FIELDS=('scan_magic','scan_protocol','scan_buf_capacity','scan_cmd_capacity','scan_request_seq',
        'scan_request_done','scan_request_phase','scan_request_start',
        'scan_state','scan_phase','scan_next_order','scan_chunk_seq','scan_ack_seq',
        'scan_chunk_start','scan_chunk_count','scan_chunk_bytes','scan_chunk_crc32',
        'scan_error','scan_brr','scan_tx_brr_request','scan_rx_brr_request','scan_tx_brr_active','scan_rx_brr_active','scan_buffer','scan_cmd_req_seq','scan_cmd_ready_seq',
        'scan_cmd_req_start','scan_cmd_start','scan_cmd_count','scan_cmd_crc32',
        'scan_cmd_stage','scan_cmd_buffer')


def load_commands(path:Path):
    if not path.is_file():raise RuntimeError('Нет файла V23 с RX: '+str(path))
    cmd_a=array('I');cmd_b=array('I')
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):
            h.update(chunk)
    previous={1:-1,2:-1};unique=set()
    with path.open('r',encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f,delimiter='\t')
        required={'stage','index','tx1','tx2','tx3'}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError('TSV не содержит stage/index/tx1/tx2/tx3')
        for row in reader:
            stage=int(row['stage']);index=int(row['index'])
            if stage not in (1,2):raise ValueError('Неизвестная группа V23: '+str(stage))
            if index<=previous[stage]:raise ValueError('Нарушен порядок индексов группы '+str(stage))
            previous[stage]=index
            a,b,c=(int(row[k],16) for k in ('tx1','tx2','tx3'))
            if any(w<0 or w>511 for w in (a,b,c)):raise ValueError('Слово TX вне 9-bit: '+str(row))
            packed=(a<<18)|(b<<9)|c
            if packed in unique:raise ValueError('Повтор команды V23: '+str(row))
            unique.add(packed)
            (cmd_a if stage==1 else cmd_b).append(packed)
    if len(cmd_a)!=A_COUNT or len(cmd_b)!=B_COUNT:
        raise ValueError(f'Ожидалось {A_COUNT}/{B_COUNT} команд A/B, получено {len(cmd_a)}/{len(cmd_b)}')
    return {3:cmd_a,5:cmd_b},h.hexdigest()


def idx_to_words(stage:int,order:int,sets:dict)->tuple[int,...]:
    if not 0<=order<STAGE_TOTALS[stage]:raise ValueError('Индекс за границами этапа')
    if stage<=2:
        flat=order if stage==1 else TWO_COUNT-1-order
        return flat>>9,flat&511
    arr=sets[3 if stage<=4 else 5]
    packed=arr[order if stage&1 else len(arr)-1-order]
    return (packed>>18)&511,(packed>>9)&511,packed&511


def packed_batch(stage:int,start:int,sets:dict)->bytes:
    count=min(CMD_CAPACITY,STAGE_TOTALS[stage]-start)
    if stage<=2:raise ValueError('Для двухсловного прохода список не передаётся')
    arr=sets[3 if stage<=4 else 5]
    data=array('I',(arr[start+i] if stage&1 else arr[len(arr)-1-(start+i)] for i in range(count)))
    # Endian independent: packed command is uint32 little-endian in target SRAM.
    return struct.pack('<'+str(len(data))+'I',*data)


def ensure_manifest(folder:Path,digest:str):
    manifest=folder/'SOURCE_V23_SHA256.txt'
    payload=digest+'\n'
    if manifest.exists():
        if manifest.read_text(encoding='ascii')!=payload:
            raise RuntimeError('В каталоге другой исходный список команд V23! Без повторной передачи не продолжаю')
    else:
        if any(folder.glob('*_block_*.blk')):
            raise RuntimeError('Есть старые блоки, но нет контрольной суммы входного списка')
        tmp=manifest.with_suffix('.tmp')
        with tmp.open('w',encoding='ascii') as f:
            f.write(payload);sync_file(f)
        os.replace(tmp,manifest)

def sym_addr(mapping:str,name:str)->int:
    rows=mapping.splitlines();ident=re.escape(name)
    p1=re.compile(r'^\s*(0x[0-9A-Fa-f]+)\s+'+ident+r'(?:\s|$)')
    p2=re.compile(r'^\s*'+ident+r'\s+(0x[0-9A-Fa-f]+)(?:\s|$)')
    for row in rows:
        for p in (p1,p2):
            m=p.match(row)
            if m:
                addr=int(m.group(1),16)
                if 0x20000000<=addr<0x20010000:return addr
    sec=re.compile(r'^\s*\.(?:data|bss)\.'+ident+r'(?:\s|$)')
    addr_re=re.compile(r'^\s*(0x[0-9a-fA-F]+)\s+(?:0x[0-9a-fA-F]+|\d+)\b')
    for i,row in enumerate(rows):
        if not sec.match(row):continue
        for nxt in rows[i:i+5]:
            m=addr_re.match(nxt)
            if m:
                addr=int(m.group(1),16)
                if 0x20000000<=addr<0x20010000:return addr
    raise RuntimeError(f'Нет адреса {name} в .map; проверь Clean+Build V26')


class OpenOCD:
    def __init__(self,root:Path,exe:Path,map_file:Path):
        if not exe.is_file():raise RuntimeError(f'Не найден OpenOCD: {exe}')
        if not map_file.is_file():raise RuntimeError(f'Не найдена карта компоновки: {map_file}')
        self.root,self.exe=root,exe
        text=map_file.read_text(encoding='utf-8',errors='replace')
        self.a={name:sym_addr(text,name) for name in FIELDS}

    def run(self,cmd:str,timeout=60)->str:
        proc=subprocess.run([str(self.exe),'-f','interface/stlink.cfg',
                             '-f','target/stm32f1x.cfg',
                             '-c','init; '+cmd+'; shutdown'],
                            cwd=str(self.root),capture_output=True,text=True,
                            errors='replace',timeout=timeout)
        output=proc.stdout+'\n'+proc.stderr
        if proc.returncode or 'Error:' in output:
            raise RuntimeError('Ошибка OpenOCD: '+output[-1800:])
        return output

    def read(self,*keys:str)->dict[str,int]:
        result=self.run('; '.join(f'echo V_{k}=[read_memory 0x{self.a[k]:08x} 32 1]' for k in keys))
        parsed={}
        for k in keys:
            m=re.search(r'(?m)^V_'+re.escape(k)+r'=0x([0-9a-fA-F]+)',result)
            if not m:raise RuntimeError(f'Не прочитан {k}: '+result[-600:])
            parsed[k]=int(m.group(1),16)
        return parsed

    def write(self,**pairs:int)->None:
        self.run('; '.join(f'mww 0x{self.a[k]:08x} 0x{v:08x}' for k,v in pairs.items()))

    def dump(self,length:int,dest:Path)->bytes:
        if not (0<=length<=CAPACITY):raise ValueError('Размер блока вне диапазона')
        if not length:return b''
        self.run(f'dump_image {{{dest.as_posix()}}} 0x{self.a["scan_buffer"]:08x} {length}')
        blob=dest.read_bytes()
        if len(blob)!=length:raise RuntimeError(f'Неполное чтение SWD: {len(blob)} != {length}')
        return blob


    def upload(self,raw:bytes,filename:Path,*,stage:int,start:int,request:int)->None:
        """Only WRITE queued commands, then signal ready; no SRAM read-back.

        A single OpenOCD invocation loads the new TX queue and sets its
        metadata; scan_cmd_ready_seq is written LAST, to release the MCU.
        The V26 firmware keeps the V25 queue format unchanged.
        """
        if len(raw)==0 or len(raw)>CMD_CAPACITY*4 or len(raw)%4:
            raise ValueError('Размер пакета команд не кратен 4 байтам или превышает SRAM')
        with filename.open('wb') as f:
            f.write(raw)
        values=dict(scan_cmd_stage=stage,scan_cmd_start=start,
                    scan_cmd_count=len(raw)//4,scan_cmd_crc32=zlib.crc32(raw),
                    scan_cmd_ready_seq=request)
        commands=[f'load_image {{{filename.as_posix()}}} 0x{self.a["scan_cmd_buffer"]:08x} bin']
        commands.extend(f'mww 0x{self.a[k]:08x} 0x{v:08x}' for k,v in values.items())
        self.run('; '.join(commands))


def decode(raw:bytes,count:int):
    result=[];pos=0
    for _ in range(count):
        if pos>=len(raw):raise ValueError('Буфер закончился раньше записи')
        tag=raw[pos];pos+=1
        if tag==0:
            result.append((0,0,0,(),()))
            continue
        if not tag&0x80:raise ValueError('Некорректная сигнатура записи')
        n=tag&15;estimated=bool(tag&0x20)
        if n>8 or pos+6+n*3>len(raw):raise ValueError('Повреждена запись RX')
        full,delay=struct.unpack_from('<IH',raw,pos);pos+=6
        if not full or n!=min(8,full):raise ValueError('Недопустимое полное число RX')
        words=[];flags=[]
        for _ in range(n):
            w,f=struct.unpack_from('<HB',raw,pos);pos+=3
            if w>511 or f&~0x0F:raise ValueError('Недопустимое RX-слово или флаги')
            words.append(w);flags.append(f)
        result.append((full,delay,int(estimated),tuple(words),tuple(flags)))
    if pos!=len(raw):raise ValueError('Наличие лишних байтов после записей')
    return result


def make_block(stage:int,seq:int,start:int,count:int,raw:bytes,crc:int)->bytes:
    if zlib.crc32(raw)!=crc:raise ValueError('CRC загружаемых данных не сходится')
    return HEADER.pack(BLOCK_MAGIC,stage,seq,start,count,len(raw),crc)+raw


def parse_block(blob:bytes):
    if len(blob)<HEADER.size:raise ValueError('Нет заголовка блока')
    magic,stage,seq,start,count,length,crc=HEADER.unpack_from(blob)
    if magic!=BLOCK_MAGIC or stage not in STAGE_TOTALS or length>CAPACITY or len(blob)!=HEADER.size+length:
        raise ValueError('Неверный формат блока S25B (V25/V26)')
    if not count or start+count>STAGE_TOTALS[stage]:raise ValueError('Некорректные индексы блока')
    raw=blob[HEADER.size:]
    if zlib.crc32(raw)!=crc:raise ValueError('CRC32 блока не сходится')
    return dict(stage=stage,seq=seq,start=start,count=count,crc=crc,
                results=decode(raw,count),raw=raw)


def sync_file(f):
    f.flush();os.fsync(f.fileno())


def save_block(folder:Path,stage:int,host_seq:int,blob:bytes)->Path:
    name=f'{NAME[stage]}_block_{host_seq:06d}.blk'
    target=folder/name
    if target.exists():raise RuntimeError('Блок уже существует: '+name)
    tmp=folder/(name+'.tmp')
    with tmp.open('wb') as f:
        f.write(blob);sync_file(f)
    os.replace(tmp,target)
    return target


def block_paths(folder:Path,stage:int):
    return sorted(folder.glob(f'{NAME[stage]}_block_*.blk'))


def blank_stats():
    return dict(n=0,rx=0,rx0=0,words=0,fe=0,ne=0,ore=0,pe=0,long=0,approx=0)


def stats(records):
    s=blank_stats()
    for full,delay,estimate,words,flags in records:
        s['n']+=1
        s['rx']+=int(full>0)
        s['rx0']+=int(full==0)
        s['words']+=full
        s['long']+=int(full>8)
        s['approx']+=estimate
        for flag in flags:
            s['fe']+=bool(flag&2)
            s['ne']+=bool(flag&4)
            s['ore']+=bool(flag&8)
            s['pe']+=bool(flag&1)
    return s


def add_stats(a,b):
    for k,v in b.items():a[k]+=v


def inspect_existing(folder:Path,stage:int):
    count=0;seq=0;summary=blank_stats();last_file=None;last=None
    for path in block_paths(folder,stage):
        item=parse_block(path.read_bytes())
        seq+=1
        if item['stage']!=stage or item['seq']!=seq or item['start']!=count:
            raise RuntimeError(f'Нарушена непрерывность журнала: {path.name}')
        count+=item['count'];add_stats(summary,stats(item['results']))
        last_file=path
        last=(item['start'],item['count'],item['crc'])
    return count,seq,summary,last_file,last


def report(stage,part,total,since,baseline):
    count=total['n'];maximum=STAGE_TOTALS[stage]
    elapsed=max(time.monotonic()-since,1e-6)
    speed=(count-baseline)/elapsed
    eta=(maximum-count)/speed if speed>0 else 0
    print(f'{NAME[stage]} {count:,}/{maximum:,} ({100*count/maximum:.3f}%) | '
          f'блок: N={part["n"]}, RX={part["rx"]}, RX0={part["rx0"]}, '
          f'words={part["words"]}, FE={part["fe"]}, NE={part["ne"]}, '
          f'ORE={part["ore"]}, >8={part["long"]} | '
          f'всего RX={total["rx"]}, FE={total["fe"]} | '
          f'{speed:.1f} команд/с, ETA {eta/3600:.2f} ч',flush=True)


def snapshot(dev:OpenOCD):
    return dev.read('scan_state','scan_phase','scan_next_order','scan_request_seq',
                    'scan_request_done','scan_chunk_seq','scan_ack_seq',
                    'scan_chunk_start','scan_chunk_count','scan_chunk_bytes',
                    'scan_chunk_crc32','scan_error',
                    'scan_cmd_req_seq','scan_cmd_ready_seq','scan_cmd_req_start')


def run_stage(dev:OpenOCD,folder:Path,stage:int,poll_s:float,sets:dict):
    total=STAGE_TOTALS[stage]
    done,disk_seq,aggregate,lastfile,last=inspect_existing(folder,stage)
    baseline=done
    if done>total:raise RuntimeError('Журнал больше допустимого количества команд')
    print(f'\n{NAME[stage]}: сохранено {done:,}/{total:,}; блоков: {disk_seq}.',flush=True)
    if done==total:
        # A full saved stage can still be frozen in the MCU if power/PC failed
        # after the final fsync but before sending the final ACK.
        state=snapshot(dev)
        if state['scan_state']==STATE_BLOCK and state['scan_phase']==stage:
            start=state['scan_chunk_start'];n=state['scan_chunk_count']
            length=state['scan_chunk_bytes'];crc=state['scan_chunk_crc32']
            if lastfile is None or last!=(start,n,crc) or start+n!=done:
                raise RuntimeError('Последний блок этапа на диске не совпадает с ожидающим STM32')
            raw=dev.dump(length,folder/'swd_v26_dump.tmp')
            if zlib.crc32(raw)!=crc or parse_block(lastfile.read_bytes())['raw']!=raw:
                raise RuntimeError('CRC/содержимое финального блока не совпадает; ACK запрещён')
            dev.write(scan_ack_seq=state['scan_chunk_seq'])
            print('Финальный блок уже был на диске: ACK отправлен без повторной передачи.',flush=True)
        elif state['scan_state']==STATE_RUN and state['scan_phase']==stage:
            # A previously sent ACK may still be transitioning to DONE.
            pass
        elif state['scan_state']==STATE_ERROR:
            raise RuntimeError('STM32 сообщает ошибку '+str(state['scan_error']))
        print('Этап полностью сохранён ранее, повторных команд не передаю.',flush=True)
        return
    state=snapshot(dev)
    if state['scan_state']==STATE_ERROR:
        raise RuntimeError('STM32 сообщает ошибку '+str(state['scan_error']))
    if state['scan_state'] in (STATE_RUN,STATE_BLOCK,STATE_NEED_CMDS):
        if state['scan_phase']!=stage:
            raise RuntimeError('STM32 выполняет другой этап, автоматическое вмешательство запрещено')
        print('Продолжаю уже выполняемый этап контроллера.',flush=True)
    elif state['scan_state'] in (STATE_IDLE,STATE_DONE):
        ticket=(state['scan_request_seq']+1)&0xFFFFFFFF or 1
        dev.write(scan_request_phase=stage,scan_request_start=done,scan_request_seq=ticket)
        print(f'Старт этапа {NAME[stage]} с порядкового индекса {done}.',flush=True)
    else:raise RuntimeError('Неизвестное состояние контроллера')
    since=time.monotonic();last_print=since
    temp_dump=folder/'swd_v26_dump.tmp'
    while True:
        state=snapshot(dev)
        if state['scan_state']==STATE_ERROR:
            raise RuntimeError('Ошибка STM32: '+str(state['scan_error']))
        if state['scan_state']==STATE_DONE and state['scan_phase']==stage:
            if done!=total:raise RuntimeError(f'STM32 завершил этап, но сохранено только {done}/{total}')
            break
        if state['scan_state']==STATE_BLOCK:
            if state['scan_phase']!=stage:raise RuntimeError('Загружен блок не того этапа')
            seq=state['scan_chunk_seq'];start=state['scan_chunk_start']
            n=state['scan_chunk_count'];length=state['scan_chunk_bytes']
            crc=state['scan_chunk_crc32']
            if not (0<n<=total and 0<length<=CAPACITY):raise RuntimeError('Неверная длина блока STM32')
            raw=dev.dump(length,temp_dump)
            stable=dev.read('scan_state','scan_chunk_seq','scan_chunk_start','scan_chunk_count',
                            'scan_chunk_bytes','scan_chunk_crc32')
            if stable['scan_state']!=STATE_BLOCK or stable['scan_chunk_seq']!=seq or stable['scan_chunk_start']!=start or stable['scan_chunk_count']!=n or stable['scan_chunk_bytes']!=length or stable['scan_chunk_crc32']!=crc:
                raise RuntimeError('Контроллер изменил блок во время чтения; ACK не отправляю')
            if zlib.crc32(raw)!=crc:raise RuntimeError('CRC32 неверен; ACK не отправляю')
            if start==done:
                host_seq=disk_seq+1
                blob=make_block(stage,host_seq,start,n,raw,crc)
                item=parse_block(blob)
                path=save_block(folder,stage,host_seq,blob)
                disk_seq+=1;done+=n
                lastfile=path;last=(start,n,crc)
                part=stats(item['results']);add_stats(aggregate,part)
                report(stage,part,aggregate,since,baseline)
            elif start+n==done and lastfile is not None and last is not None:
                # Previously saved but ACK not yet delivered; verify the frozen bytes.
                if last!=(start,n,crc):raise RuntimeError('Последний блок на диске не совпадает с замороженным STM32')
                recorded=parse_block(lastfile.read_bytes())
                if recorded['raw']!=raw:raise RuntimeError('Данные сохранённого блока отличаются от STM32')
                print(f'Блок {lastfile.name} уже записан; подтверждаю без повторной передачи.',flush=True)
            else:
                raise RuntimeError(f'Разрыв индексов: STM32 start={start}, count={n}; на диске {done}. ACK запрещён.')
            # Successful disk fsync+atomic rename was completed above.
            dev.write(scan_ack_seq=seq)
        elif state['scan_state']==STATE_NEED_CMDS:
            if stage<=2:raise RuntimeError('STM32 запросил пакет данных для двухсловного этапа')
            req=state['scan_cmd_req_seq'];start=state['scan_cmd_req_start']
            if req==0 or start!=done:
                raise RuntimeError(f'Индекс пакета TX не совпадает с сохранённым RX: MCU={start}, диск={done}')
            raw=packed_batch(stage,start,sets)
            temp_cmd=folder/'swd_v26_cmd_upload.tmp'
            # One SWD load + ready signal; no read-back of TX queue.
            dev.upload(raw,temp_cmd,stage=stage,start=start,request=req)
        elif state['scan_state']==STATE_RUN:
            if time.monotonic()-last_print>3:
                print(f' STM32 выполняет {NAME[stage]}: {state["scan_next_order"]:,}/{total:,}; '
                      f'на ПК сохранено {done:,}',flush=True)
                last_print=time.monotonic()
            time.sleep(poll_s)
        else:time.sleep(poll_s)
    print(f'{NAME[stage]}: все {done:,} записей сохранены в блоках.',flush=True)


def export_stage(folder:Path,stage:int,sets:dict,make_tsv:bool=True):
    """Sequential streaming without ever keeping the entire (up to 134M) scan in RAM."""
    name=NAME[stage]
    binfile=folder/f'{name}.bin'
    tsvfile=folder/(name+'.tsv' if stage<=4 else name+'.tsv.gz')
    if binfile.exists() and (not make_tsv or tsvfile.exists()):
        print(f'Экспорт {name} уже существует: {binfile.name}, {tsvfile.name if make_tsv else "TSV отключён"}.',flush=True)
        return
    bin_tmp=folder/(binfile.name+'.part')
    tsv_tmp=folder/(tsvfile.name+'.part')
    print(f'Экспортирую {name} в {binfile.name}' + (f' и {tsvfile.name}' if make_tsv else ''),flush=True)
    expected=0
    with bin_tmp.open('wb') as bfile:
        if make_tsv:
            text_raw=tsv_tmp.open('wb')
            # For Windows: fsync the actual writable file descriptor after gzip is closed.
            if stage>=5:
                gz=gzip.GzipFile(fileobj=text_raw,mode='wb',compresslevel=1,mtime=0)
                tf=io.TextIOWrapper(gz,encoding='utf-8',newline='')
            else:
                tf=io.TextIOWrapper(text_raw,encoding='utf-8',newline='')
            writer=csv.writer(tf,delimiter='\t',lineterminator='\n')
            writer.writerow(['stage','order_index','tx_9n1','rx_count_full','first_rx_delay_us',
                             'saved_9bit_words','saved_flags_hex','rx_gt_8','start_time_estimated'])
        try:
            for filename in block_paths(folder,stage):
                blob=filename.read_bytes();x=parse_block(blob)
                if x['stage']!=stage or x['start']!=expected:
                    raise RuntimeError(f'Несовпадение порядка при экспорте: {filename.name}')
                bfile.write(blob)
                if make_tsv:
                    for k,(full,delay,estimated,words,flags) in enumerate(x['results']):
                        order=x['start']+k
                        command=idx_to_words(stage,order,sets)
                        writer.writerow([name,order,' '.join(f'{w:03X}' for w in command),
                                         full,f'{delay/CYCLES_PER_US:.3f}' if full else '',
                                         ' '.join(f'{w:03X}' for w in words),
                                         ' '.join(f'{f:02X}' for f in flags),
                                         int(full>8),estimated])
                expected+=x['count']
            if expected!=STAGE_TOTALS[stage]:
                raise RuntimeError(f'Экспортирован неполный этап: {expected} из {STAGE_TOTALS[stage]}')
            sync_file(bfile)
            if make_tsv:
                tf.flush()
                if stage<=4:
                    # For plain TSV TextIOWrapper owns the raw file; fsync
                    # BEFORE closing the wrapper (Windows EBADF regression).
                    sync_file(text_raw)
                    tf.close()
                else:
                    # Closing the wrapper finalizes the gzip trailer; the
                    # supplied fileobj remains open for fsync after that.
                    tf.close()
                    sync_file(text_raw)
        finally:
            if make_tsv:
                if not tf.closed:tf.close()
                text_raw.close()
    os.replace(bin_tmp,binfile)
    if make_tsv:os.replace(tsv_tmp,tsvfile)
    print(f'Готово: {name} — {expected:,} запросов.',flush=True)


def write_summary(folder:Path,digest:str):
    rows=[]
    for phase in STAGE_TOTALS:
        done,block_count,summary,_,_=inspect_existing(folder,phase)
        if done!=STAGE_TOTALS[phase]:raise RuntimeError('Итоговый отчёт: не хватает записей '+NAME[phase])
        rows.append([NAME[phase],done,block_count,summary['rx'],summary['rx0'],
                     summary['words'],summary['fe'],summary['ne'],summary['ore'],
                     summary['pe'],summary['long'],summary['approx']])
    dest=folder/'SUMMARY_V26.tsv';tmp=folder/'SUMMARY_V26.tsv.tmp'
    with tmp.open('w',encoding='utf-8',newline='') as f:
        writer=csv.writer(f,delimiter='\t',lineterminator='\n')
        writer.writerow(['stage','commands','blocks','rx_positive','rx_zero','rx_words',
                         'FE','NE','ORE','PE','rx_gt_8','timing_approx'])
        writer.writerows(rows)
        sums=[sum(r[j] for r in rows) for j in range(1,len(rows[0]))]
        writer.writerow(['TOTAL']+sums)
        sync_file(f)
    os.replace(tmp,dest)
    print('Итоговый отчёт:',dest,flush=True)


def self_test(source:Path):
    import tempfile
    sets,digest=load_commands(source)
    assert sum(STAGE_TOTALS.values())==1199198
    assert idx_to_words(1,0,sets)==(0,0)
    assert idx_to_words(1,262143,sets)==(511,511)
    assert idx_to_words(2,0,sets)==(511,511)
    assert idx_to_words(2,262143,sets)==(0,0)
    assert idx_to_words(3,0,sets)==idx_to_words(4,A_COUNT-1,sets)
    assert idx_to_words(5,0,sets)==idx_to_words(6,B_COUNT-1,sets)
    for stage in (3,4,5,6):
        for start in (0,1,512,STAGE_TOTALS[stage]-1):
            raw=packed_batch(stage,start,sets)
            actual=struct.unpack('<'+str(len(raw)//4)+'I',raw)
            for i,p in enumerate(actual):
                w=idx_to_words(stage,start+i,sets)
                assert p==(w[0]<<18)|(w[1]<<9)|w[2]
    rx3=b'\x83'+struct.pack('<IH',3,768)+b''.join(struct.pack('<HB',w,f) for w,f in [(0x181,2),(0x1C8,0),(0x01C,0)])
    rx29=b'\x88'+struct.pack('<IH',29,1440)+b''.join(struct.pack('<HB',w,2) for w in range(8))
    raw=b'\x00'+rx3+rx29
    for stage in STAGE_TOTALS:
        blob=make_block(stage,1,0,3,raw,zlib.crc32(raw))
        item=parse_block(blob)
        assert [r[0] for r in item['results']]==[0,3,29]
        try:parse_block(blob[:-1]+b'X')
        except ValueError:pass
        else:raise AssertionError('CRC32 не заметил повреждения')
    with tempfile.TemporaryDirectory() as td:
        folder=Path(td)
        ensure_manifest(folder,digest)
        ensure_manifest(folder,digest)
        try:ensure_manifest(folder,'f'*64)
        except RuntimeError:pass
        else:raise AssertionError('Исходный список должен быть закреплён SHA256')
        blob=make_block(3,1,0,3,raw,zlib.crc32(raw))
        save_block(folder,3,1,blob)
        done,seq,s,lastfile,last=inspect_existing(folder,3)
        assert done==3 and seq==1 and s['rx']==2 and s['rx0']==1
    print('SELFTEST PASS: V26 host split BRR | 6 stages | 1199198 TX | 2-word both directions | '+
          'V23 A/B both directions | TX write-only | RX0/RX3/RX29 | disk recovery')


def brr_baud(brr:int)->float:
    return 48_000_000/brr


def validate_brr(tx:int,rx_values:list[int]):
    if not 16<=tx<=65535:raise ValueError(f'TX BRR вне 16..65535: {tx}')
    if not rx_values:raise ValueError('Нужно указать хотя бы один RX BRR')
    for rx in rx_values:
        if not 16<=rx<=65535:raise ValueError(f'RX BRR вне 16..65535: {rx}')
    if len(set(rx_values))!=len(rx_values):
        raise ValueError('Повторяющиеся RX BRR в одной серии недопустимы')


def write_brr_settings(folder:Path,tx:int,rx:int,digest:str):
    """Each data folder carries its TX/RX settings without changing V25 TSV columns."""
    dest=folder/'BRR_SETTINGS.tsv'
    with dest.open('w',encoding='utf-8',newline='') as f:
        w=csv.writer(f,delimiter='\t',lineterminator='\n')
        w.writerow(['tx_brr','rx_brr','tx_baud','rx_baud','rx_start_deadline_us','rx_frame_us',
                    'first_word_wait_us','rx_silence_gap_us','post_rx_us','source_sha256'])
        frame=11*rx/48
        gap=max(30,frame+7)
        first=max(22,frame+7)
        w.writerow([tx,rx,f'{brr_baud(tx):.6f}',f'{brr_baud(rx):.6f}',84,
                    f'{frame:.6f}',f'{first:.6f}',f'{gap:.6f}',40,digest])
        sync_file(f)


def write_sweep_summary(root_folder:Path,rows):
    dest=root_folder/'SWEEP_SUMMARY_V26.tsv'
    with dest.open('w',encoding='utf-8',newline='') as f:
        w=csv.writer(f,delimiter='\t',lineterminator='\n')
        w.writerow(['tx_brr','rx_brr','tx_baud','rx_baud','folder','commands','rx_positive','FE','NE','ORE'])
        w.writerows(rows)
        sync_file(f)
    print('Сводка серии:',dest,flush=True)


def main():
    parser=argparse.ArgumentParser(description='SIR88 V26: 6 проходов на каждый RX BRR, независимые делители TX/RX')
    parser.add_argument('--root',type=Path,default=DEFAULT_ROOT)
    parser.add_argument('--openocd',type=Path,default=DEFAULT_OPENOCD)
    parser.add_argument('--map',type=Path)
    parser.add_argument('--commands',type=Path,default=Path(__file__).resolve().with_name('SIR88_V23_A_B_RX_commands.tsv'))
    parser.add_argument('--poll-ms',type=int,default=200)
    parser.add_argument('--tx-brr',type=int,default=64,help='BRR передачи (по умолчанию 64 = 750000 бод)')
    parser.add_argument('--rx-brr',type=int,nargs='+',default=[67,70,73,76],
                        help='BRR приёма: одно значение или список, по умолчанию 67 70 73 76')
    parser.add_argument('--self-test',action='store_true')
    args=parser.parse_args()
    validate_brr(args.tx_brr,args.rx_brr)
    sets,digest=load_commands(args.commands)
    if args.self_test:self_test(args.commands);return
    root=args.root
    parent=root/('sir88_scan_v26_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
    if parent.exists():raise RuntimeError('Каталог нового опыта уже существует: '+str(parent))
    map_file=args.map
    if map_file is None:
        expected=root/'Debug'/(root.name+'.map')
        if expected.is_file():map_file=expected
        else:
            candidates=sorted((root/'Debug').glob('*.map'))
            if len(candidates)==1:map_file=candidates[0]
            else:raise RuntimeError('Не выбран .map V26. Передай --map с полным путём')
    dev=OpenOCD(root,args.openocd,map_file)
    identity=dev.read('scan_magic','scan_protocol','scan_buf_capacity','scan_cmd_capacity')
    expected=dict(scan_magic=MAGIC,scan_protocol=26,scan_buf_capacity=CAPACITY,
                  scan_cmd_capacity=CMD_CAPACITY)
    if identity!=expected:
        raise RuntimeError(f'Неверная прошивка V26 или старая .map! STM32={identity}, ожидается={expected}')
    # One reset at the BEGINNING of the series, only STM32 via ST-LINK.
    # Neither SIR88 nor STM32 is reset between divider settings or scan stages.
    dev.run('reset run')
    ready=dev.read('scan_magic','scan_protocol','scan_state','scan_phase','scan_request_seq','scan_brr',
                   'scan_tx_brr_request','scan_rx_brr_request')
    if ready!=dict(scan_magic=MAGIC,scan_protocol=26,scan_state=STATE_IDLE,scan_phase=0,scan_request_seq=0,
                   scan_brr=64,scan_tx_brr_request=64,scan_rx_brr_request=64):
        raise RuntimeError('Сброс STM32 не подготовил чистое состояние: '+str(ready))
    parent.mkdir(parents=True)
    print(f'SIR88 V26 | 9N1 | TX BRR={args.tx_brr} ({brr_baud(args.tx_brr):.1f} бод) | '
          f'RX BRR={args.rx_brr} | 84 мкс | {ALL_TOTAL*len(args.rx_brr):,} запросов')
    print(f'V23 источник: {args.commands} | SHA256 {digest}')
    print('Каталог:',parent,flush=True)
    totals=[]
    for rx in args.rx_brr:
        folder=parent/f'TX_{args.tx_brr:05d}_RX_{rx:05d}'
        folder.mkdir()
        ensure_manifest(folder,digest)
        write_brr_settings(folder,args.tx_brr,rx,digest)
        print(f'\n=== TX BRR {args.tx_brr}, RX BRR {rx} | '
              f'TX {brr_baud(args.tx_brr):.1f} бод / RX {brr_baud(rx):.1f} бод ===',flush=True)
        # One SWD write for both requested dividers; MCU latches at each stage entry.
        # No read-back of transmitted TX command batches.
        dev.write(scan_tx_brr_request=args.tx_brr,scan_rx_brr_request=rx)
        for stage in STAGE_TOTALS:
            run_stage(dev,folder,stage,max(args.poll_ms,1)/1000,sets)
            export_stage(folder,stage,sets,make_tsv=True)
        write_summary(folder,digest)
        stage_rows=[inspect_existing(folder,stage)[2] for stage in STAGE_TOTALS]
        totals.append([args.tx_brr,rx,f'{brr_baud(args.tx_brr):.6f}',f'{brr_baud(rx):.6f}',
                       folder.name,ALL_TOTAL,sum(v['rx'] for v in stage_rows),
                       sum(v['fe'] for v in stage_rows),sum(v['ne'] for v in stage_rows),
                       sum(v['ore'] for v in stage_rows)])
        write_sweep_summary(parent,totals)
        print(f'RX BRR {rx}: все 6 проходов сохранены и экспортированы.',flush=True)
    print('Серия V26 завершена; прежние версии не изменены.',flush=True)


if __name__=='__main__':
    try:main()
    except (KeyboardInterrupt,EOFError):
        print('Остановлено на ПК. Неподтверждённый блок остаётся в STM32 при сохранении питания.',file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        import traceback
        print('ОШИБКА:',exc,file=sys.stderr);traceback.print_exc()
        print('ACK не отправляется при ошибке сохранения/CRC; никакого автоматического повтора не выполняется.',file=sys.stderr)
        sys.exit(1)
