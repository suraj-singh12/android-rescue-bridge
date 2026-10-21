#!/usr/bin/env python3
"""Provision/revoke Rescue Bridge devices without user accounts."""
from __future__ import annotations
import argparse, hashlib, json, os, re, secrets, ssl, subprocess
from pathlib import Path


def slug(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.strip()).strip("-").lower()
    return s[:40] or "android"


def load(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"version": 1, "devices": {}}


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    try: os.chmod(path, 0o600)
    except OSError: pass


def cert_fp(path: Path) -> str:
    pem = path.read_text(encoding="utf-8")
    der = ssl.PEM_cert_to_DER_cert(pem)
    return hashlib.sha256(der).hexdigest().upper()


def cmd_init(args):
    out = Path(args.dir).resolve(); out.mkdir(parents=True, exist_ok=True)
    key, cert, registry = out/"relay.key", out/"relay.crt", out/"devices.json"
    if key.exists() or cert.exists():
        raise SystemExit("relay.key/relay.crt already exist; refusing to overwrite")
    san = f"DNS:{args.name}" if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", args.name) else f"IP:{args.name}"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:3072", "-sha256", "-nodes",
        "-days", str(args.days), "-keyout", str(key), "-out", str(cert),
        "-subj", f"/CN={args.name}", "-addext", f"subjectAltName={san}"
    ], check=True)
    os.chmod(key, 0o600)
    save(registry, {"version": 1, "devices": {}})
    print(f"Created:\n  {key}\n  {cert}\n  {registry}")
    print("Certificate SHA-256 pin:", cert_fp(cert))


def cmd_add(args):
    registry = Path(args.registry).resolve(); data = load(registry)
    device_id = args.device_id or f"{slug(args.name)}-{secrets.token_hex(3)}"
    if device_id in data.setdefault("devices", {}):
        raise SystemExit(f"device id already exists: {device_id}")
    token = secrets.token_urlsafe(32)
    data["devices"][device_id] = {
        "name": args.name,
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
    }
    save(registry, data)
    verify = args.tls_verify
    fp = ""
    if verify == "pin":
        if not args.cert:
            raise SystemExit("--cert is required with --tls-verify pin")
        fp = cert_fp(Path(args.cert))
    base = {
        "version": 1,
        "relayHost": args.relay_host,
        "relayPort": args.relay_port,
        "tls": not args.no_tls,
        "tlsVerify": verify,
        "certSha256": fp,
        "deviceId": device_id,
        "token": token,
    }
    phone = dict(base, localAdbHost="127.0.0.1", localAdbPort=args.adb_port)
    mac = dict(base, localListenHost="127.0.0.1", localListenPort=args.mac_port)
    out = Path(args.out).resolve(); out.mkdir(parents=True, exist_ok=True)
    phone_path = out/f"{device_id}.phone.json"; mac_path = out/f"{device_id}.mac.json"
    save(phone_path, phone); save(mac_path, mac)
    print("Enrolled:", device_id)
    print("Phone config:", phone_path)
    print("Mac config:  ", mac_path)
    print("Registry:    ", registry)
    print("\nThe plaintext token exists only in the two config files. The relay registry stores only its SHA-256 hash.")


def cmd_remove(args):
    path = Path(args.registry).resolve(); data = load(path)
    if data.get("devices", {}).pop(args.device_id, None) is None:
        raise SystemExit("device id not found")
    save(path, data); print("Revoked:", args.device_id)


def cmd_list(args):
    data=load(Path(args.registry));
    for k,v in sorted(data.get("devices",{}).items()): print(f"{k}\t{v.get('name','')}")


def main():
    p=argparse.ArgumentParser(description="Provision Rescue Bridge without accounts")
    sub=p.add_subparsers(dest="cmd",required=True)
    q=sub.add_parser("init-relay", help="create a self-signed TLS relay certificate and empty registry")
    q.add_argument("--dir",default="relay-state"); q.add_argument("--name",required=True,help="relay DNS name or IP included in certificate"); q.add_argument("--days",type=int,default=825); q.set_defaults(fn=cmd_init)
    q=sub.add_parser("add-device")
    q.add_argument("--registry",required=True); q.add_argument("--name",required=True); q.add_argument("--device-id")
    q.add_argument("--relay-host",required=True); q.add_argument("--relay-port",type=int,default=8443)
    q.add_argument("--tls-verify",choices=["system","pin","none"],default="pin"); q.add_argument("--cert"); q.add_argument("--no-tls",action="store_true")
    q.add_argument("--adb-port",type=int,default=5555); q.add_argument("--mac-port",type=int,default=15555); q.add_argument("--out",default="configs"); q.set_defaults(fn=cmd_add)
    q=sub.add_parser("remove-device"); q.add_argument("--registry",required=True); q.add_argument("device_id"); q.set_defaults(fn=cmd_remove)
    q=sub.add_parser("list-devices"); q.add_argument("--registry",required=True); q.set_defaults(fn=cmd_list)
    args=p.parse_args(); args.fn(args)

if __name__=="__main__": main()
