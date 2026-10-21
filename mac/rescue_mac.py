#!/usr/bin/env python3
"""Mac-side Rescue Bridge.

Exposes the distant Android adbd as a local TCP endpoint. Ordinary adb then uses:
    adb connect 127.0.0.1:15555
No account/sign-in is involved; the JSON config contains a per-device PSK.
"""
from __future__ import annotations
import argparse, asyncio, hashlib, json, os, signal, socket, ssl, subprocess, sys
from pathlib import Path
from typing import Optional

CHUNK=128*1024


def norm_fp(s:str)->str: return ''.join(c for c in s.upper() if c in '0123456789ABCDEF')


def load_config(path:Path)->dict:
    data=json.loads(path.read_text(encoding='utf-8'))
    for k in ('relayHost','relayPort','deviceId','token'):
        if k not in data: raise ValueError(f'missing config field: {k}')
    return data


def tls_context(cfg:dict)->Optional[ssl.SSLContext]:
    if not cfg.get('tls',True): return None
    mode=cfg.get('tlsVerify','system')
    if mode=='system':
        return ssl.create_default_context()
    ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    return ctx


async def open_relay(cfg:dict):
    ctx=tls_context(cfg); host=str(cfg['relayHost']); port=int(cfg['relayPort'])
    reader,writer=await asyncio.open_connection(host,port,ssl=ctx,server_hostname=(host if ctx and cfg.get('tlsVerify','system')=='system' else None))
    sock=writer.get_extra_info('socket')
    if sock:
        try: sock.setsockopt(socket.SOL_SOCKET,socket.SO_KEEPALIVE,1)
        except OSError: pass
    if ctx and cfg.get('tlsVerify')=='pin':
        sslobj=writer.get_extra_info('ssl_object'); cert=sslobj.getpeercert(binary_form=True) if sslobj else None
        got=hashlib.sha256(cert or b'').hexdigest().upper(); expected=norm_fp(str(cfg.get('certSha256','')))
        if not expected or not __import__('hmac').compare_digest(got,expected):
            writer.close(); await writer.wait_closed(); raise ssl.SSLError(f'relay certificate pin mismatch: got {got}')
    return reader,writer


async def send_hello(writer,cfg,role='client'):
    msg={'v':1,'role':role,'device_id':cfg['deviceId'],'token':cfg['token']}
    writer.write((json.dumps(msg,separators=(',',':'))+'\n').encode()); await writer.drain()


async def agent_status(cfg):
    reader,writer=await open_relay(cfg)
    try:
        await send_hello(writer,cfg,'status')
        line=await asyncio.wait_for(reader.readline(),timeout=10)
        text=line.decode('utf-8','replace').strip()
        if text.startswith('STATUS '): return text.split(' ',1)[1]
        raise RuntimeError(text or 'relay closed connection')
    finally:
        writer.close()
        try: await writer.wait_closed()
        except Exception: pass


async def pipe(reader,writer):
    try:
        while True:
            b=await reader.read(CHUNK)
            if not b: return
            writer.write(b); await writer.drain()
    except (ConnectionError,asyncio.CancelledError): return


class Bridge:
    def __init__(self,cfg): self.cfg=cfg; self.lock=asyncio.Lock(); self.active=False
    async def handle(self,lreader,lwriter):
        peer=lwriter.get_extra_info('peername')
        if self.lock.locked():
            print(f'[!] Rejecting extra local connection from {peer}: one ADB transport is already active')
            lwriter.close(); await lwriter.wait_closed(); return
        async with self.lock:
            print(f'[+] Local ADB connection from {peer}; contacting relay...')
            rwriter=None
            try:
                rreader,rwriter=await open_relay(self.cfg); await send_hello(rwriter,self.cfg)
                line=await asyncio.wait_for(rreader.readline(),timeout=15)
                status=line.decode('utf-8','replace').strip()
                if status!='GO':
                    print('[x] Relay refused tunnel:',status or 'connection closed'); return
                print('[+] Remote ADB tunnel ACTIVE')
                a=asyncio.create_task(pipe(lreader,rwriter)); b=asyncio.create_task(pipe(rreader,lwriter))
                done,pending=await asyncio.wait({a,b},return_when=asyncio.FIRST_COMPLETED)
                for t in pending:t.cancel()
                await asyncio.gather(*pending,return_exceptions=True); await asyncio.gather(*done,return_exceptions=True)
            except Exception as exc:
                print('[x] Tunnel error:',exc)
            finally:
                if rwriter:
                    rwriter.close()
                    try: await rwriter.wait_closed()
                    except Exception: pass
                lwriter.close()
                try: await lwriter.wait_closed()
                except Exception: pass
                print('[*] Remote ADB tunnel closed; waiting for another adb connect')


async def run_cmd(*args):
    proc=await asyncio.create_subprocess_exec(*args); return await proc.wait()


async def main_async(args):
    cfg=load_config(Path(args.config).expanduser())
    host=args.listen_host or cfg.get('localListenHost','127.0.0.1'); port=args.listen_port or int(cfg.get('localListenPort',15555))
    serial=f'{host}:{port}'
    if args.status:
        print('[+] Agent status:',await agent_status(cfg))
        return
    bridge=Bridge(cfg); server=await asyncio.start_server(bridge.handle,host,port)
    print(f'[+] Rescue Bridge ready for {cfg["deviceId"]}')
    print(f'[+] Local endpoint: {serial}')
    print(f'    adb connect {serial}')
    print(f'    scrcpy -s {serial}')
    if args.wait_agent:
        while True:
            try:
                state=await agent_status(cfg)
                print('[*] Agent status:',state)
                if state=='ONLINE': break
            except Exception as exc:
                print('[!] Status check failed:',exc)
            await asyncio.sleep(args.wait_interval)
    stop=asyncio.Event(); loop=asyncio.get_running_loop()
    for sig in (signal.SIGINT,signal.SIGTERM):
        try: loop.add_signal_handler(sig,stop.set)
        except NotImplementedError: pass
    async with server:
        if args.connect_adb:
            await asyncio.sleep(.25)
            adb=args.adb or 'adb'
            await run_cmd(adb,'disconnect',serial)
            try:
                rc=await run_cmd(adb,'connect',serial)
            except FileNotFoundError:
                raise RuntimeError(f'adb executable not found: {adb}')
            if rc==0 and args.scrcpy:
                scrcpy=args.scrcpy_bin or 'scrcpy'
                try:
                    subprocess.Popen([scrcpy,'-s',serial])
                except FileNotFoundError:
                    print(f'[!] scrcpy executable not found: {scrcpy}')
        await stop.wait()


def main():
    p=argparse.ArgumentParser(description='Expose a remote Rescue Bridge Android phone as a local ADB endpoint')
    p.add_argument('--config',required=True); p.add_argument('--listen-host'); p.add_argument('--listen-port',type=int)
    p.add_argument('--connect-adb',action='store_true',help='automatically run adb connect against the local bridge')
    p.add_argument('--status',action='store_true',help='print authenticated phone status and exit')
    p.add_argument('--wait-agent',action='store_true',help='wait until the phone agent reports ONLINE before continuing')
    p.add_argument('--wait-interval',type=float,default=3.0)
    p.add_argument('--adb'); p.add_argument('--scrcpy',action='store_true'); p.add_argument('--scrcpy-bin')
    args=p.parse_args()
    try: asyncio.run(main_async(args))
    except KeyboardInterrupt: pass

if __name__=='__main__': main()
