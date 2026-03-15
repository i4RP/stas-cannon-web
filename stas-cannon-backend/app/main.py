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


# --- STAS Service Integration ---

STAS_SERVICE_URL = os.environ.get("STAS_SERVICE_URL", "http://localhost:3001")


async def stas_service_call(endpoint: str, payload: dict) -> dict:
    """Call the Node.js STAS service."""
    import logging
    logger = logging.getLogger("stas_service")
    logger.info(f"STAS call: POST {endpoint}")
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(f"{STAS_SERVICE_URL}{endpoint}", json=payload)
        logger.info(f"STAS response: {resp.status_code} ({len(resp.content)} bytes)")
        data = resp.json()
        if resp.status_code != 200:
            raise RuntimeError(data.get("error", "STAS service error"))
        return data


async def woc_get_utxos(address: str, mode: str) -> list[dict]:
    """Get UTXOs for an address via WoC API."""
    api_base = "https://api.whatsonchain.com/v1/bsv/test" if mode == "bsvtestnet" else "https://api.whatsonchain.com/v1/bsv/main"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{api_base}/address/{address}/unspent")
        resp.raise_for_status()
        return resp.json()


async def woc_get_tx_hex(txid: str, mode: str) -> str:
    """Get raw transaction hex via WoC API."""
    api_base = "https://api.whatsonchain.com/v1/bsv/test" if mode == "bsvtestnet" else "https://api.whatsonchain.com/v1/bsv/main"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{api_base}/tx/{txid}/hex")
        resp.raise_for_status()
        return resp.text


async def woc_broadcast_tx(tx_hex: str, mode: str) -> str:
    """Broadcast a raw transaction via WoC API. Returns txid."""
    api_base = "https://api.whatsonchain.com/v1/bsv/test" if mode == "bsvtestnet" else "https://api.whatsonchain.com/v1/bsv/main"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{api_base}/tx/raw", json={"txhex": tx_hex})
        if resp.status_code != 200:
            raise RuntimeError(f"Broadcast failed: {resp.text}")
        return resp.text.strip().strip('"')


async def woc_get_tx_status(txid: str, mode: str) -> dict:
    """Check transaction status via WoC API."""
    api_base = "https://api.whatsonchain.com/v1/bsv/test" if mode == "bsvtestnet" else "https://api.whatsonchain.com/v1/bsv/main"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{api_base}/tx/{txid}")
        if resp.status_code == 200:
            return resp.json()
        return {"error": resp.text}


def build_utxo_info(txid: str, vout: int, satoshis: int, locking_script_hex: str, address_hash160_hex: str, script_type: str = "p2pkh") -> dict:
    """Build UTXO info dict for STAS service calls."""
    return {
        "txId": txid,
        "vout": vout,
        "satoshis": satoshis,
        "lockingScriptHex": locking_script_hex,
        "addressHash160": address_hash160_hex,
        "scriptType": script_type,
    }


class CannonState:
    """Per-connection state for the STAS Cannon."""
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
        # Real blockchain state
        self.token_scheme: dict | None = None
        self.issue_txid: str = ""
        self.stas_utxos: list[dict] = []  # Available STAS UTXOs for transfer
        self.fee_utxos: list[dict] = []   # Available fee UTXOs
        self.receiver_hash160: str = ""


def generate_address(mode: str = "localtest"):
    """Generate a simulated BSV address with correct network prefix."""
    raw = secrets.token_bytes(20)
    # Use proper version byte for each network
    if mode in ("bsvtestnet",):
        version = 0x6f  # testnet: m or n prefix
    else:
        version = 0x00  # mainnet: 1 prefix
    return _base58check_encode(version, raw)


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
                mode_defaults = {"localtest": 1_000_000, "bsvtestnet": 10, "bsvmainnet": 1_000_000}
                mode_max = {"localtest": 1_000_000_000, "bsvtestnet": 1_000_000_000, "bsvmainnet": 1_000_000_000}
                default_total = mode_defaults.get(mode, 1_000_000)
                max_total = mode_max.get(mode, 1_000_000)
                total = min(int(raw), max_total) if raw and int(raw) > 0 else default_total
                prev_wallet = st.wallet
                prev_mode = st.mode
                st = CannonState()
                st.wallet = prev_wallet
                st.mode = prev_mode
                st.total_transfers = total
                st.sender_address = st.wallet["address"] if st.wallet else generate_address(mode)
                # For real modes, receiver = sender (self-transfer for token recycling)
                # For localtest, generate a random receiver
                if mode in ("bsvtestnet", "bsvmainnet") and st.wallet:
                    st.receiver_address = st.wallet["address"]
                else:
                    st.receiver_address = generate_address(mode)
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
    if st.mode != "localtest":
        await run_real_power_charge(ws, st)
        return

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
    if st.mode != "localtest":
        await run_real_launch(ws, st)
        return

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
    if st.mode != "localtest":
        await run_real_confirm(ws, st)
        return

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


# =============================================================================
# Real Blockchain Operations (testnet/mainnet)
# =============================================================================


async def run_real_power_charge(ws: WebSocket, st: CannonState):
    """Real Phase 1: Prepare STAS tokens for transfer.

    Optimizations:
    1. Token recycling: Reuse existing DSTAS UTXOs from previous runs
    2. Direct multi-output issuance: Issue N individual 1-sat tokens (no splits needed)
    3. Lower fee rate: 0.05 sat/byte instead of 0.1 (BSV accepts this)
    4. Self-transfer: Send to own wallet so tokens can be reused next time
    """
    st.phase = "power_charge"
    total = st.total_transfers
    mode = st.mode
    wallet = st.wallet
    FEE_RATE = 0.05 if mode == "bsvmainnet" else 0.02  # mainnet: safe rate, testnet: aggressive

    if not wallet:
        await ws.send_json({"type": "error", "message": "ウォレットが設定されていません"})
        return

    privkey_hex = wallet["privkey_bytes"].hex()
    address_hash160 = wallet["hash160"].hex()

    await ws.send_json({"type": "phase", "phase": "power_charge", "total": total})
    await ws.send_json({"type": "progress", "phase": "power_charge", "current": 0, "total": total, "percent": 0, "status": "UTXOを取得中..."})

    # Step 1: Get wallet UTXOs
    try:
        raw_utxos = await woc_get_utxos(wallet["address"], mode)
    except Exception as e:
        await ws.send_json({"type": "error", "message": f"UTXO取得失敗: {e}"})
        return

    if not raw_utxos:
        await ws.send_json({"type": "error", "message": "UTXOがありません。BSVを送金してください。"})
        return

    # Step 2: Parse UTXOs - collect both P2PKH (for fees) and DSTAS (for recycling)
    sorted_utxos = sorted(raw_utxos, key=lambda u: u.get("value", 0), reverse=True)
    p2pkh_utxos: list[dict] = []
    dstas_1sat_utxos: list[dict] = []  # Existing 1-sat DSTAS tokens for recycling

    for idx, candidate in enumerate(sorted_utxos):
        try:
            await ws.send_json({"type": "progress", "phase": "power_charge", "current": 0, "total": total, "percent": 1, "status": f"UTXO検証中... ({idx+1}/{len(sorted_utxos)})"})
            tx_hex = await woc_get_tx_hex(candidate["tx_hash"], mode)
            tx_info = await stas_service_call("/parse-tx", {"txHex": tx_hex})
            output = tx_info["outputs"][candidate["tx_pos"]]
            if output.get("scriptType") == "p2pkh":
                p2pkh_utxos.append({
                    "raw": candidate,
                    "txid": candidate["tx_hash"],
                    "vout": candidate["tx_pos"],
                    "satoshis": candidate["value"],
                    "locking_script": output["lockingScriptHex"],
                })
            elif output.get("scriptType") == "dstas" and candidate["value"] == 1:
                dstas_1sat_utxos.append({
                    "txId": candidate["tx_hash"],
                    "vout": candidate["tx_pos"],
                    "satoshis": 1,
                    "lockingScriptHex": output["lockingScriptHex"],
                    "addressHash160": output.get("addressHash160", address_hash160),
                    "scriptType": "dstas",
                })
        except Exception as e:
            print(f"[CHARGE] UTXO parse error for {candidate.get('tx_hash', '?')}:{candidate.get('tx_pos', '?')}: {e}")
            continue

    print(f"[CHARGE] Found {len(p2pkh_utxos)} P2PKH UTXOs, {len(dstas_1sat_utxos)} recyclable DSTAS UTXOs")

    token_scheme = {
        "name": "STAS",
        "tokenId": address_hash160,
        "symbol": "STAS",
        "satoshisPerToken": 1,
        "freeze": False,
        "confiscation": False,
        "isDivisible": False,
    }
    st.token_scheme = token_scheme

    # =========================================================================
    # Optimization 1: Token recycling - reuse existing DSTAS UTXOs
    # =========================================================================
    if len(dstas_1sat_utxos) >= total:
        print(f"[CHARGE] Recycling {total} existing DSTAS tokens (skipping issuance)")
        await ws.send_json({"type": "progress", "phase": "power_charge", "current": total, "total": total, "percent": 90, "status": f"既存トークン再利用: {total}個 (発行スキップ)"})

        final_stas = dstas_1sat_utxos[:total]
        fee_pool = []
        for u in p2pkh_utxos:
            fee_pool.append({
                "txId": u["txid"],
                "vout": u["vout"],
                "satoshis": u["satoshis"],
                "lockingScriptHex": u["locking_script"],
                "addressHash160": address_hash160,
                "scriptType": "p2pkh",
            })

        st.issue_txid = "recycled"

    else:
        # =====================================================================
        # Optimization 2: Direct multi-output issuance (no splits needed)
        # Issue N individual 1-sat DSTAS UTXOs directly
        # =====================================================================
        existing_count = len(dstas_1sat_utxos)
        new_count = total - existing_count

        if not p2pkh_utxos:
            await ws.send_json({"type": "error", "message": "利用可能なP2PKH UTXOが見つかりません。BSVを送金してください。"})
            return

        total_p2pkh_sats = sum(u["satoshis"] for u in p2pkh_utxos)
        # Estimate fee: each DSTAS output ~2918 bytes, at FEE_RATE
        estimated_issue_fee = int((new_count * 2918 + 1000) * FEE_RATE) + 500
        min_required = new_count + estimated_issue_fee

        if total_p2pkh_sats < min_required:
            max_affordable = int((total_p2pkh_sats - 500) / (1 + 2918 * FEE_RATE))
            max_affordable = max(0, max_affordable) + existing_count
            await ws.send_json({"type": "error", "message": f"資金不足: {total_p2pkh_sats} sats < 必要量 ~{min_required} sats。現在の残高で最大 {max_affordable} 件のテストが可能です。"})
            return

        # Consolidate if needed
        funding_txid = p2pkh_utxos[0]["txid"]
        funding_vout = p2pkh_utxos[0]["vout"]
        funding_satoshis = p2pkh_utxos[0]["satoshis"]
        funding_locking_script = p2pkh_utxos[0]["locking_script"]

        if funding_satoshis < min_required and len(p2pkh_utxos) > 1:
            await ws.send_json({"type": "progress", "phase": "power_charge", "current": 0, "total": total, "percent": 2, "status": f"UTXO統合中... ({len(p2pkh_utxos)}個 → 1個, {total_p2pkh_sats} sats)"})
            try:
                consolidate_inputs = [
                    build_utxo_info(u["txid"], u["vout"], u["satoshis"], u["locking_script"], address_hash160)
                    for u in p2pkh_utxos
                ]
                consolidate_result = await stas_service_call("/consolidate", {
                    "privkeyHex": privkey_hex,
                    "utxos": consolidate_inputs,
                    "feeRate": FEE_RATE,
                })
                consolidate_txid = await woc_broadcast_tx(consolidate_result["txHex"], mode)
                print(f"[CHARGE] Consolidation TX broadcast: {consolidate_txid}")
                consolidated_output = consolidate_result["outputs"][0]
                funding_txid = consolidate_result["txId"]
                funding_vout = 0
                funding_satoshis = consolidated_output["satoshis"]
                funding_locking_script = consolidated_output["lockingScriptHex"]
                p2pkh_utxos = []  # All merged
            except Exception as e:
                print(f"[CHARGE] Consolidation failed: {e}")
                await ws.send_json({"type": "error", "message": f"UTXO統合失敗: {e}"})
                return

        # =====================================================================
        # Batched issuance: for large counts, issue in batches
        # =====================================================================
        MAX_ISSUE_BATCH = 500  # Max tokens per issue TX
        final_stas = list(dstas_1sat_utxos)  # Start with recycled tokens
        fee_pool = []
        remaining_to_issue = new_count
        batch_num = 0
        total_batches = (new_count + MAX_ISSUE_BATCH - 1) // MAX_ISSUE_BATCH

        # Track current funding UTXO (chains across batches)
        cur_funding_txid = funding_txid
        cur_funding_vout = funding_vout
        cur_funding_sats = funding_satoshis
        cur_funding_script = funding_locking_script

        # Add unused P2PKH UTXOs to fee pool (if no consolidation happened)
        if funding_satoshis >= min_required:
            for u in p2pkh_utxos[1:]:
                fee_pool.append({
                    "txId": u["txid"],
                    "vout": u["vout"],
                    "satoshis": u["satoshis"],
                    "lockingScriptHex": u["locking_script"],
                    "addressHash160": address_hash160,
                    "scriptType": "p2pkh",
                })

        while remaining_to_issue > 0 and st.running:
            batch_count = min(remaining_to_issue, MAX_ISSUE_BATCH)
            batch_num += 1
            pct = 5 + int(85 * (batch_num / total_batches))

            await ws.send_json({"type": "progress", "phase": "power_charge", "current": len(final_stas) - existing_count, "total": total, "percent": pct, "status": f"バッチ {batch_num}/{total_batches}: {batch_count}トークン発行中..."})

            destinations = [{"satoshis": 1, "toHash160": address_hash160} for _ in range(batch_count)]

            try:
                print(f"[CHARGE] Batch {batch_num}/{total_batches}: issuing {batch_count} tokens, funding={cur_funding_sats} sats")
                issue_result = await stas_service_call("/issue", {
                    "privkeyHex": privkey_hex,
                    "fundingUtxo": build_utxo_info(cur_funding_txid, cur_funding_vout, cur_funding_sats, cur_funding_script, address_hash160),
                    "scheme": token_scheme,
                    "destinations": destinations,
                    "feeRate": FEE_RATE,
                })
            except Exception as e:
                print(f"[CHARGE] Issue batch {batch_num} failed: {e}")
                await ws.send_json({"type": "error", "message": f"トークン発行失敗 (バッチ {batch_num}): {e}"})
                return

            # Broadcast Contract TX
            try:
                contract_txid = await woc_broadcast_tx(issue_result["contractTxHex"], mode)
                print(f"[CHARGE] Batch {batch_num} Contract TX: {contract_txid}")
            except Exception as e:
                await ws.send_json({"type": "error", "message": f"Contract TXブロードキャスト失敗 (バッチ {batch_num}): {e}"})
                return

            # Broadcast Issue TX
            try:
                issue_txid = await woc_broadcast_tx(issue_result["issueTxHex"], mode)
                st.issue_txid = issue_txid
                print(f"[CHARGE] Batch {batch_num} Issue TX: {issue_txid}")
            except Exception as e:
                await ws.send_json({"type": "error", "message": f"Issue TXブロードキャスト失敗 (バッチ {batch_num}): {e}"})
                return

            # Collect outputs from this batch
            issue_outputs = issue_result["issueOutputs"]
            for out in issue_outputs:
                if out["scriptType"] == "dstas" and out["satoshis"] == 1:
                    final_stas.append({
                        "txId": issue_txid,
                        "vout": out["vout"],
                        "satoshis": 1,
                        "lockingScriptHex": out["lockingScriptHex"],
                        "addressHash160": out.get("addressHash160", address_hash160),
                        "scriptType": "dstas",
                    })
                elif out["scriptType"] == "p2pkh" and out["satoshis"] > 0:
                    # Chain: use this P2PKH change as funding for next batch
                    cur_funding_txid = issue_txid
                    cur_funding_vout = out["vout"]
                    cur_funding_sats = out["satoshis"]
                    cur_funding_script = out["lockingScriptHex"]

            remaining_to_issue -= batch_count

        # Last batch's fee change goes to fee pool
        if cur_funding_sats > 0 and cur_funding_txid == st.issue_txid:
            fee_pool.append({
                "txId": cur_funding_txid,
                "vout": cur_funding_vout,
                "satoshis": cur_funding_sats,
                "lockingScriptHex": cur_funding_script,
                "addressHash160": address_hash160,
                "scriptType": "p2pkh",
            })

        print(f"[CHARGE] Issued {len(final_stas) - existing_count} new + {existing_count} recycled = {len(final_stas)} tokens, {len(fee_pool)} fee UTXOs")

    # Store results - no split phase needed!
    st.stas_utxos = final_stas[:total]
    st.fee_utxos = fee_pool
    st.utxos_prepared = len(st.stas_utxos)

    # Optimization 4: Self-transfer - tokens stay in wallet for reuse next time
    st.receiver_address = wallet["address"]
    st.receiver_hash160 = address_hash160

    total_fee_sats = sum(u["satoshis"] for u in fee_pool)
    print(f"[CHARGE] Final: {st.utxos_prepared} STAS UTXOs, {len(fee_pool)} fee UTXOs ({total_fee_sats} sats)")

    if st.running:
        recycled = st.issue_txid == "recycled"
        await ws.send_json({
            "type": "phase_complete",
            "phase": "power_charge",
            "utxos_prepared": st.utxos_prepared,
            "issue_txid": st.issue_txid,
            "recycled": recycled,
            "fee_budget": total_fee_sats,
        })


async def run_real_launch(ws: WebSocket, st: CannonState):
    """Real Phase 2: Build and broadcast STAS transfer transactions.

    For large transfer counts (>=1000), uses concurrent transfer groups:
    - Splits fee UTXOs into N groups via a split TX
    - Each group processes transfers sequentially (chaining fee change)
    - Groups run in parallel with asyncio.gather for ~Nx speedup
    """
    st.phase = "launch"
    total = len(st.stas_utxos)
    if total == 0:
        await ws.send_json({"type": "error", "message": "転送可能なSTAS UTXOがありません"})
        return

    wallet = st.wallet
    if not wallet:
        await ws.send_json({"type": "error", "message": "ウォレットが設定されていません"})
        return

    privkey_hex = wallet["privkey_bytes"].hex()
    address_hash160 = wallet["hash160"].hex()
    token_scheme = st.token_scheme
    receiver_hash160 = st.receiver_hash160
    FEE_RATE = 0.05 if st.mode == "bsvmainnet" else 0.02

    await ws.send_json({"type": "phase", "phase": "launch", "total": total})

    build_start = time.time()
    await ws.send_json({"type": "phase", "phase": "build", "total": total})

    broadcast_start = time.time()
    st.tx_broadcast = 0
    st.tx_errors = 0
    st.tx_ids = []

    # Determine concurrency: use parallel groups for large counts
    NUM_GROUPS = min(10, total) if total >= 100 else 1

    if NUM_GROUPS > 1:
        # Split fee UTXOs into N groups via a fee-split TX
        # Each group needs its own fee chain
        total_fee_sats = sum(u["satoshis"] for u in st.fee_utxos)
        per_group_sats = total_fee_sats // NUM_GROUPS
        transfers_per_group = total // NUM_GROUPS

        await ws.send_json({"type": "progress", "phase": "broadcast", "current": 0, "total": total, "percent": 0, "status": f"Fee UTXO を {NUM_GROUPS} グループに分割中..."})

        # Split the main fee UTXO into N outputs
        try:
            split_result = await stas_service_call("/split-fee", {
                "privkeyHex": privkey_hex,
                "utxos": st.fee_utxos,
                "numOutputs": NUM_GROUPS,
                "feeRate": FEE_RATE,
            })
            split_txid = await woc_broadcast_tx(split_result["txHex"], st.mode)
            print(f"[LAUNCH] Fee split TX: {split_txid}, {NUM_GROUPS} groups")

            group_fee_utxos = []
            for out in split_result["outputs"]:
                if out["scriptType"] == "p2pkh" and out["satoshis"] > 0:
                    group_fee_utxos.append({
                        "txId": split_txid,
                        "vout": out["vout"],
                        "satoshis": out["satoshis"],
                        "lockingScriptHex": out["lockingScriptHex"],
                        "addressHash160": out.get("addressHash160", address_hash160),
                        "scriptType": "p2pkh",
                    })
        except Exception as e:
            print(f"[LAUNCH] Fee split failed, falling back to sequential: {e}")
            NUM_GROUPS = 1
            group_fee_utxos = []

    if NUM_GROUPS <= 1:
        # Sequential mode (original behavior)
        for i in range(0, total):
            if not st.running:
                return

            stas_utxo = st.stas_utxos[i]
            if not st.fee_utxos:
                st.tx_errors += total - st.tx_broadcast
                await ws.send_json({"type": "error", "message": f"Fee UTXO不足 ({st.tx_broadcast}/{total} completed)"})
                break

            fee_utxo = st.fee_utxos.pop(0)
            transfers = [{"stasUtxo": stas_utxo, "feeUtxo": fee_utxo, "toHash160": receiver_hash160}]

            try:
                batch_result = await stas_service_call("/batch-transfer", {
                    "privkeyHex": privkey_hex,
                    "transfers": transfers,
                    "scheme": token_scheme,
                    "feeRate": FEE_RATE,
                })

                for result in batch_result["results"]:
                    try:
                        txid = await woc_broadcast_tx(result["txHex"], st.mode)
                        st.tx_broadcast += 1
                        st.tx_ids.append(txid)
                        for out in result["outputs"]:
                            if out["scriptType"] == "p2pkh" and out["satoshis"] > 0:
                                st.fee_utxos.append({
                                    "txId": txid, "vout": out["vout"],
                                    "satoshis": out["satoshis"],
                                    "lockingScriptHex": out["lockingScriptHex"],
                                    "addressHash160": out.get("addressHash160", address_hash160),
                                    "scriptType": "p2pkh",
                                })
                    except Exception:
                        st.tx_errors += 1
            except Exception as e:
                st.tx_errors += 1

            elapsed = time.time() - broadcast_start
            st.tps = st.tx_broadcast / elapsed if elapsed > 0 else 0
            if i % max(1, total // 100) == 0 or i == total - 1:
                await ws.send_json({
                    "type": "progress", "phase": "broadcast",
                    "current": st.tx_broadcast, "total": total,
                    "percent": round(st.tx_broadcast / total * 100, 1),
                    "tps": round(st.tps, 1), "errors": st.tx_errors,
                })
    else:
        # Concurrent transfer groups
        # Divide STAS UTXOs into groups
        stas_groups = [[] for _ in range(NUM_GROUPS)]
        for idx, utxo in enumerate(st.stas_utxos):
            stas_groups[idx % NUM_GROUPS].append(utxo)

        # Shared counters (protected by lock)
        lock = asyncio.Lock()

        async def process_group(group_idx: int, stas_list: list, fee_utxo: dict):
            """Process a single transfer group sequentially."""
            current_fee = fee_utxo
            for stas_utxo in stas_list:
                if not st.running:
                    return
                transfers = [{"stasUtxo": stas_utxo, "feeUtxo": current_fee, "toHash160": receiver_hash160}]
                try:
                    batch_result = await stas_service_call("/batch-transfer", {
                        "privkeyHex": privkey_hex,
                        "transfers": transfers,
                        "scheme": token_scheme,
                        "feeRate": FEE_RATE,
                    })
                    for result in batch_result["results"]:
                        try:
                            txid = await woc_broadcast_tx(result["txHex"], st.mode)
                            async with lock:
                                st.tx_broadcast += 1
                                st.tx_ids.append(txid)
                            # Chain fee change for next transfer
                            for out in result["outputs"]:
                                if out["scriptType"] == "p2pkh" and out["satoshis"] > 0:
                                    current_fee = {
                                        "txId": txid, "vout": out["vout"],
                                        "satoshis": out["satoshis"],
                                        "lockingScriptHex": out["lockingScriptHex"],
                                        "addressHash160": out.get("addressHash160", address_hash160),
                                        "scriptType": "p2pkh",
                                    }
                                    break
                        except Exception:
                            async with lock:
                                st.tx_errors += 1
                except Exception:
                    async with lock:
                        st.tx_errors += 1

        # Progress reporter task
        async def report_progress():
            while st.running and st.tx_broadcast < total:
                elapsed = time.time() - broadcast_start
                st.tps = st.tx_broadcast / elapsed if elapsed > 0 else 0
                await ws.send_json({
                    "type": "progress", "phase": "broadcast",
                    "current": st.tx_broadcast, "total": total,
                    "percent": round(st.tx_broadcast / total * 100, 1),
                    "tps": round(st.tps, 1), "errors": st.tx_errors,
                })
                await asyncio.sleep(1.0)

        # Launch all groups + progress reporter concurrently
        tasks = []
        for g_idx in range(NUM_GROUPS):
            if g_idx < len(group_fee_utxos) and stas_groups[g_idx]:
                tasks.append(process_group(g_idx, stas_groups[g_idx], group_fee_utxos[g_idx]))
        tasks.append(report_progress())

        await asyncio.gather(*tasks)

    st.build_duration = time.time() - build_start
    st.broadcast_duration = time.time() - broadcast_start

    if st.running:
        avg_tps = round(st.tx_broadcast / st.broadcast_duration, 1) if st.broadcast_duration > 0 else 0
        st.tps = avg_tps
        await ws.send_json({
            "type": "phase_complete",
            "phase": "launch",
            "tx_built": st.tx_broadcast,
            "tx_broadcast": st.tx_broadcast,
            "build_duration": round(st.build_duration, 2),
            "broadcast_duration": round(st.broadcast_duration, 2),
            "avg_tps": avg_tps,
        })


async def run_real_confirm(ws: WebSocket, st: CannonState):
    """Real Phase 3: Verify transactions on-chain with concurrency."""
    st.phase = "confirm"
    total = len(st.tx_ids)
    if total == 0:
        await ws.send_json({"type": "error", "message": "確認するトランザクションがありません"})
        return

    await ws.send_json({"type": "phase", "phase": "confirm", "total": total})

    confirmed = 0
    checked = 0
    lock = asyncio.Lock()

    CONCURRENCY = min(20, total)
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def check_tx(txid: str):
        nonlocal confirmed, checked
        async with semaphore:
            try:
                tx_status = await woc_get_tx_status(txid, st.mode)
                if "error" not in tx_status:
                    async with lock:
                        confirmed += 1
            except Exception:
                pass
            async with lock:
                checked += 1

    # Launch all checks concurrently (bounded by semaphore)
    tasks = [check_tx(txid) for txid in st.tx_ids]

    # Progress reporter
    async def report_progress():
        while checked < total:
            await ws.send_json({
                "type": "progress", "phase": "confirm",
                "current": checked, "total": total,
                "percent": round(checked / total * 100, 1),
            })
            await asyncio.sleep(1.0)

    await asyncio.gather(asyncio.gather(*tasks), report_progress())

    st.tx_confirmed = confirmed
    if st.running:
        await ws.send_json({
            "type": "phase_complete",
            "phase": "confirm",
            "tx_confirmed": confirmed,
            "total_yen": f"{confirmed:,}",
        })
