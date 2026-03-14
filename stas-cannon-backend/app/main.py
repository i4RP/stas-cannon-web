from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import asyncio
import os
import secrets
import time
import json
import hashlib
import httpx

app = FastAPI()

# Disable CORS. Do not remove this for full-stack development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)


# --- BSV Wallet Utilities ---

BASE58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _hash256(data: bytes) -> bytes:
    return _sha256(_sha256(data))


def _ripemd160(data: bytes) -> bytes:
    """Pure Python RIPEMD-160 implementation for environments where OpenSSL doesn't support it."""
    try:
        return hashlib.new("ripemd160", data).digest()
    except (ValueError, AttributeError):
        pass
    # Pure Python RIPEMD-160
    def _f(x, y, z, i):
        if i == 0: return x ^ y ^ z
        if i == 1: return (x & y) | (~x & z)
        if i == 2: return (x | ~y) ^ z
        if i == 3: return (x & z) | (y & ~z)
        return x ^ (y | ~z)
    def _rol(x, n):
        return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF
    K_L = [0x00000000, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E]
    K_R = [0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0x00000000]
    R_L = [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,
           7,4,13,1,10,6,15,3,12,0,9,5,2,14,11,8,
           3,10,14,4,9,15,8,1,2,7,0,6,13,11,5,12,
           1,9,11,10,0,8,12,4,13,3,7,15,14,5,6,2,
           4,0,5,9,7,12,2,10,14,1,3,8,11,6,15,13]
    R_R = [5,14,7,0,9,2,11,4,13,6,15,8,1,10,3,12,
           6,11,3,7,0,13,5,10,14,15,8,12,4,9,1,2,
           15,5,1,3,7,14,6,9,11,8,12,2,10,0,4,13,
           8,6,4,1,3,11,15,0,5,12,2,13,9,7,10,14,
           12,15,10,4,1,5,8,7,6,2,13,14,0,3,9,11]
    S_L = [11,14,15,12,5,8,7,9,11,13,14,15,6,7,9,8,
           7,6,8,13,11,9,7,15,7,12,15,9,11,7,13,12,
           11,13,6,7,14,9,13,15,14,8,13,6,5,12,7,5,
           11,12,14,15,14,15,9,8,9,14,5,6,8,6,5,12,
           9,15,5,11,6,8,13,12,5,12,13,14,11,8,5,6]
    S_R = [8,9,9,11,13,15,15,5,7,7,8,11,14,14,12,6,
           9,13,15,7,12,8,9,11,7,7,12,7,6,15,13,11,
           9,7,15,11,8,6,6,14,12,13,5,14,13,13,7,5,
           15,5,8,11,14,14,6,14,6,9,12,9,12,5,15,8,
           8,5,12,9,12,5,14,6,8,13,6,5,15,13,11,11]
    msg = bytearray(data)
    orig_len = len(msg)
    msg.append(0x80)
    while len(msg) % 64 != 56:
        msg.append(0)
    msg += (orig_len * 8).to_bytes(8, 'little')
    h0,h1,h2,h3,h4 = 0x67452301,0xEFCDAB89,0x98BADCFE,0x10325476,0xC3D2E1F0
    M = 0xFFFFFFFF
    for i in range(0, len(msg), 64):
        X = [int.from_bytes(msg[i+j:i+j+4], 'little') for j in range(0, 64, 4)]
        al,bl,cl,dl,el = h0,h1,h2,h3,h4
        ar,br,cr,dr,er = h0,h1,h2,h3,h4
        for j in range(80):
            rnd = j // 16
            t = (al + _f(bl,cl,dl,rnd) + X[R_L[j]] + K_L[rnd]) & M
            t = (_rol(t, S_L[j]) + el) & M
            al=el; el=dl; dl=_rol(cl,10); cl=bl; bl=t
            rnd_r = j // 16
            t = (ar + _f(br,cr,dr,4-rnd_r) + X[R_R[j]] + K_R[rnd_r]) & M
            t = (_rol(t, S_R[j]) + er) & M
            ar=er; er=dr; dr=_rol(cr,10); cr=br; br=t
        t = (h1 + cl + dr) & M
        h1 = (h2 + dl + er) & M
        h2 = (h3 + el + ar) & M
        h3 = (h4 + al + br) & M
        h4 = (h0 + bl + cr) & M
        h0 = t
    return b''.join(v.to_bytes(4,'little') for v in [h0,h1,h2,h3,h4])


def _hash160(data: bytes) -> bytes:
    return _ripemd160(_sha256(data))


def _base58_encode(payload: bytes) -> str:
    n = int.from_bytes(payload, "big")
    result = ""
    while n > 0:
        n, r = divmod(n, 58)
        result = BASE58_ALPHABET[r:r+1].decode() + result
    for b in payload:
        if b == 0:
            result = "1" + result
        else:
            break
    return result


def _base58check_encode(payload: bytes) -> str:
    checksum = _hash256(payload)[:4]
    return _base58_encode(payload + checksum)


def _base58_decode(s: str) -> bytes:
    n = 0
    for ch in s:
        idx = BASE58_ALPHABET.index(ch.encode())
        n = n * 58 + idx
    result = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = 0
    for ch in s:
        if ch == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + result


def _base58check_decode(s: str) -> bytes:
    raw = _base58_decode(s)
    payload, checksum = raw[:-4], raw[-4:]
    if _hash256(payload)[:4] != checksum:
        raise ValueError("Invalid base58check checksum")
    return payload


def _privkey_to_pubkey(privkey_bytes: bytes) -> bytes:
    """Derive compressed public key from 32-byte private key using ecdsa."""
    try:
        from ecdsa import SigningKey, SECP256k1
        sk = SigningKey.from_string(privkey_bytes, curve=SECP256k1)
        vk = sk.get_verifying_key()
        x = vk.pubkey.point.x()
        y = vk.pubkey.point.y()
        prefix = b"\x02" if y % 2 == 0 else b"\x03"
        return prefix + x.to_bytes(32, "big")
    except ImportError:
        raise ImportError("ecdsa package required for key generation")


def generate_bsv_wallet(mode: str) -> dict:
    """Generate a new BSV wallet (privkey + address) for the given mode."""
    is_testnet = mode == "bsvtestnet"
    pubkey_prefix = 0x6f if is_testnet else 0x00
    wif_prefix = 0xef if is_testnet else 0x80

    privkey_bytes = secrets.token_bytes(32)
    pubkey = _privkey_to_pubkey(privkey_bytes)
    h160 = _hash160(pubkey)

    # Address
    address = _base58check_encode(bytes([pubkey_prefix]) + h160)

    # WIF (compressed)
    wif = _base58check_encode(bytes([wif_prefix]) + privkey_bytes + b"\x01")

    return {
        "address": address,
        "wif": wif,
        "privkey_bytes": privkey_bytes,
        "pubkey": pubkey,
        "hash160": h160,
    }


def import_bsv_wallet(wif: str) -> dict:
    """Import a BSV wallet from WIF private key."""
    raw = _base58check_decode(wif)
    prefix = raw[0]

    if prefix == 0x80:
        mode = "bsvmainnet"
        pubkey_prefix = 0x00
    elif prefix == 0xef:
        mode = "bsvtestnet"
        pubkey_prefix = 0x6f
    else:
        raise ValueError(f"Unknown WIF prefix: 0x{prefix:02x}")

    # Handle compressed (34 bytes: prefix + 32 + 0x01) or uncompressed (33 bytes)
    if len(raw) == 34 and raw[-1] == 0x01:
        privkey_bytes = raw[1:33]
    elif len(raw) == 33:
        privkey_bytes = raw[1:33]
    else:
        raise ValueError(f"Invalid WIF key length: {len(raw)}")

    pubkey = _privkey_to_pubkey(privkey_bytes)
    h160 = _hash160(pubkey)
    address = _base58check_encode(bytes([pubkey_prefix]) + h160)

    return {
        "address": address,
        "wif": wif,
        "privkey_bytes": privkey_bytes,
        "pubkey": pubkey,
        "hash160": h160,
        "mode": mode,
    }


async def check_bsv_balance(address: str, mode: str) -> dict:
    """Check BSV balance via WoC API."""
    if mode == "bsvtestnet":
        api_base = "https://api.whatsonchain.com/v1/bsv/test"
    else:
        api_base = "https://api.whatsonchain.com/v1/bsv/main"

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{api_base}/address/{address}/balance")
        resp.raise_for_status()
        data = resp.json()

    confirmed = data.get("confirmed", 0)
    unconfirmed = data.get("unconfirmed", 0)
    total_satoshis = confirmed + unconfirmed
    total_bsv = total_satoshis / 1e8

    return {
        "balance_satoshis": total_satoshis,
        "balance_bsv": total_bsv,
        "confirmed": confirmed,
        "unconfirmed": unconfirmed,
        "funded": total_satoshis > 0,
    }


class CannonState:
    """Per-connection state for the STAS Cannon simulation."""
    def __init__(self):
        self.phase = "idle"
        self.total_transfers = 1_000_000
        self.utxos_prepared = 0
        self.tx_built = 0
        self.tx_broadcast = 0
        self.tx_confirmed = 0
        self.tx_errors = 0
        self.tps = 0.0
        self.start_time = 0.0
        self.charge_duration = 0.0
        self.build_duration = 0.0
        self.broadcast_duration = 0.0
        self.confirm_duration = 0.0
        self.total_duration = 0.0
        self.sender_address = ""
        self.receiver_address = ""
        self.running = False
        self.tx_ids: list[str] = []
        self.mode = "localtest"
        self.wallet: dict | None = None


def generate_address():
    """Generate a simulated BSV address."""
    raw = secrets.token_bytes(20)
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    addr = "1"
    num = int.from_bytes(raw, "big")
    while num > 0:
        num, rem = divmod(num, 58)
        addr += alphabet[rem]
    return addr[:34]


def generate_txid():
    """Generate a simulated BSV transaction ID (64-char hex)."""
    return secrets.token_hex(32)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# Serve frontend static files
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
if os.path.isdir(STATIC_DIR):
    # Serve static assets
    assets_dir = os.path.join(STATIC_DIR, "assets")
    if os.path.isdir(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/")
    @app.get("/localtest")
    @app.get("/bsvtestnet")
    @app.get("/bsvmainnet")
    async def serve_index():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.websocket("/ws/cannon")
async def websocket_cannon(websocket: WebSocket, mode: str = Query(default="localtest")):
    await websocket.accept()
    st = CannonState()
    st.mode = mode
    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            action = msg.get("action")

            if action == "create_wallet":
                if mode == "localtest":
                    await websocket.send_json({"type": "wallet_error", "message": "ローカルテストモードではウォレット不要です"})
                    continue
                try:
                    wallet_data = generate_bsv_wallet(mode)
                    st.wallet = wallet_data
                    balance = await check_bsv_balance(wallet_data["address"], mode)
                    await websocket.send_json({
                        "type": "wallet_created",
                        "address": wallet_data["address"],
                        "wif": wallet_data["wif"],
                        "balance_bsv": balance["balance_bsv"],
                        "balance_satoshis": balance["balance_satoshis"],
                        "funded": balance["funded"],
                    })
                except Exception as e:
                    await websocket.send_json({"type": "wallet_error", "message": str(e)})

            elif action == "import_wallet":
                if mode == "localtest":
                    await websocket.send_json({"type": "wallet_error", "message": "ローカルテストモードではウォレット不要です"})
                    continue
                wif = msg.get("wif", "")
                if not wif:
                    await websocket.send_json({"type": "wallet_error", "message": "WIFキーが必要です"})
                    continue
                try:
                    wallet_data = import_bsv_wallet(wif)
                    st.wallet = wallet_data
                    balance = await check_bsv_balance(wallet_data["address"], mode)
                    await websocket.send_json({
                        "type": "wallet_created",
                        "address": wallet_data["address"],
                        "balance_bsv": balance["balance_bsv"],
                        "balance_satoshis": balance["balance_satoshis"],
                        "funded": balance["funded"],
                    })
                except Exception as e:
                    await websocket.send_json({"type": "wallet_error", "message": str(e)})

            elif action == "check_balance":
                if not st.wallet:
                    await websocket.send_json({"type": "wallet_error", "message": "ウォレットが設定されていません"})
                    continue
                try:
                    balance = await check_bsv_balance(st.wallet["address"], mode)
                    await websocket.send_json({
                        "type": "wallet_balance",
                        "address": st.wallet["address"],
                        "balance_bsv": balance["balance_bsv"],
                        "balance_satoshis": balance["balance_satoshis"],
                        "funded": balance["funded"],
                    })
                except Exception as e:
                    await websocket.send_json({"type": "wallet_error", "message": str(e)})

            elif action == "configure":
                raw = msg.get("total_transfers")
                total = int(raw) if raw and int(raw) > 0 else 1_000_000
                prev_wallet = st.wallet
                prev_mode = st.mode
                st = CannonState()
                st.wallet = prev_wallet
                st.mode = prev_mode
                st.total_transfers = total
                st.sender_address = st.wallet["address"] if st.wallet else generate_address()
                st.receiver_address = generate_address()
                await websocket.send_json({
                    "type": "configured",
                    "total_transfers": st.total_transfers,
                    "sender_address": st.sender_address,
                    "receiver_address": st.receiver_address,
                })

            elif action == "charge":
                if st.running:
                    await websocket.send_json({"type": "error", "message": "Already running"})
                    continue
                st.running = True
                charge_start = time.time()
                await run_power_charge(websocket, st)
                st.charge_duration = time.time() - charge_start
                st.running = False

            elif action == "launch":
                if st.running:
                    await websocket.send_json({"type": "error", "message": "Already running"})
                    continue
                st.running = True
                await run_launch(websocket, st)
                st.running = False

            elif action == "confirm":
                if st.running:
                    await websocket.send_json({"type": "error", "message": "Already running"})
                    continue
                st.running = True
                confirm_start = time.time()
                await run_confirm(websocket, st)
                st.confirm_duration = time.time() - confirm_start
                st.total_duration = st.charge_duration + st.build_duration + st.broadcast_duration + st.confirm_duration
                st.running = False
                st.phase = "done"
                await websocket.send_json({
                    "type": "complete",
                    "total_duration": round(st.total_duration, 2),
                    "tx_broadcast": st.tx_broadcast,
                    "tx_confirmed": st.tx_confirmed,
                    "tx_errors": st.tx_errors,
                    "tps": round(st.tps, 0),
                    "build_duration": round(st.build_duration, 2),
                    "broadcast_duration": round(st.broadcast_duration, 2),
                    "tx_ids": st.tx_ids,
                })

            elif action == "stop":
                st.running = False
                st.phase = "idle"
                await websocket.send_json({"type": "stopped"})

    except WebSocketDisconnect:
        st.running = False


async def run_power_charge(ws: WebSocket, st: CannonState):
    """Phase 1: Prepare UTXOs through hierarchical splitting."""
    st.phase = "power_charge"
    total = st.total_transfers
    await ws.send_json({"type": "phase", "phase": "power_charge", "total": total})

    steps = 100
    batch_size = max(1, total // steps)
    prepared = 0

    for _ in range(steps + 1):
        if not st.running:
            return
        chunk = min(batch_size, total - prepared)
        if chunk <= 0:
            break
        await asyncio.sleep(0.01)
        prepared += chunk
        st.utxos_prepared = prepared
        await ws.send_json({
            "type": "progress",
            "phase": "power_charge",
            "current": prepared,
            "total": total,
            "percent": round(prepared / total * 100, 1),
        })

    if st.running:
        st.utxos_prepared = total
        await ws.send_json({
            "type": "phase_complete",
            "phase": "power_charge",
            "utxos_prepared": total,
        })


async def run_launch(ws: WebSocket, st: CannonState):
    """Phase 2: Build and broadcast transactions at high speed."""
    st.phase = "launch"
    total = st.total_transfers
    await ws.send_json({"type": "phase", "phase": "launch", "total": total})

    # Phase 2a: Build transactions
    build_start = time.time()
    steps = 50
    batch_size = max(1, total // steps)
    built = 0

    for _ in range(steps + 1):
        if not st.running:
            return
        chunk = min(batch_size, total - built)
        if chunk <= 0:
            break
        await asyncio.sleep(0.005)
        built += chunk
        st.tx_built = built
        await ws.send_json({
            "type": "progress",
            "phase": "build",
            "current": built,
            "total": total,
            "percent": round(built / total * 100, 1),
        })

    st.tx_built = total
    st.build_duration = time.time() - build_start

    if not st.running:
        return

    # Phase 2b: Broadcast transactions (simulates ~10s of broadcasting)
    broadcast_start = time.time()
    steps = 100
    broadcast_batch = max(1, total // steps)
    broadcast = 0
    target_duration = 10.0
    interval = target_duration / max(1, steps)

    await ws.send_json({"type": "phase", "phase": "broadcast", "total": total})

    for _ in range(steps + 1):
        if not st.running:
            return
        chunk = min(broadcast_batch, total - broadcast)
        if chunk <= 0:
            break
        await asyncio.sleep(interval)
        broadcast += chunk
        st.tx_broadcast = broadcast

        elapsed = time.time() - broadcast_start
        st.tps = broadcast / elapsed if elapsed > 0 else 0

        errors = int(broadcast * 0.0001)
        st.tx_errors = errors

        await ws.send_json({
            "type": "progress",
            "phase": "broadcast",
            "current": broadcast,
            "total": total,
            "percent": round(broadcast / total * 100, 1),
            "tps": round(st.tps, 0),
            "errors": errors,
        })

    st.tx_broadcast = total
    st.broadcast_duration = time.time() - broadcast_start

    # Generate simulated transaction IDs
    st.tx_ids = [generate_txid() for _ in range(min(total, 100))]

    if st.running:
        avg_tps = round(st.tx_broadcast / st.broadcast_duration, 0) if st.broadcast_duration > 0 else 0
        st.tps = avg_tps
        await ws.send_json({
            "type": "phase_complete",
            "phase": "launch",
            "tx_built": st.tx_built,
            "tx_broadcast": st.tx_broadcast,
            "build_duration": round(st.build_duration, 2),
            "broadcast_duration": round(st.broadcast_duration, 2),
            "avg_tps": avg_tps,
        })


async def run_confirm(ws: WebSocket, st: CannonState):
    """Phase 3: Verify transferred transactions."""
    st.phase = "confirm"
    total = st.tx_broadcast if st.tx_broadcast > 0 else st.total_transfers
    await ws.send_json({"type": "phase", "phase": "confirm", "total": total})

    steps = 50
    batch_size = max(1, total // steps)
    confirmed = 0

    for _ in range(steps + 1):
        if not st.running:
            return
        chunk = min(batch_size, total - confirmed)
        if chunk <= 0:
            break
        await asyncio.sleep(0.02)
        confirmed += chunk
        st.tx_confirmed = confirmed
        await ws.send_json({
            "type": "progress",
            "phase": "confirm",
            "current": confirmed,
            "total": total,
            "percent": round(confirmed / total * 100, 1),
        })

    st.tx_confirmed = total
    if st.running:
        await ws.send_json({
            "type": "phase_complete",
            "phase": "confirm",
            "tx_confirmed": st.tx_confirmed,
            "total_yen": f"{st.tx_confirmed:,}",
        })
