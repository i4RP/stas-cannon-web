from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
import asyncio
import os
import secrets
import time
import json
import hashlib
import logging
import httpx

logger = logging.getLogger("stas_cannon")

# Fixed testnet wallet WIF for background UTXO splitting (loaded from env)
TESTNET_WIF = os.environ.get("TESTNET_WIF", "")

# JPYS Net (regtest) RPC configuration
JPYSNET_RPC_URL = os.environ.get("JPYSNET_RPC_URL", "http://127.0.0.1:19445")
JPYSNET_RPC_USER = os.environ.get("JPYSNET_RPC_USER", "jpysnet")
JPYSNET_RPC_PASS = os.environ.get("JPYSNET_RPC_PASS", "jpysnet2026")
JPYSNET_DEFAULT_WIF = os.environ.get("JPYSNET_WIF", "cVTwjdCeFchwVsYmswyDMe1x4fFJT9xhWBvJbYydgUGqJLYwpp81")

# Local UTXO tracking: Bitails address/unspent endpoint is too slow from EC2
# for addresses with many UTXOs. Track known UTXOs locally instead.
_tracked_utxos: dict[str, list[dict]] = {}  # address -> [{tx_hash, tx_pos, value}]

def register_utxo(address: str, tx_hash: str, tx_pos: int, value: int):
    """Register a known UTXO for local tracking."""
    if address not in _tracked_utxos:
        _tracked_utxos[address] = []
    # Avoid duplicates
    for u in _tracked_utxos[address]:
        if u["tx_hash"] == tx_hash and u["tx_pos"] == tx_pos:
            return
    _tracked_utxos[address].append({"tx_hash": tx_hash, "tx_pos": tx_pos, "value": value})
    logger.info(f"[UTXO-TRACK] Registered: {address[:10]}... {tx_hash[:16]}:{tx_pos} = {value} sats")

def consume_utxo(address: str, tx_hash: str, tx_pos: int):
    """Remove a consumed UTXO from local tracking."""
    if address in _tracked_utxos:
        _tracked_utxos[address] = [
            u for u in _tracked_utxos[address]
            if not (u["tx_hash"] == tx_hash and u["tx_pos"] == tx_pos)
        ]
        logger.info(f"[UTXO-TRACK] Consumed: {address[:10]}... {tx_hash[:16]}:{tx_pos}")

def get_tracked_utxos(address: str) -> list[dict]:
    """Get locally tracked UTXOs for an address."""
    return _tracked_utxos.get(address, [])


class PreSplitPool:
    """Global pool of pre-split 1-sat DSTAS UTXOs ready for transfer.
    
    The backend automatically splits tokens in the background so that
    when a user starts a test, the charge phase is nearly instant.
    """
    def __init__(self):
        self.stas_utxos: list[dict] = []  # Available 1-sat DSTAS UTXOs
        self.fee_utxos: list[dict] = []   # Available fee UTXOs
        self.token_scheme: dict | None = None
        self.wallet: dict | None = None
        self.status: str = "idle"  # idle, scanning, minting, splitting, ready, error
        self.progress: int = 0
        self.total: int = 0
        self.error: str = ""
        self.lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    @property
    def available(self) -> int:
        return len(self.stas_utxos)

    def take(self, count: int) -> tuple[list[dict], list[dict]]:
        """Take up to `count` pre-split UTXOs and associated fee UTXOs from the pool."""
        taken_stas = self.stas_utxos[:count]
        self.stas_utxos = self.stas_utxos[count:]
        taken_fees = list(self.fee_utxos)
        self.fee_utxos = []
        return taken_stas, taken_fees

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "available": self.available,
            "progress": self.progress,
            "total": self.total,
            "error": self.error,
        }


# Global pre-split pool instance
pre_split_pool = PreSplitPool()


async def background_presplit():
    """Background task: pre-split DSTAS tokens for testnet using fixed wallet."""
    pool = pre_split_pool
    try:
        if not TESTNET_WIF:
            pool.status = "idle"
            logger.info("[PRESPLIT] No TESTNET_WIF configured, skipping pre-split")
            return

        # Wait for STAS service to be ready
        pool.status = "scanning"
        for attempt in range(30):
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(f"{STAS_SERVICE_URL}/healthz")
                    if resp.status_code == 200:
                        break
            except Exception:
                pass
            await asyncio.sleep(2)
        else:
            pool.status = "error"
            pool.error = "STAS service not reachable"
            logger.error("[PRESPLIT] STAS service not reachable after 60s")
            return

        # Import testnet wallet
        wallet = import_bsv_wallet(TESTNET_WIF)
        pool.wallet = wallet
        mode = "bsvtestnet"
        privkey_hex = wallet["privkey_bytes"].hex()
        address_hash160 = wallet["hash160"].hex()
        FEE_RATE = 0.001  # Minimum fee rate for testnet (1 sat per ~1000 bytes)

        token_scheme = {
            "name": "STAS",
            "tokenId": address_hash160,
            "symbol": "STAS",
            "satoshisPerToken": 1,
            "freeze": False,
            "confiscation": False,
            "isDivisible": True,
        }
        pool.token_scheme = token_scheme

        # Wait a bit for recent TXs to propagate to WoC
        await asyncio.sleep(5)

        # Get wallet UTXOs
        logger.info(f"[PRESPLIT] Scanning UTXOs for {wallet['address']}")
        raw_utxos = await woc_get_utxos(wallet["address"], mode)
        if not raw_utxos:
            pool.status = "error"
            pool.error = "UTXOがありません。tBSVを送金してください。"
            logger.warning("[PRESPLIT] No UTXOs found")
            return

        # Parse UTXOs
        sorted_utxos = sorted(raw_utxos, key=lambda u: u.get("value", 0), reverse=True)
        p2pkh_utxos: list[dict] = []
        dstas_1sat_utxos: list[dict] = []
        dstas_large_utxos: list[dict] = []

        for idx, candidate in enumerate(sorted_utxos):
            try:
                # Retry WoC tx hex fetch up to 3 times (unconfirmed TXs may 404 briefly)
                tx_hex = None
                for retry in range(3):
                    try:
                        tx_hex = await woc_get_tx_hex(candidate["tx_hash"], mode)
                        break
                    except Exception:
                        if retry < 2:
                            await asyncio.sleep(2)
                if not tx_hex:
                    logger.warning(f"[PRESPLIT] Skipping UTXO {candidate['tx_hash']}:{candidate['tx_pos']} (value={candidate['value']}) - TX hex not found after retries")
                    continue
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
                elif output.get("scriptType") == "dstas":
                    if candidate["value"] == 1:
                        dstas_1sat_utxos.append({
                            "txId": candidate["tx_hash"],
                            "vout": candidate["tx_pos"],
                            "satoshis": 1,
                            "lockingScriptHex": output["lockingScriptHex"],
                            "addressHash160": output.get("addressHash160", address_hash160),
                            "scriptType": "dstas",
                        })
                    elif candidate["value"] > 1:
                        dstas_large_utxos.append({
                            "txId": candidate["tx_hash"],
                            "vout": candidate["tx_pos"],
                            "satoshis": candidate["value"],
                            "lockingScriptHex": output["lockingScriptHex"],
                            "addressHash160": output.get("addressHash160", address_hash160),
                            "scriptType": "dstas",
                        })
            except Exception as e:
                logger.warning(f"[PRESPLIT] UTXO parse error: {e}")
                continue

        total_p2pkh_sats = sum(u["satoshis"] for u in p2pkh_utxos)
        total_large_dstas_sats = sum(u["satoshis"] for u in dstas_large_utxos)
        logger.info(f"[PRESPLIT] Found {len(p2pkh_utxos)} P2PKH ({total_p2pkh_sats} sats), {len(dstas_1sat_utxos)} 1-sat DSTAS, {len(dstas_large_utxos)} large DSTAS ({total_large_dstas_sats} sats)")
        logger.info(f"[PRESPLIT] Raw UTXOs from WoC: {len(sorted_utxos)} total, parsed {len(p2pkh_utxos)+len(dstas_1sat_utxos)+len(dstas_large_utxos)} successfully")

        # Store existing 1-sat UTXOs in pool
        pool.stas_utxos = list(dstas_1sat_utxos)
        fee_pool: list[dict] = []
        for u in p2pkh_utxos:
            fee_pool.append({
                "txId": u["txid"], "vout": u["vout"], "satoshis": u["satoshis"],
                "lockingScriptHex": u["locking_script"],
                "addressHash160": address_hash160, "scriptType": "p2pkh",
            })

        # If no large DSTAS and not enough P2PKH to mint, just use what we have
        if not dstas_large_utxos and (not p2pkh_utxos or dstas_1sat_utxos):
            pool.fee_utxos = fee_pool
            pool.status = "ready"
            logger.info(f"[PRESPLIT] Ready with {pool.available} recycled UTXOs, {len(fee_pool)} fee UTXOs")
            return

        # If we have large DSTAS and fee UTXOs, split them
        if dstas_large_utxos and fee_pool:
            source_dstas = dstas_large_utxos[0]
            new_count = source_dstas["satoshis"]
            pool.status = "splitting"
            pool.total = new_count
            pool.progress = 0

            MAX_SPLIT_BATCH = 3
            cur = source_dstas
            remaining = new_count
            batch_num = 0
            total_batches = (new_count + MAX_SPLIT_BATCH - 1) // MAX_SPLIT_BATCH

            # Reserve at least 80% of fee sats for user operations
            # Don't split at all if we have < 200,000 sats (not enough to be worth splitting)
            MIN_RESERVE_SATS = max(50000, int(total_p2pkh_sats * 0.8))
            if total_p2pkh_sats < 200000:
                logger.info(f"[PRESPLIT] Skipping split: only {total_p2pkh_sats} sats (need >= 200,000)")
                pool.fee_utxos = fee_pool
                pool.status = "ready"
                return
            while remaining > 0 and cur and fee_pool:
                # Stop if fee pool drops below reserve threshold
                fee_total = sum(u["satoshis"] for u in fee_pool)
                if fee_total < MIN_RESERVE_SATS:
                    logger.info(f"[PRESPLIT] Stopping split: fee reserve {fee_total} < {MIN_RESERVE_SATS} sats")
                    break
                batch = min(remaining, MAX_SPLIT_BATCH)
                batch_num += 1

                dests = [{"satoshis": 1, "toHash160": address_hash160} for _ in range(batch)]
                change_sats = cur["satoshis"] - batch
                if change_sats > 0:
                    dests.append({"satoshis": change_sats, "toHash160": address_hash160})

                fee_utxo = fee_pool.pop(0)

                try:
                    result = await stas_service_call("/split", {
                        "privkeyHex": privkey_hex, "stasUtxo": cur, "feeUtxo": fee_utxo,
                        "destinations": dests, "scheme": token_scheme, "feeRate": FEE_RATE,
                    })
                    await woc_broadcast_tx(result["txHex"], mode)
                except Exception as e:
                    logger.error(f"[PRESPLIT] Split batch {batch_num} failed: {e}")
                    if fee_utxo:
                        fee_pool.insert(0, fee_utxo)
                    break

                cur = None
                for out in result["outputs"]:
                    if out["scriptType"] == "dstas" and out["satoshis"] == 1:
                        pool.stas_utxos.append({
                            "txId": result["txId"], "vout": out["vout"], "satoshis": 1,
                            "lockingScriptHex": out["lockingScriptHex"],
                            "addressHash160": out.get("addressHash160", address_hash160),
                            "scriptType": "dstas",
                        })
                    elif out["scriptType"] == "dstas" and out["satoshis"] > 1:
                        cur = {"txId": result["txId"], "vout": out["vout"], "satoshis": out["satoshis"],
                               "lockingScriptHex": out["lockingScriptHex"],
                               "addressHash160": out.get("addressHash160", address_hash160),
                               "scriptType": "dstas"}
                    elif out["scriptType"] == "p2pkh" and out["satoshis"] > 0:
                        fee_pool.insert(0, {"txId": result["txId"], "vout": out["vout"], "satoshis": out["satoshis"],
                                           "lockingScriptHex": out["lockingScriptHex"],
                                           "addressHash160": address_hash160, "scriptType": "p2pkh"})

                remaining -= batch
                pool.progress = pool.available

                if batch_num % 50 == 0:
                    logger.info(f"[PRESPLIT] Split progress: {pool.available} UTXOs, batch {batch_num}/{total_batches}")

        # If large DSTAS exists but no fee UTXOs, mark ready with what we have
        elif dstas_large_utxos and not fee_pool:
            pool.fee_utxos = []
            pool.status = "ready"
            logger.info(f"[PRESPLIT] Ready with {pool.available} UTXOs (no fee UTXOs for further splitting)")
            return

        # If we have enough P2PKH but no DSTAS at all, mint first
        elif p2pkh_utxos and not dstas_1sat_utxos and not dstas_large_utxos:
            total_p2pkh_sats = sum(u["satoshis"] for u in p2pkh_utxos)
            MINT_AMOUNT = 10_000_000
            estimated_issue_fee = int(5000 * FEE_RATE) + 500
            min_required = MINT_AMOUNT + estimated_issue_fee

            # Don't mint if balance is low (< 200,000 sats) - preserve for user operations
            if total_p2pkh_sats < 200000:
                pool.fee_utxos = fee_pool
                pool.status = "ready"
                logger.info(f"[PRESPLIT] Insufficient balance ({total_p2pkh_sats} sats) to mint, ready with {pool.available} UTXOs")
                return

            pool.status = "minting"
            if total_p2pkh_sats < min_required:
                MINT_AMOUNT = max(1000, total_p2pkh_sats - estimated_issue_fee)

            funding = p2pkh_utxos[0]
            funding_txid = funding["txid"]
            funding_vout = funding["vout"]
            funding_satoshis = funding["satoshis"]
            funding_locking_script = funding["locking_script"]

            # Consolidate if needed
            if funding_satoshis < min_required and len(p2pkh_utxos) > 1:
                try:
                    consolidate_inputs = [
                        build_utxo_info(u["txid"], u["vout"], u["satoshis"], u["locking_script"], address_hash160)
                        for u in p2pkh_utxos
                    ]
                    consolidate_result = await stas_service_call("/consolidate", {
                        "privkeyHex": privkey_hex, "utxos": consolidate_inputs, "feeRate": FEE_RATE,
                    })
                    await woc_broadcast_tx(consolidate_result["txHex"], mode)
                    consolidated_output = consolidate_result["outputs"][0]
                    funding_txid = consolidate_result["txId"]
                    funding_vout = 0
                    funding_satoshis = consolidated_output["satoshis"]
                    funding_locking_script = consolidated_output["lockingScriptHex"]
                    fee_pool = []
                except Exception as e:
                    pool.status = "error"
                    pool.error = f"UTXO統合失敗: {e}"
                    return

            try:
                logger.info(f"[PRESPLIT] Minting {MINT_AMOUNT:,} tokens")
                issue_result = await stas_service_call("/issue", {
                    "privkeyHex": privkey_hex,
                    "fundingUtxo": build_utxo_info(funding_txid, funding_vout, funding_satoshis, funding_locking_script, address_hash160),
                    "scheme": token_scheme,
                    "destinations": [{"satoshis": MINT_AMOUNT, "toHash160": address_hash160}],
                    "feeRate": FEE_RATE,
                })
                contract_txid = await woc_broadcast_tx(issue_result["contractTxHex"], mode)
                issue_txid = await woc_broadcast_tx(issue_result["issueTxHex"], mode)
                logger.info(f"[PRESPLIT] Minted: contract={contract_txid}, issue={issue_txid}")

                # Find DSTAS output and start splitting
                source_dstas = None
                fee_pool = []
                for out in issue_result["issueOutputs"]:
                    if out["scriptType"] == "dstas":
                        source_dstas = {
                            "txId": issue_txid, "vout": out["vout"], "satoshis": out["satoshis"],
                            "lockingScriptHex": out["lockingScriptHex"],
                            "addressHash160": out.get("addressHash160", address_hash160),
                            "scriptType": "dstas",
                        }
                    elif out["scriptType"] == "p2pkh" and out["satoshis"] > 0:
                        fee_pool = [{
                            "txId": issue_txid, "vout": out["vout"], "satoshis": out["satoshis"],
                            "lockingScriptHex": out["lockingScriptHex"],
                            "addressHash160": address_hash160, "scriptType": "p2pkh",
                        }]

                if source_dstas and fee_pool:
                    pool.status = "splitting"
                    pool.total = source_dstas["satoshis"]
                    MAX_SPLIT_BATCH = 3
                    cur = source_dstas
                    remaining = source_dstas["satoshis"]
                    batch_num = 0

                    # Reserve at least 50% of fee sats for user operations
                    MINT_RESERVE_SATS = max(10000, sum(u["satoshis"] for u in fee_pool) // 2)
                    while remaining > 0 and cur and fee_pool:
                        # Stop if fee pool drops below reserve threshold
                        fee_total = sum(u["satoshis"] for u in fee_pool)
                        if fee_total < MINT_RESERVE_SATS:
                            logger.info(f"[PRESPLIT] Stopping mint-split: fee reserve {fee_total} < {MINT_RESERVE_SATS} sats")
                            break
                        batch = min(remaining, MAX_SPLIT_BATCH)
                        batch_num += 1

                        dests = [{"satoshis": 1, "toHash160": address_hash160} for _ in range(batch)]
                        change_sats = cur["satoshis"] - batch
                        if change_sats > 0:
                            dests.append({"satoshis": change_sats, "toHash160": address_hash160})

                        fee_utxo = fee_pool.pop(0)
                        try:
                            result = await stas_service_call("/split", {
                                "privkeyHex": privkey_hex, "stasUtxo": cur, "feeUtxo": fee_utxo,
                                "destinations": dests, "scheme": token_scheme, "feeRate": FEE_RATE,
                            })
                            await woc_broadcast_tx(result["txHex"], mode)
                        except Exception as e:
                            logger.error(f"[PRESPLIT] Split failed: {e}")
                            break

                        cur = None
                        for out in result["outputs"]:
                            if out["scriptType"] == "dstas" and out["satoshis"] == 1:
                                pool.stas_utxos.append({
                                    "txId": result["txId"], "vout": out["vout"], "satoshis": 1,
                                    "lockingScriptHex": out["lockingScriptHex"],
                                    "addressHash160": out.get("addressHash160", address_hash160),
                                    "scriptType": "dstas",
                                })
                            elif out["scriptType"] == "dstas" and out["satoshis"] > 1:
                                cur = {"txId": result["txId"], "vout": out["vout"], "satoshis": out["satoshis"],
                                       "lockingScriptHex": out["lockingScriptHex"],
                                       "addressHash160": out.get("addressHash160", address_hash160),
                                       "scriptType": "dstas"}
                            elif out["scriptType"] == "p2pkh" and out["satoshis"] > 0:
                                fee_pool.insert(0, {"txId": result["txId"], "vout": out["vout"], "satoshis": out["satoshis"],
                                                   "lockingScriptHex": out["lockingScriptHex"],
                                                   "addressHash160": address_hash160, "scriptType": "p2pkh"})

                        remaining -= batch
                        pool.progress = pool.available

                        if batch_num % 50 == 0:
                            logger.info(f"[PRESPLIT] Split progress: {pool.available} UTXOs")

            except Exception as e:
                pool.status = "error"
                pool.error = f"Mint失敗: {e}"
                logger.error(f"[PRESPLIT] Mint failed: {e}")
                return

        pool.fee_utxos = fee_pool
        pool.status = "ready"
        logger.info(f"[PRESPLIT] Complete: {pool.available} DSTAS UTXOs, {len(fee_pool)} fee UTXOs ({sum(u['satoshis'] for u in fee_pool)} sats)")

    except Exception as e:
        pool.status = "error"
        pool.error = str(e)
        logger.error(f"[PRESPLIT] Unexpected error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: launch background pre-split task
    pre_split_pool._task = asyncio.create_task(background_presplit())
    logger.info("[STARTUP] Background pre-split task started")
    yield
    # Shutdown: cancel task
    if pre_split_pool._task:
        pre_split_pool._task.cancel()


app = FastAPI(lifespan=lifespan)

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
    is_testnet = mode in ("bsvtestnet", "jpysnet")
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
    """Check BSV balance. Uses local UTXO tracking first, then WoC balance (fast), then Bitails as fallback."""
    import subprocess
    data = None

    # JPYS Net: use RPC directly
    if mode == "jpysnet":
        data = await rpc_get_balance(address)
        total_satoshis = data["confirmed"] + data.get("unconfirmed", 0)
        return {
            "balance_satoshis": total_satoshis,
            "balance_bsv": total_satoshis / 1e8,
            "confirmed": data["confirmed"],
            "unconfirmed": data.get("unconfirmed", 0),
            "funded": total_satoshis > 0,
        }

    # Check locally tracked UTXOs first (much faster than network queries)
    tracked = get_tracked_utxos(address)
    if tracked:
        total = sum(u["value"] for u in tracked)
        logger.info(f"[BALANCE] Local tracking: {len(tracked)} UTXOs, {total} sats")
        data = {"confirmed": total, "unconfirmed": 0}

    # Try WoC balance endpoint first (fast - returns just numbers, not UTXO list)
    if data is None:
        timeout = httpx.Timeout(15.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            api_base = "https://api.whatsonchain.com/v1/bsv/test" if mode == "bsvtestnet" else "https://api.whatsonchain.com/v1/bsv/main"
            try:
                resp = await client.get(f"{api_base}/address/{address}/balance")
                if resp.status_code == 404:
                    # Address not found = no balance yet (new address)
                    pass
                elif resp.status_code == 200:
                    data = resp.json()
                    logger.info(f"[BALANCE] WoC balance: confirmed={data.get('confirmed',0)}, unconfirmed={data.get('unconfirmed',0)}")
            except Exception as e:
                logger.warning(f"[BALANCE] WoC error: {e}")

    # For testnet, try Bitails unspent as fallback (slow for addresses with many UTXOs)
    if data is None and mode == "bsvtestnet":
        def _curl_balance():
            try:
                r = subprocess.run(
                    ["curl", "-4", "-s", "--connect-timeout", "5", "--max-time", "15",
                     f"https://test-api.bitails.io/address/{address}/unspent"],
                    capture_output=True, text=True, timeout=20,
                )
                return r
            except Exception:
                return None
        try:
            result = await asyncio.to_thread(_curl_balance)
            if result and result.returncode == 0 and result.stdout.strip():
                import json
                body = json.loads(result.stdout)
                utxos = body.get("unspent", body) if isinstance(body, dict) else body
                if isinstance(utxos, list):
                    total = sum(u.get("value", u.get("satoshis", 0)) for u in utxos)
                    data = {"confirmed": total, "unconfirmed": 0}
                    logger.info(f"[BALANCE] Bitails (curl): {len(utxos)} UTXOs, {total} sats")
        except Exception as e:
            logger.warning(f"[BALANCE] Bitails curl error: {e}")

    if data is None:
        data = {"confirmed": 0, "unconfirmed": 0}

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
    """Get UTXOs for an address. Uses local tracking first, then Bitails/WoC as fallback."""
    import subprocess, json as _json

    # JPYS Net: use RPC directly
    if mode == "jpysnet":
        tracked = get_tracked_utxos(address)
        if tracked:
            logger.info(f"[UTXO] JPYSNET local tracking: {len(tracked)} UTXOs for {address[:10]}...")
            return list(tracked)
        return await rpc_get_utxos(address)

    # Check locally tracked UTXOs first
    tracked = get_tracked_utxos(address)
    if tracked:
        logger.info(f"[UTXO] Local tracking: {len(tracked)} UTXOs for {address[:10]}...")
        return list(tracked)  # Already in {tx_hash, tx_pos, value} format

    # For testnet, use Bitails via curl in thread (non-blocking)
    if mode == "bsvtestnet":
        def _curl_utxos():
            try:
                r = subprocess.run(
                    ["curl", "-4", "-s", "--connect-timeout", "10", "--max-time", "30",
                     f"https://test-api.bitails.io/address/{address}/unspent"],
                    capture_output=True, text=True, timeout=35,
                )
                return r
            except Exception:
                return None
        try:
            result = await asyncio.to_thread(_curl_utxos)
            if result and result.returncode == 0 and result.stdout.strip():
                body = _json.loads(result.stdout)
                utxos = body.get("unspent", body) if isinstance(body, dict) else body
                if isinstance(utxos, list) and len(utxos) > 0:
                    logger.info(f"[UTXO] Bitails (curl): {len(utxos)} UTXOs")
                    return [{"tx_hash": u.get("txid", ""), "tx_pos": u.get("vout", 0), "value": u.get("satoshis", u.get("value", 0))} for u in utxos]
        except Exception as e:
            logger.warning(f"[UTXO] Bitails curl error: {e}")
    # Fallback to WoC
    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        api_base = "https://api.whatsonchain.com/v1/bsv/test" if mode == "bsvtestnet" else "https://api.whatsonchain.com/v1/bsv/main"
        try:
            resp = await client.get(f"{api_base}/address/{address}/unspent")
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"[UTXO] WoC error: {e}")
            return []


async def woc_get_tx_hex(txid: str, mode: str) -> str:
    """Get raw transaction hex. Testnet uses Bitails first, mainnet uses WoC."""
    # JPYS Net: use RPC directly
    if mode == "jpysnet":
        return await rpc_get_tx_hex(txid)

    timeout = httpx.Timeout(60.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        # For testnet, try Bitails first
        if mode == "bsvtestnet":
            try:
                resp = await client.get(f"https://test-api.bitails.io/tx/{txid}/raw")
                if resp.status_code == 200:
                    return resp.text
            except Exception as e:
                logger.warning(f"[TX_HEX] Bitails error: {e}")
        # Fallback to WoC
        api_base = "https://api.whatsonchain.com/v1/bsv/test" if mode == "bsvtestnet" else "https://api.whatsonchain.com/v1/bsv/main"
        resp = await client.get(f"{api_base}/tx/{txid}/hex")
        resp.raise_for_status()
        return resp.text


def compute_txid_from_hex(tx_hex: str) -> str:
    """Compute txid from raw transaction hex (double SHA256, reversed)."""
    tx_bytes = bytes.fromhex(tx_hex)
    return _hash256(tx_bytes)[::-1].hex()


async def bitails_broadcast_tx(tx_hex: str, mode: str) -> str:
    """Broadcast a raw transaction via Bitails API. Returns txid."""
    api_base = "https://test-api.bitails.io" if mode == "bsvtestnet" else "https://api.bitails.io"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{api_base}/tx/broadcast", json={"raw": tx_hex})
            try:
                data = resp.json()
                # Success: txid in response
                if isinstance(data, dict) and "txid" in data and len(data["txid"]) == 64:
                    print(f"[BITAILS] Broadcast OK: {data['txid'][:16]}...")
                    return data["txid"]
                # Already in mempool = success (TX was already broadcast)
                if isinstance(data, dict) and isinstance(data.get("error"), dict):
                    err_msg = data["error"].get("message", "")
                    if "already-in-mempool" in err_msg or "already known" in err_msg:
                        txid = compute_txid_from_hex(tx_hex)
                        print(f"[BITAILS] TX already in mempool: {txid[:16]}...")
                        return txid
            except Exception:
                pass
            if resp.status_code in (200, 201):
                return resp.text.strip().strip('"')
            raise RuntimeError(f"Bitails broadcast failed ({resp.status_code}): {resp.text}")
    except (httpx.TimeoutException, httpx.ConnectError) as e:
        raise RuntimeError(f"Bitails broadcast error: {e}")


async def woc_broadcast_tx_raw(tx_hex: str, mode: str) -> str:
    """Broadcast a raw transaction via WoC API only. Returns txid."""
    api_base = "https://api.whatsonchain.com/v1/bsv/test" if mode == "bsvtestnet" else "https://api.whatsonchain.com/v1/bsv/main"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{api_base}/tx/raw", json={"txhex": tx_hex})
        if resp.status_code == 200:
            return resp.text.strip().strip('"')
        woc_error = resp.text
        if "already in the mempool" in woc_error or "txn-already-in-mempool" in woc_error:
            txid = compute_txid_from_hex(tx_hex)
            print(f"[WOC] TX already in mempool: {txid[:16]}...")
            return txid
        raise RuntimeError(f"WoC broadcast failed ({resp.status_code}): {woc_error}")


async def woc_broadcast_tx(tx_hex: str, mode: str, retries: int = 3) -> str:
    """Broadcast TX: Bitails first (default), WoC as fallback. Returns txid."""
    # JPYS Net: use RPC directly
    if mode == "jpysnet":
        return await rpc_broadcast_tx(tx_hex)

    # Try Bitails first
    try:
        return await bitails_broadcast_tx(tx_hex, mode)
    except Exception as bitails_err:
        print(f"[BITAILS] Primary broadcast failed: {bitails_err}")

    # Fallback to WoC
    last_error = None
    for attempt in range(retries):
        try:
            return await woc_broadcast_tx_raw(tx_hex, mode)
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_error = e
            if attempt < retries - 1:
                print(f"[WOC] Fallback timeout/connect error, retry {attempt+1}/{retries}")
                await asyncio.sleep(2 * (attempt + 1))
                continue
        except RuntimeError as e:
            last_error = e
            err_str = str(e)
            if any(code in err_str for code in ("502", "503", "429")) and attempt < retries - 1:
                print(f"[WOC] Fallback retryable error, retry {attempt+1}/{retries}")
                await asyncio.sleep(2 * (attempt + 1))
                continue
            break
    raise RuntimeError(f"Broadcast failed (Bitails + WoC): {last_error}")


async def woc_get_tx_status(txid: str, mode: str) -> dict:
    """Check transaction status via WoC API."""
    # JPYS Net: use RPC directly
    if mode == "jpysnet":
        return await rpc_get_tx_status(txid)

    api_base = "https://api.whatsonchain.com/v1/bsv/test" if mode == "bsvtestnet" else "https://api.whatsonchain.com/v1/bsv/main"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{api_base}/tx/{txid}")
        if resp.status_code == 200:
            return resp.json()
        return {"error": resp.text}


async def wait_for_tx_confirmation(txid: str, mode: str, timeout: int = 900, poll_interval: int = 15) -> bool:
    """Wait for a TX to get at least 1 confirmation. Returns True if confirmed within timeout."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            status = await woc_get_tx_status(txid, mode)
            if isinstance(status, dict) and status.get("confirmations", 0) > 0:
                return True
        except Exception:
            pass
        await asyncio.sleep(poll_interval)
    return False


# --- JPYS Net (regtest) RPC Functions ---

_jpysnet_imported_addresses: set[str] = set()  # Track imported addresses to avoid re-importing


async def rpc_call(method: str, params: list = None) -> dict:
    """Make a JSON-RPC call to the JPYS Net BSV node."""
    payload = {
        "jsonrpc": "1.0",
        "id": "stas-cannon",
        "method": method,
        "params": params or [],
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            JPYSNET_RPC_URL,
            json=payload,
            auth=(JPYSNET_RPC_USER, JPYSNET_RPC_PASS),
        )
        data = resp.json()
        if data.get("error"):
            raise RuntimeError(f"RPC error: {data['error']}")
        return data.get("result")


async def rpc_import_address(address: str):
    """Import an address into the JPYS Net node wallet (watch-only) for UTXO tracking."""
    if address in _jpysnet_imported_addresses:
        return
    try:
        await rpc_call("importaddress", [address, "", False])  # False = no rescan
        _jpysnet_imported_addresses.add(address)
        logger.info(f"[JPYSNET] Imported address: {address[:15]}...")
    except Exception as e:
        if "already in the address book" in str(e) or "already have" in str(e):
            _jpysnet_imported_addresses.add(address)
        else:
            logger.warning(f"[JPYSNET] importaddress failed: {e}")


async def rpc_broadcast_tx(tx_hex: str) -> str:
    """Broadcast a raw transaction to JPYS Net. Returns txid."""
    try:
        txid = await rpc_call("sendrawtransaction", [tx_hex])
        logger.info(f"[JPYSNET] Broadcast OK: {txid[:16]}...")
        return txid
    except Exception as e:
        err_str = str(e)
        if "already in the mempool" in err_str or "already known" in err_str or "txn-already-in-mempool" in err_str:
            txid = compute_txid_from_hex(tx_hex)
            logger.info(f"[JPYSNET] TX already in mempool: {txid[:16]}...")
            return txid
        raise


async def rpc_get_utxos(address: str) -> list[dict]:
    """Get UTXOs for an address from JPYS Net node."""
    await rpc_import_address(address)
    # Rescan just this address range to pick up new UTXOs
    try:
        utxos = await rpc_call("listunspent", [0, 9999999, [address]])
    except Exception as e:
        logger.warning(f"[JPYSNET] listunspent failed: {e}")
        return []
    return [{"tx_hash": u["txid"], "tx_pos": u["vout"], "value": int(round(u["amount"] * 1e8))} for u in (utxos or [])]


async def rpc_get_balance(address: str) -> dict:
    """Get balance for an address from JPYS Net node."""
    utxos = await rpc_get_utxos(address)
    total = sum(u["value"] for u in utxos)
    return {"confirmed": total, "unconfirmed": 0}


async def rpc_get_tx_hex(txid: str) -> str:
    """Get raw transaction hex from JPYS Net node."""
    return await rpc_call("getrawtransaction", [txid, 0])


async def rpc_get_tx_status(txid: str) -> dict:
    """Get transaction status from JPYS Net node."""
    try:
        data = await rpc_call("getrawtransaction", [txid, 1])
        return {"confirmations": data.get("confirmations", 0)}
    except Exception:
        return {"confirmations": 0}


async def rpc_generate_blocks(n: int = 1) -> list[str]:
    """Generate blocks on JPYS Net (for explicit confirmation)."""
    try:
        return await rpc_call("generate", [n])
    except Exception as e:
        logger.warning(f"[JPYSNET] generate failed: {e}")
        return []


async def broadcast_with_chain_wait(tx_hex: str, mode: str, parent_txid: str, max_waits: int = 100) -> str:
    """Broadcast a TX, waiting for confirmation if mempool chain limit or min fee is hit.
    Returns txid on success, raises on permanent failure."""
    for wait_round in range(max_waits):
        try:
            return await woc_broadcast_tx(tx_hex, mode)
        except Exception as e:
            err_str = str(e)
            if "too-long-mempool-chain" in err_str:
                if wait_round == 0:
                    logger.info(f"[CHAIN-WAIT] Mempool chain limit hit, waiting for confirmation of {parent_txid[:16]}...")
                # Wait for parent TX to confirm (next block)
                confirmed = await wait_for_tx_confirmation(parent_txid, mode, timeout=900, poll_interval=15)
                if confirmed:
                    logger.info(f"[CHAIN-WAIT] Parent {parent_txid[:16]}... confirmed after {wait_round + 1} round(s), retrying broadcast")
                    continue
                else:
                    raise RuntimeError(f"Timeout waiting for TX confirmation: {parent_txid}")
            elif "mempool min fee not met" in err_str or "Missing inputs" in err_str:
                # Mempool is full or parent TX evicted - wait for blocks to clear space
                if wait_round == 0:
                    logger.info(f"[CHAIN-WAIT] Mempool issue ({err_str[:60]}), waiting for blocks to clear...")
                await asyncio.sleep(15)  # Wait for gen=1 auto-mining to clear mempool
                if wait_round >= 40:
                    raise RuntimeError(f"Mempool issue not resolved after {wait_round + 1} retries: {err_str[:80]}")
                continue
            else:
                raise


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
    if mode in ("bsvtestnet", "jpysnet"):
        version = 0x6f  # testnet/regtest: m or n prefix
    else:
        version = 0x00  # mainnet: 1 prefix
    return _base58check_encode(version, raw)


def generate_txid():
    """Generate a simulated BSV transaction ID (64-char hex)."""
    return secrets.token_hex(32)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/api/seed-utxo")
async def seed_utxo(data: dict = None):
    """Manually register a known UTXO for local tracking."""
    if not data:
        return {"error": "data required"}
    address = data.get("address", "")
    tx_hash = data.get("tx_hash", "")
    tx_pos = data.get("tx_pos", 0)
    value = data.get("value", 0)
    if address and tx_hash and value > 0:
        register_utxo(address, tx_hash, tx_pos, value)
        return {"ok": True, "tracked": get_tracked_utxos(address)}
    return {"error": "invalid params"}

@app.get("/api/presplit-status")
async def presplit_status():
    """Get the status of the background pre-split pool."""
    return pre_split_pool.to_dict()


# --- Auto Faucet System ---

FAUCET_API_URL = "https://witnessonchain.com/v1/faucet/tbsv"
FAUCET_CHANNEL = "scrypt.io"


async def _request_faucet(address: str) -> dict:
    """Request tBSV from the scrypt.io faucet API (no CAPTCHA required)."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            FAUCET_API_URL,
            json={"address": address, "channel": FAUCET_CHANNEL},
        )
        data = resp.json()
        return data


async def _build_consolidation_tx(
    temp_wallets: list[dict],
    target_address: str,
    faucet_results: list[dict],
) -> str | None:
    """Build a raw TX to consolidate faucet funds from temp wallets to the target address.
    Uses BIP143 sighash (required for BSV).
    Returns the raw TX hex, or None if no funds to consolidate."""
    inputs = []
    total_value = 0
    FAUCET_AMOUNT = 100_000  # known faucet amount per request

    for i, wallet in enumerate(temp_wallets):
        if wallet is None:
            continue
        result = faucet_results[i]
        if result.get("code") != 0:
            continue
        txid = result.get("txid", "")
        if not txid:
            continue

        # Determine output value: try parsing raw TX, fallback to known amount
        out_value = FAUCET_AMOUNT
        raw_hex = result.get("raw", "")
        if raw_hex:
            try:
                raw_bytes = bytes.fromhex(raw_hex)
                offset = 4  # skip version
                vin_count = raw_bytes[offset]
                offset += 1
                for _ in range(vin_count):
                    offset += 36  # txid + vout
                    script_len = raw_bytes[offset]
                    offset += 1 + script_len + 4  # script + sequence
                offset += 1  # skip vout_count
                out_value = int.from_bytes(raw_bytes[offset:offset + 8], "little")
            except Exception:
                out_value = FAUCET_AMOUNT

        # Find the correct vout index for our address
        vout_idx = 0
        if raw_hex:
            try:
                target_h160 = _hash160(wallet["pubkey"])
                raw_bytes = bytes.fromhex(raw_hex)
                off = 4
                vic = raw_bytes[off]
                off += 1
                for _ in range(vic):
                    off += 36
                    sl = raw_bytes[off]
                    off += 1 + sl + 4
                voc = raw_bytes[off]
                off += 1
                for vo_i in range(voc):
                    val = int.from_bytes(raw_bytes[off:off + 8], "little")
                    off += 8
                    scr_len = raw_bytes[off]
                    off += 1
                    scr = raw_bytes[off:off + scr_len]
                    off += scr_len
                    # Check if P2PKH script matches our pubkey hash
                    if len(scr) == 25 and scr[3:23] == target_h160:
                        vout_idx = vo_i
                        out_value = val
                        break
            except Exception:
                vout_idx = 0

        inputs.append({
            "txid": txid,
            "vout": vout_idx,
            "value": out_value,
            "privkey_bytes": wallet["privkey_bytes"],
            "pubkey": wallet["pubkey"],
        })
        total_value += out_value

    if not inputs:
        return None

    # Estimate fee: ~148 bytes per input + 34 bytes per output + 10 bytes overhead
    estimated_size = len(inputs) * 148 + 34 + 10
    fee = max(estimated_size, 200)  # minimum 200 sats fee
    send_value = total_value - fee

    if send_value <= 0:
        return None

    # Output script (P2PKH to target address)
    target_raw = _base58check_decode(target_address)
    target_h160 = target_raw[1:]  # strip version byte
    script_pubkey = b"\x76\xa9\x14" + target_h160 + b"\x88\xac"

    # --- BIP143 sighash precomputation ---
    # hashPrevouts = hash256(all outpoints)
    prevouts_data = b""
    for inp in inputs:
        prevouts_data += bytes.fromhex(inp["txid"])[::-1]
        prevouts_data += inp["vout"].to_bytes(4, "little")
    hash_prevouts = _hash256(prevouts_data)

    # hashSequence = hash256(all sequences)
    sequence_data = b""
    for _ in inputs:
        sequence_data += b"\xff\xff\xff\xff"
    hash_sequence = _hash256(sequence_data)

    # hashOutputs = hash256(all outputs)
    outputs_data = b""
    outputs_data += send_value.to_bytes(8, "little")
    outputs_data += bytes([len(script_pubkey)]) + script_pubkey
    hash_outputs = _hash256(outputs_data)

    # Sign each input using BIP143 sighash
    signed_tx = b""
    signed_tx += (1).to_bytes(4, "little")  # version
    signed_tx += bytes([len(inputs)])  # input count

    for idx, inp in enumerate(inputs):
        # BIP143 sighash preimage:
        # 1. nVersion (4)
        # 2. hashPrevouts (32)
        # 3. hashSequence (32)
        # 4. outpoint (36) - this input's txid + vout
        # 5. scriptCode (var) - P2PKH script for this input
        # 6. value (8) - value of UTXO being spent
        # 7. nSequence (4)
        # 8. hashOutputs (32)
        # 9. nLocktime (4)
        # 10. nHashType (4) - SIGHASH_ALL|FORKID = 0x41
        h160 = _hash160(inp["pubkey"])
        script_code = b"\x76\xa9\x14" + h160 + b"\x88\xac"

        preimage = b""
        preimage += (1).to_bytes(4, "little")  # nVersion
        preimage += hash_prevouts  # hashPrevouts
        preimage += hash_sequence  # hashSequence
        preimage += bytes.fromhex(inp["txid"])[::-1]  # outpoint txid
        preimage += inp["vout"].to_bytes(4, "little")  # outpoint vout
        preimage += bytes([len(script_code)]) + script_code  # scriptCode
        preimage += inp["value"].to_bytes(8, "little")  # value
        preimage += b"\xff\xff\xff\xff"  # nSequence
        preimage += hash_outputs  # hashOutputs
        preimage += (0).to_bytes(4, "little")  # nLocktime
        preimage += (0x41).to_bytes(4, "little")  # SIGHASH_ALL|FORKID

        sighash = _hash256(preimage)

        # Sign with ECDSA (secp256k1)
        signature = _ecdsa_sign(sighash, inp["privkey_bytes"])
        sig_der = signature + b"\x41"  # SIGHASH_ALL|FORKID hashtype byte

        # Build scriptSig: <sig> <pubkey>
        script_sig = bytes([len(sig_der)]) + sig_der + bytes([len(inp["pubkey"])]) + inp["pubkey"]

        # Write signed input
        signed_tx += bytes.fromhex(inp["txid"])[::-1]
        signed_tx += inp["vout"].to_bytes(4, "little")
        signed_tx += bytes([len(script_sig)]) + script_sig
        signed_tx += b"\xff\xff\xff\xff"

    signed_tx += b"\x01"  # output count
    signed_tx += send_value.to_bytes(8, "little")
    signed_tx += bytes([len(script_pubkey)]) + script_pubkey
    signed_tx += (0).to_bytes(4, "little")  # locktime

    return signed_tx.hex()


def _ecdsa_sign(msg_hash: bytes, privkey_bytes: bytes) -> bytes:
    """Sign a message hash with ECDSA secp256k1, return DER-encoded signature."""
    # secp256k1 curve parameters
    p = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
    n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
    Gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
    Gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

    def modinv(a: int, m: int) -> int:
        g, x, _ = _ext_gcd(a % m, m)
        return x % m

    def _ext_gcd(a: int, b: int) -> tuple:
        if a == 0:
            return b, 0, 1
        g, x, y = _ext_gcd(b % a, a)
        return g, y - (b // a) * x, x

    def point_add(x1, y1, x2, y2):
        if x1 is None:
            return x2, y2
        if x2 is None:
            return x1, y1
        if x1 == x2 and y1 == y2:
            lam = (3 * x1 * x1) * modinv(2 * y1, p) % p
        elif x1 == x2:
            return None, None
        else:
            lam = (y2 - y1) * modinv(x2 - x1, p) % p
        x3 = (lam * lam - x1 - x2) % p
        y3 = (lam * (x1 - x3) - y1) % p
        return x3, y3

    def scalar_mult(k, x, y):
        rx, ry = None, None
        while k > 0:
            if k & 1:
                rx, ry = point_add(rx, ry, x, y)
            x, y = point_add(x, y, x, y)
            k >>= 1
        return rx, ry

    z = int.from_bytes(msg_hash, "big")
    d = int.from_bytes(privkey_bytes, "big")

    # RFC 6979 deterministic k
    k = _rfc6979_k(msg_hash, privkey_bytes, n)

    rx, _ = scalar_mult(k, Gx, Gy)
    r = rx % n
    s = (modinv(k, n) * (z + r * d)) % n

    # Low-S normalization (BIP 62)
    if s > n // 2:
        s = n - s

    # DER encode
    def der_int(v: int) -> bytes:
        b = v.to_bytes((v.bit_length() + 8) // 8, "big")
        return b"\x02" + bytes([len(b)]) + b

    rs = der_int(r) + der_int(s)
    return b"\x30" + bytes([len(rs)]) + rs


def _rfc6979_k(msg_hash: bytes, privkey_bytes: bytes, n: int) -> int:
    """Deterministic k generation per RFC 6979."""
    import hmac
    v = b"\x01" * 32
    k = b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + privkey_bytes + msg_hash, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + privkey_bytes + msg_hash, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        candidate = int.from_bytes(v, "big")
        if 1 <= candidate < n:
            return candidate
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


@app.post("/api/faucet")
async def auto_faucet(request_data: dict = None):
    """Auto-faucet: request tBSV from scrypt.io faucet using temp addresses.

    Always generates a fresh temp address per request to bypass per-address cooldown,
    then consolidates funds to the target address via a signed TX.

    Body: {"address": "<target_testnet_address>", "count": 1}
    - address: target testnet address to receive tBSV
    - count: number of faucet requests (each generates a new temp address), default 1
    """
    if request_data is None:
        request_data = {}
    target_address = request_data.get("address", "")
    count = min(request_data.get("count", 1), 10)  # max 10 per call

    if not target_address:
        return {"error": "address is required"}

    results = []
    temp_wallets = []
    faucet_results = []

    for i in range(count):
        # Always generate a fresh temp address to avoid per-address cooldown
        wallet = generate_bsv_wallet("bsvtestnet")
        temp_wallets.append(wallet)
        data = await _request_faucet(wallet["address"])
        faucet_results.append(data)
        results.append({
            "address": wallet["address"],
            "code": data.get("code"),
            "message": data.get("message", ""),
            "txid": data.get("txid", ""),
        })
        # Small delay between requests to avoid rate limiting
        if i < count - 1:
            await asyncio.sleep(1)

    success_count = sum(1 for r in results if r["code"] == 0)
    total_received = success_count * 100000  # ~100,000 sats per faucet request

    response = {
        "success": success_count > 0,
        "total_requests": count,
        "successful": success_count,
        "total_sats": total_received,
        "results": results,
    }

    # Consolidate funds from temp addresses to target address
    if success_count > 0:
        try:
            consolidation_hex = await _build_consolidation_tx(
                temp_wallets, target_address, faucet_results
            )
            if consolidation_hex:
                # Broadcast consolidation TX via Bitails (testnet)
                async with httpx.AsyncClient(timeout=30.0) as client:
                    broadcast_resp = await client.post(
                        "https://test-api.bitails.io/tx/broadcast",
                        json={"raw": consolidation_hex},
                    )
                    broadcast_data = broadcast_resp.json()
                    txid = broadcast_data.get("txid", "")
                    if txid:
                        response["consolidation_txid"] = txid
                        response["consolidated"] = True
                        # Register UTXO in local tracking
                        output_sats = total_received - 200  # estimated fee
                        register_utxo(target_address, txid, 0, output_sats)
                    elif broadcast_resp.status_code == 200:
                        response["consolidation_txid"] = str(broadcast_data)
                        response["consolidated"] = True
                    else:
                        response["consolidation_error"] = str(broadcast_data)
                        response["consolidated"] = False
            else:
                response["consolidation_error"] = "Could not build consolidation TX"
                response["consolidated"] = False
        except Exception as e:
            response["consolidation_error"] = str(e)
            response["consolidated"] = False

    return response


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
    @app.get("/jpysnet")
    async def serve_index():
        return FileResponse(
            os.path.join(STATIC_DIR, "index.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"},
        )


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
                    # Send pre-split pool status for testnet
                    if mode == "bsvtestnet" and wif == TESTNET_WIF:
                        await websocket.send_json({
                            "type": "presplit_status",
                            **pre_split_pool.to_dict(),
                        })
                except Exception as e:
                    try:
                        await websocket.send_json({"type": "wallet_error", "message": str(e)})
                    except RuntimeError:
                        logger.warning(f"[WS] Could not send wallet_error (connection closed): {e}")

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
                    try:
                        await websocket.send_json({"type": "wallet_error", "message": str(e)})
                    except RuntimeError:
                        logger.warning(f"[WS] Could not send balance_error (connection closed): {e}")

            elif action == "configure":
                raw = msg.get("total_transfers")
                mode_defaults = {"localtest": 1_000_000, "bsvtestnet": 10, "bsvmainnet": 1_000_000, "jpysnet": 10}
                mode_max = {"localtest": 1_000_000_000, "bsvtestnet": 1_000_000_000, "bsvmainnet": 1_000_000_000, "jpysnet": 1_000_000_000}
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
                if mode in ("bsvtestnet", "bsvmainnet", "jpysnet") and st.wallet:
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

    Strategy:
    0. Check pre-split pool first (instant if available)
    1. Check for existing recyclable 1-sat DSTAS UTXOs (instant recycling)
    2. If large DSTAS UTXOs exist, split them into 1-sat UTXOs using parallel chains
    3. If no DSTAS, mint tokens in a SINGLE issue TX, then parallel split
    4. Self-transfer for token recycling on next run
    """
    st.phase = "power_charge"
    total = st.total_transfers
    mode = st.mode
    wallet = st.wallet
    FEE_RATE = 0.05 if mode == "bsvmainnet" else 0.001  # 1 sat/TX minimum for testnet/jpysnet

    if not wallet:
        await ws.send_json({"type": "error", "message": "\u30a6\u30a9\u30ec\u30c3\u30c8\u304c\u8a2d\u5b9a\u3055\u308c\u3066\u3044\u307e\u305b\u3093"})
        return

    privkey_hex = wallet["privkey_bytes"].hex()
    address_hash160 = wallet["hash160"].hex()

    await ws.send_json({"type": "phase", "phase": "power_charge", "total": total})

    # === Step 0: Check pre-split pool (testnet only, same wallet) ===
    if mode == "bsvtestnet" and wallet.get("wif") == TESTNET_WIF and pre_split_pool.status == "ready" and pre_split_pool.available >= total:
        await ws.send_json({"type": "progress", "phase": "power_charge", "current": 0, "total": total, "percent": 5,
                           "status": f"\u4e8b\u524d\u5206\u5272\u30d7\u30fc\u30eb\u304b\u3089\u53d6\u5f97\u4e2d... ({pre_split_pool.available}\u500b\u5229\u7528\u53ef\u80fd)"})
        taken_stas, taken_fees = pre_split_pool.take(total)
        if len(taken_stas) >= total:
            st.stas_utxos = taken_stas[:total]
            st.fee_utxos = taken_fees
            st.utxos_prepared = total
            st.token_scheme = pre_split_pool.token_scheme
            st.issue_txid = "presplit"
            st.receiver_address = wallet["address"]
            st.receiver_hash160 = address_hash160
            total_fee_sats = sum(u["satoshis"] for u in taken_fees)
            # If pool has no fee UTXOs, fetch P2PKH UTXOs from wallet
            if total_fee_sats < total:
                logger.info(f"[CHARGE] Pre-split pool fee insufficient ({total_fee_sats} sats), fetching from wallet...")
                try:
                    raw_utxos = await woc_get_utxos(wallet["address"], mode)
                    for candidate in sorted(raw_utxos, key=lambda u: u.get("value", 0), reverse=True):
                        if candidate["value"] <= 1:
                            continue  # Skip 1-sat UTXOs (likely DSTAS)
                        try:
                            tx_hex = await woc_get_tx_hex(candidate["tx_hash"], mode)
                            tx_info = await stas_service_call("/parse-tx", {"txHex": tx_hex})
                            output = tx_info["outputs"][candidate["tx_pos"]]
                            if output.get("scriptType") == "p2pkh":
                                fee_utxo = {
                                    "txId": candidate["tx_hash"], "vout": candidate["tx_pos"],
                                    "satoshis": candidate["value"],
                                    "lockingScriptHex": output["lockingScriptHex"],
                                    "addressHash160": address_hash160, "scriptType": "p2pkh",
                                }
                                st.fee_utxos = [fee_utxo]
                                total_fee_sats = candidate["value"]
                                logger.info(f"[CHARGE] Supplemented fee from wallet: {total_fee_sats} sats")
                                break
                        except Exception:
                            continue
                except Exception as e:
                    logger.warning(f"[CHARGE] Failed to supplement fee: {e}")
            logger.info(f"[CHARGE] Used pre-split pool: {total} UTXOs, {len(st.fee_utxos)} fee UTXOs ({total_fee_sats} sats)")
            await ws.send_json({
                "type": "phase_complete", "phase": "power_charge",
                "utxos_prepared": total, "issue_txid": "presplit",
                "recycled": True, "fee_budget": total_fee_sats,
            })
            return

    await ws.send_json({"type": "progress", "phase": "power_charge", "current": 0, "total": total, "percent": 0, "status": "UTXO\u3092\u53d6\u5f97\u4e2d..."})

    # Step 1: Get wallet UTXOs
    try:
        raw_utxos = await woc_get_utxos(wallet["address"], mode)
    except Exception as e:
        await ws.send_json({"type": "error", "message": f"UTXO\u53d6\u5f97\u5931\u6557: {e}"})
        return

    if not raw_utxos:
        await ws.send_json({"type": "error", "message": "UTXO\u304c\u3042\u308a\u307e\u305b\u3093\u3002BSV\u3092\u9001\u91d1\u3057\u3066\u304f\u3060\u3055\u3044\u3002"})
        return

    # Step 2: Parse UTXOs
    sorted_utxos = sorted(raw_utxos, key=lambda u: u.get("value", 0), reverse=True)
    p2pkh_utxos: list[dict] = []
    dstas_1sat_utxos: list[dict] = []
    dstas_large_utxos: list[dict] = []

    for idx, candidate in enumerate(sorted_utxos):
        try:
            await ws.send_json({"type": "progress", "phase": "power_charge", "current": 0, "total": total, "percent": 1, "status": f"UTXO\u691c\u8a3c\u4e2d... ({idx+1}/{len(sorted_utxos)})"})
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
            elif output.get("scriptType") == "dstas":
                if candidate["value"] == 1:
                    dstas_1sat_utxos.append({
                        "txId": candidate["tx_hash"],
                        "vout": candidate["tx_pos"],
                        "satoshis": 1,
                        "lockingScriptHex": output["lockingScriptHex"],
                        "addressHash160": output.get("addressHash160", address_hash160),
                        "scriptType": "dstas",
                    })
                elif candidate["value"] > 1:
                    dstas_large_utxos.append({
                        "txId": candidate["tx_hash"],
                        "vout": candidate["tx_pos"],
                        "satoshis": candidate["value"],
                        "lockingScriptHex": output["lockingScriptHex"],
                        "addressHash160": output.get("addressHash160", address_hash160),
                        "scriptType": "dstas",
                    })
        except Exception as e:
            print(f"[CHARGE] UTXO parse error for {candidate.get('tx_hash', '?')}:{candidate.get('tx_pos', '?')}: {e}")
            continue

    print(f"[CHARGE] Found {len(p2pkh_utxos)} P2PKH, {len(dstas_1sat_utxos)} recyclable 1-sat DSTAS, {len(dstas_large_utxos)} large DSTAS")

    token_scheme = {
        "name": "STAS",
        "tokenId": address_hash160,
        "symbol": "STAS",
        "satoshisPerToken": 1,
        "freeze": False,
        "confiscation": False,
        "isDivisible": True,
    }
    st.token_scheme = token_scheme

    final_stas = list(dstas_1sat_utxos)
    fee_pool: list[dict] = []

    for u in p2pkh_utxos:
        fee_pool.append({
            "txId": u["txid"],
            "vout": u["vout"],
            "satoshis": u["satoshis"],
            "lockingScriptHex": u["locking_script"],
            "addressHash160": address_hash160,
            "scriptType": "p2pkh",
        })

    # =========================================================================
    # Case 1: Already have enough 1-sat DSTAS UTXOs (full recycling)
    # =========================================================================
    if len(final_stas) >= total:
        print(f"[CHARGE] Recycling {total} existing DSTAS tokens (skipping issuance)")
        await ws.send_json({"type": "progress", "phase": "power_charge", "current": total, "total": total, "percent": 90,
                           "status": f"\u65e2\u5b58\u30c8\u30fc\u30af\u30f3\u518d\u5229\u7528: {total}\u500b (\u767a\u884c\u30b9\u30ad\u30c3\u30d7)"})
        st.issue_txid = "recycled"

    else:
        existing_count = len(final_stas)
        new_count = total - existing_count

        # Determine DSTAS source: existing large UTXOs or new mint
        large_sats_available = sum(u["satoshis"] for u in dstas_large_utxos)
        source_dstas = None  # The large DSTAS UTXO to split from

        if large_sats_available >= new_count and dstas_large_utxos:
            # ================================================================
            # Case 2: Have large DSTAS UTXOs - use them as split source
            # ================================================================
            print(f"[CHARGE] Using existing large DSTAS ({large_sats_available} sats) for splitting")
            source_dstas = dstas_large_utxos[0]
            st.issue_txid = "split"
            await ws.send_json({"type": "progress", "phase": "power_charge", "current": existing_count, "total": total, "percent": 5,
                               "status": f"\u65e2\u5b58\u30c8\u30fc\u30af\u30f3\u3092\u5206\u5272\u4e2d... ({new_count}\u500b)"})

        else:
            # ================================================================
            # Case 3: Need to mint new tokens - issue ONE large DSTAS output
            # ================================================================
            if not p2pkh_utxos:
                await ws.send_json({"type": "error", "message": "\u5229\u7528\u53ef\u80fd\u306aP2PKH UTXO\u304c\u898b\u3064\u304b\u308a\u307e\u305b\u3093\u3002BSV\u3092\u9001\u91d1\u3057\u3066\u304f\u3060\u3055\u3044\u3002"})
                return

            MINT_AMOUNT = max(new_count, 10_000_000)
            total_p2pkh_sats = sum(u["satoshis"] for u in p2pkh_utxos)

            estimated_issue_fee = int(5000 * FEE_RATE) + 500
            min_required = MINT_AMOUNT + estimated_issue_fee

            if total_p2pkh_sats < min_required:
                MINT_AMOUNT = new_count
                min_required = MINT_AMOUNT + estimated_issue_fee
                if total_p2pkh_sats < min_required:
                    await ws.send_json({"type": "error", "message": f"\u8cc7\u91d1\u4e0d\u8db3: {total_p2pkh_sats} sats < \u5fc5\u8981\u91cf ~{min_required} sats"})
                    return

            funding_txid = p2pkh_utxos[0]["txid"]
            funding_vout = p2pkh_utxos[0]["vout"]
            funding_satoshis = p2pkh_utxos[0]["satoshis"]
            funding_locking_script = p2pkh_utxos[0]["locking_script"]

            if funding_satoshis < min_required and len(p2pkh_utxos) > 1:
                await ws.send_json({"type": "progress", "phase": "power_charge", "current": 0, "total": total, "percent": 2,
                                   "status": f"UTXO\u7d71\u5408\u4e2d... ({len(p2pkh_utxos)}\u500b \u2192 1\u500b)"})
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
                    print(f"[CHARGE] Consolidation TX: {consolidate_txid}")
                    consolidated_output = consolidate_result["outputs"][0]
                    funding_txid = consolidate_result["txId"]
                    funding_vout = 0
                    funding_satoshis = consolidated_output["satoshis"]
                    funding_locking_script = consolidated_output["lockingScriptHex"]
                    fee_pool = []
                except Exception as e:
                    await ws.send_json({"type": "error", "message": f"UTXO\u7d71\u5408\u5931\u6557: {e}"})
                    return

            await ws.send_json({"type": "progress", "phase": "power_charge", "current": 0, "total": total, "percent": 5,
                               "status": f"{MINT_AMOUNT:,}\u30c8\u30fc\u30af\u30f3\u3092\u4e00\u62ec\u767a\u884c\u4e2d..."})

            try:
                print(f"[CHARGE] Issuing {MINT_AMOUNT:,} tokens in single TX, funding={funding_satoshis} sats")
                issue_result = await stas_service_call("/issue", {
                    "privkeyHex": privkey_hex,
                    "fundingUtxo": build_utxo_info(funding_txid, funding_vout, funding_satoshis, funding_locking_script, address_hash160),
                    "scheme": token_scheme,
                    "destinations": [{"satoshis": MINT_AMOUNT, "toHash160": address_hash160}],
                    "feeRate": FEE_RATE,
                })
            except Exception as e:
                await ws.send_json({"type": "error", "message": f"\u30c8\u30fc\u30af\u30f3\u767a\u884c\u5931\u6557: {e}"})
                return

            try:
                contract_txid = await broadcast_with_chain_wait(issue_result["contractTxHex"], mode, funding_txid)
                print(f"[CHARGE] Contract TX: {contract_txid}")
            except Exception as e:
                await ws.send_json({"type": "error", "message": f"Contract TX\u30d6\u30ed\u30fc\u30c9\u30ad\u30e3\u30b9\u30c8\u5931\u6557: {e}"})
                return

            try:
                issue_txid = await broadcast_with_chain_wait(issue_result["issueTxHex"], mode, contract_txid)
                st.issue_txid = issue_txid
                print(f"[CHARGE] Issue TX: {issue_txid} ({MINT_AMOUNT:,} tokens)")
            except Exception as e:
                await ws.send_json({"type": "error", "message": f"Issue TX\u30d6\u30ed\u30fc\u30c9\u30ad\u30e3\u30b9\u30c8\u5931\u6557: {e}"})
                return

            # Find the large DSTAS output and fee change from issue TX
            for out in issue_result["issueOutputs"]:
                if out["scriptType"] == "dstas":
                    source_dstas = {
                        "txId": issue_txid,
                        "vout": out["vout"],
                        "satoshis": out["satoshis"],
                        "lockingScriptHex": out["lockingScriptHex"],
                        "addressHash160": out.get("addressHash160", address_hash160),
                        "scriptType": "dstas",
                    }
                elif out["scriptType"] == "p2pkh" and out["satoshis"] > 0:
                    fee_pool = [{
                        "txId": issue_txid,
                        "vout": out["vout"],
                        "satoshis": out["satoshis"],
                        "lockingScriptHex": out["lockingScriptHex"],
                        "addressHash160": address_hash160,
                        "scriptType": "p2pkh",
                    }]

            if not source_dstas:
                await ws.send_json({"type": "error", "message": "\u30c8\u30fc\u30af\u30f3\u767a\u884c\u7d50\u679c\u306bDSTA\u51fa\u529b\u304c\u3042\u308a\u307e\u305b\u3093"})
                return

        # =================================================================
        # Split the large DSTAS source into individual 1-sat UTXOs
        # Uses parallel chains for large counts (>= 100)
        # =================================================================
        MAX_SPLIT_BATCH = 3  # DSTAS script enforces max 4 DSTAS outputs per split TX (3 + 1 change)

        if not fee_pool:
            await ws.send_json({"type": "error", "message": "Fee UTXO\u304c\u4e0d\u8db3\u3057\u3066\u3044\u307e\u3059"})
            return

        NUM_WORKERS = min(5, max(1, new_count // 100))  # Cap at 5 to avoid overwhelming regtest node RPC
        if new_count < 30:
            NUM_WORKERS = 1

        print(f"[CHARGE] Splitting {new_count} tokens from {source_dstas['satoshis']} sats DSTAS, {NUM_WORKERS} workers")

        if NUM_WORKERS <= 1:
            # === Simple sequential split ===
            await ws.send_json({"type": "progress", "phase": "power_charge", "current": existing_count, "total": total, "percent": 10,
                               "status": f"\u30c8\u30fc\u30af\u30f3\u5206\u5272\u4e2d... ({new_count}\u500b)"})

            cur = source_dstas
            remaining = new_count
            batch_num = 0
            total_batches = (new_count + MAX_SPLIT_BATCH - 1) // MAX_SPLIT_BATCH

            while remaining > 0 and st.running and cur:
                batch = min(remaining, MAX_SPLIT_BATCH)
                batch_num += 1
                pct = 10 + int(80 * batch_num / total_batches)

                if batch_num % 10 == 0 or batch_num <= 3:
                    await ws.send_json({"type": "progress", "phase": "power_charge", "current": existing_count + len(final_stas) - len(dstas_1sat_utxos), "total": total, "percent": pct,
                                       "status": f"\u5206\u5272 {batch_num}/{total_batches}"})

                dests = [{"satoshis": 1, "toHash160": address_hash160} for _ in range(batch)]
                change_sats = cur["satoshis"] - batch
                if change_sats > 0:
                    dests.append({"satoshis": change_sats, "toHash160": address_hash160})

                fee_utxo = fee_pool.pop(0)
                print(f"[SPLIT-DEBUG] batch={batch_num} stasUtxo={cur} feeUtxo={fee_utxo}")

                try:
                    result = await stas_service_call("/split", {
                        "privkeyHex": privkey_hex, "stasUtxo": cur, "feeUtxo": fee_utxo,
                        "destinations": dests, "scheme": token_scheme, "feeRate": FEE_RATE,
                    })
                except Exception as e:
                    print(f"[SPLIT-DEBUG] FAILED: {e}")
                    await ws.send_json({"type": "error", "message": f"\u5206\u5272\u5931\u6557 (\u30d0\u30c3\u30c1 {batch_num}): {e}"})
                    return

                try:
                    await woc_broadcast_tx(result["txHex"], mode)
                except Exception as e:
                    await ws.send_json({"type": "error", "message": f"\u5206\u5272TX\u30d6\u30ed\u30fc\u30c9\u30ad\u30e3\u30b9\u30c8\u5931\u6557: {e}"})
                    return

                cur = None
                for out in result["outputs"]:
                    if out["scriptType"] == "dstas" and out["satoshis"] == 1:
                        final_stas.append({
                            "txId": result["txId"], "vout": out["vout"], "satoshis": 1,
                            "lockingScriptHex": out["lockingScriptHex"],
                            "addressHash160": out.get("addressHash160", address_hash160),
                            "scriptType": "dstas",
                        })
                    elif out["scriptType"] == "dstas" and out["satoshis"] > 1:
                        cur = {"txId": result["txId"], "vout": out["vout"], "satoshis": out["satoshis"],
                               "lockingScriptHex": out["lockingScriptHex"],
                               "addressHash160": out.get("addressHash160", address_hash160),
                               "scriptType": "dstas"}
                    elif out["scriptType"] == "p2pkh" and out["satoshis"] > 0:
                        fee_pool.insert(0, {"txId": result["txId"], "vout": out["vout"], "satoshis": out["satoshis"],
                                           "lockingScriptHex": out["lockingScriptHex"],
                                           "addressHash160": address_hash160, "scriptType": "p2pkh"})

                remaining -= batch

        else:
            # === Parallel split with NUM_WORKERS chains ===
            await ws.send_json({"type": "progress", "phase": "power_charge", "current": existing_count, "total": total, "percent": 8,
                               "status": f"\u4e26\u5217\u5206\u5272\u6e96\u5099\u4e2d... ({NUM_WORKERS}\u30ef\u30fc\u30ab\u30fc)"})

            # --- Step A: Chunk the large DSTAS into NUM_WORKERS pieces ---
            per_worker = new_count // NUM_WORKERS
            remainder_tokens = new_count % NUM_WORKERS

            chunks = []
            cur_dstas = source_dstas
            chunk_fee = fee_pool.pop(0)

            for i in range(NUM_WORKERS - 1):
                chunk_size = per_worker + (1 if i < remainder_tokens else 0)
                remaining_sats = cur_dstas["satoshis"] - chunk_size

                if remaining_sats <= 0:
                    chunks.append(cur_dstas)
                    cur_dstas = None
                    break

                dests = [
                    {"satoshis": chunk_size, "toHash160": address_hash160},
                    {"satoshis": remaining_sats, "toHash160": address_hash160},
                ]

                try:
                    result = await stas_service_call("/split", {
                        "privkeyHex": privkey_hex, "stasUtxo": cur_dstas, "feeUtxo": chunk_fee,
                        "destinations": dests, "scheme": token_scheme, "feeRate": FEE_RATE,
                    })
                    await broadcast_with_chain_wait(result["txHex"], mode, cur_dstas["txId"])
                except Exception as e:
                    await ws.send_json({"type": "error", "message": f"\u30c1\u30e3\u30f3\u30af\u5206\u5272\u5931\u6557 ({i+1}): {e}"})
                    return

                chunk_utxo = None
                remaining_utxo = None
                new_chunk_fee = None
                for out in result["outputs"]:
                    if out["scriptType"] == "dstas":
                        utxo_data = {
                            "txId": result["txId"], "vout": out["vout"], "satoshis": out["satoshis"],
                            "lockingScriptHex": out["lockingScriptHex"],
                            "addressHash160": out.get("addressHash160", address_hash160),
                            "scriptType": "dstas",
                        }
                        if out["satoshis"] == chunk_size and chunk_utxo is None:
                            chunk_utxo = utxo_data
                        else:
                            remaining_utxo = utxo_data
                    elif out["scriptType"] == "p2pkh" and out["satoshis"] > 0:
                        new_chunk_fee = {
                            "txId": result["txId"], "vout": out["vout"], "satoshis": out["satoshis"],
                            "lockingScriptHex": out["lockingScriptHex"],
                            "addressHash160": address_hash160, "scriptType": "p2pkh",
                        }

                if chunk_utxo:
                    chunks.append(chunk_utxo)
                if remaining_utxo:
                    cur_dstas = remaining_utxo
                if new_chunk_fee:
                    chunk_fee = new_chunk_fee

                await ws.send_json({"type": "progress", "phase": "power_charge", "current": existing_count, "total": total, "percent": 9,
                                   "status": f"\u30c1\u30e3\u30f3\u30af\u5206\u5272\u4e2d... ({i+2}/{NUM_WORKERS})"})

            # Last worker gets the remaining DSTAS
            if cur_dstas and len(chunks) < NUM_WORKERS:
                chunks.append(cur_dstas)

            # Return unused chunk fee to pool
            if chunk_fee:
                fee_pool.insert(0, chunk_fee)

            actual_workers = len(chunks)
            print(f"[CHARGE] Created {actual_workers} DSTAS chunks: {[c['satoshis'] for c in chunks]}")

            # --- Step B: Split fee UTXOs into worker fee chains + transfer reserve ---
            if not fee_pool:
                await ws.send_json({"type": "error", "message": "Fee UTXO\u304c\u4e0d\u8db3\u3057\u3066\u3044\u307e\u3059"})
                return

            # Fee allocation strategy:
            # /split-fee creates EQUAL outputs. Workers return unused fee after splits.
            # Total fee available for transfers = reserve_outputs + worker_remaining.
            # Key: minimize worker outputs size (just enough for splits) to maximize
            # the leftover that flows to transfers.
            #
            # DSTAS split TXs are ~15KB → ~16 sats/split at 0.001 rate
            # STAS transfer TXs observed ~7 sats/transfer
            import math
            total_fee_sats = sum(u["satoshis"] for u in fee_pool)
            splits_per_worker = math.ceil(per_worker / MAX_SPLIT_BATCH)
            est_fee_per_split = max(16, int(15000 * FEE_RATE))
            est_fee_per_transfer = max(7, int(7000 * FEE_RATE))  # observed ~7 sats/transfer

            # Workers need just enough for their splits (with 20% margin)
            min_per_worker_sats = int(splits_per_worker * est_fee_per_split * 1.2)
            total_worker_need = min_per_worker_sats * actual_workers
            # Transfers need: total * est_fee_per_transfer
            total_transfer_need = total * est_fee_per_transfer

            # Calculate optimal output count:
            # We want worker outputs small enough for splits, and as many reserve
            # outputs as possible to cover transfers.
            # Since outputs are equal: sats_per_output = total / num_outputs
            # Worker constraint: sats_per_output >= min_per_worker_sats
            # Transfer constraint: (num_outputs - actual_workers) * sats_per_output >= total_transfer_need
            # Max outputs we can create while satisfying worker constraint:
            if min_per_worker_sats > 0:
                max_total_outputs = total_fee_sats // min_per_worker_sats
            else:
                max_total_outputs = actual_workers + 10
            # Reserve outputs = max_total_outputs - actual_workers
            num_reserve_outputs = max(1, max_total_outputs - actual_workers)
            # Cap reserve at reasonable level (transfer groups, max 10)
            num_transfer_groups = min(10, total) if total >= 100 else 1
            num_reserve_outputs = min(num_reserve_outputs, num_transfer_groups)
            total_fee_outputs = actual_workers + num_reserve_outputs
            sats_per_output = total_fee_sats // total_fee_outputs if total_fee_outputs > 0 else 0
            est_splits_possible = sats_per_output // est_fee_per_split if est_fee_per_split > 0 else 0
            # After splits, each worker returns ~(sats_per_output - splits_per_worker * est_fee_per_split)
            est_worker_leftover = max(0, sats_per_output - splits_per_worker * est_fee_per_split) * actual_workers
            est_transfer_sats = num_reserve_outputs * sats_per_output + est_worker_leftover
            est_transfers_possible = est_transfer_sats // est_fee_per_transfer if est_fee_per_transfer > 0 else 0
            print(f"[CHARGE] Fee pool: {total_fee_sats} sats, splitting into {actual_workers} worker + {num_reserve_outputs} reserve outputs")
            print(f"[CHARGE] Each output: ~{sats_per_output} sats, est {est_splits_possible} splits/worker (need {splits_per_worker})")
            print(f"[CHARGE] Transfer est: ~{est_transfer_sats} sats avail ({num_reserve_outputs} reserve + worker leftover), ~{est_transfers_possible} transfers possible (need {total})")

            try:
                fee_split_result = await stas_service_call("/split-fee", {
                    "privkeyHex": privkey_hex,
                    "utxos": fee_pool,
                    "numOutputs": total_fee_outputs,
                    "feeRate": FEE_RATE,
                })
                fee_split_txid = await woc_broadcast_tx(fee_split_result["txHex"], mode)
                print(f"[CHARGE] Fee split TX: {fee_split_txid} -> {total_fee_outputs} outputs ({actual_workers} worker + {num_reserve_outputs} reserve)")
            except Exception as e:
                await ws.send_json({"type": "error", "message": f"Fee\u5206\u5272\u5931\u6557: {e}"})
                return

            worker_fees = []
            extra_fees = []
            for out in fee_split_result["outputs"]:
                fee_item = {
                    "txId": fee_split_result["txId"], "vout": out["vout"], "satoshis": out["satoshis"],
                    "lockingScriptHex": out["lockingScriptHex"],
                    "addressHash160": address_hash160, "scriptType": "p2pkh",
                }
                if len(worker_fees) < actual_workers:
                    worker_fees.append(fee_item)
                else:
                    extra_fees.append(fee_item)

            # --- Step C: Run parallel split workers ---
            progress_counter = [0]
            progress_lock = asyncio.Lock()

            async def split_worker(worker_id, dstas_chunk, worker_fee, target_count):
                """Split a DSTAS chunk into individual 1-sat UTXOs.
                Handles BSV mempool chain limits by waiting for confirmation and retrying."""
                worker_results = []
                cur = dstas_chunk
                cur_fee = worker_fee
                remaining = target_count
                batch_num = 0
                chain_waits = 0

                while remaining > 0 and st.running and cur and cur_fee:
                    batch = min(remaining, MAX_SPLIT_BATCH)
                    batch_num += 1

                    dests = [{"satoshis": 1, "toHash160": address_hash160} for _ in range(batch)]
                    change_sats = cur["satoshis"] - batch
                    if change_sats > 0:
                        dests.append({"satoshis": change_sats, "toHash160": address_hash160})

                    try:
                        result = await stas_service_call("/split", {
                            "privkeyHex": privkey_hex, "stasUtxo": cur, "feeUtxo": cur_fee,
                            "destinations": dests, "scheme": token_scheme, "feeRate": FEE_RATE,
                        })
                    except Exception as e:
                        print(f"[CHARGE] Worker {worker_id} split {batch_num} failed: {e}")
                        return worker_results, cur_fee

                    try:
                        await broadcast_with_chain_wait(result["txHex"], mode, cur["txId"])
                    except Exception as e:
                        error_str = str(e)
                        if "mempool min fee not met" in error_str:
                            # Mempool is full - wait for a block to be mined, then retry
                            print(f"[CHARGE] Worker {worker_id} mempool full at batch {batch_num}, waiting for block...")
                            for wait_attempt in range(10):
                                await asyncio.sleep(15)  # Wait for gen=1 to mine a block
                                try:
                                    await broadcast_with_chain_wait(result["txHex"], mode, cur["txId"])
                                    print(f"[CHARGE] Worker {worker_id} retry succeeded after {wait_attempt + 1} waits")
                                    break
                                except Exception as retry_e:
                                    if "mempool min fee not met" in str(retry_e):
                                        continue
                                    elif "Missing inputs" in str(retry_e) or "already in" in str(retry_e):
                                        break  # TX already confirmed or inputs spent
                                    else:
                                        print(f"[CHARGE] Worker {worker_id} retry {wait_attempt + 1} failed: {retry_e}")
                                        return worker_results, cur_fee
                            else:
                                print(f"[CHARGE] Worker {worker_id} gave up after 10 mempool waits")
                                return worker_results, cur_fee
                        elif "too-long-mempool-chain" in error_str or "Timeout waiting" in error_str:
                            print(f"[CHARGE] Worker {worker_id} chain limit at batch {batch_num}, giving up after waits")
                            return worker_results, cur_fee
                        elif "Server disconnected" in error_str or "Broken promise" in error_str:
                            # Node temporarily overwhelmed - wait and retry
                            print(f"[CHARGE] Worker {worker_id} node disconnected at batch {batch_num}, retrying after wait...")
                            for retry_attempt in range(5):
                                await asyncio.sleep(10 + retry_attempt * 5)  # Increasing backoff
                                try:
                                    await broadcast_with_chain_wait(result["txHex"], mode, cur["txId"])
                                    print(f"[CHARGE] Worker {worker_id} reconnect retry succeeded after {retry_attempt + 1} attempts")
                                    break
                                except Exception as retry_e:
                                    retry_str = str(retry_e)
                                    if "Missing inputs" in retry_str or "already in" in retry_str:
                                        break  # TX already confirmed
                                    if retry_attempt == 4:
                                        print(f"[CHARGE] Worker {worker_id} gave up after 5 reconnect retries: {retry_e}")
                                        return worker_results, cur_fee
                        else:
                            print(f"[CHARGE] Worker {worker_id} broadcast {batch_num} failed: {e}")
                            return worker_results, cur_fee

                    cur = None
                    cur_fee = None
                    for out in result["outputs"]:
                        if out["scriptType"] == "dstas" and out["satoshis"] == 1:
                            worker_results.append({
                                "txId": result["txId"], "vout": out["vout"], "satoshis": 1,
                                "lockingScriptHex": out["lockingScriptHex"],
                                "addressHash160": out.get("addressHash160", address_hash160),
                                "scriptType": "dstas",
                            })
                        elif out["scriptType"] == "dstas" and out["satoshis"] > 1:
                            cur = {"txId": result["txId"], "vout": out["vout"], "satoshis": out["satoshis"],
                                   "lockingScriptHex": out["lockingScriptHex"],
                                   "addressHash160": out.get("addressHash160", address_hash160),
                                   "scriptType": "dstas"}
                        elif out["scriptType"] == "p2pkh" and out["satoshis"] > 0:
                            cur_fee = {"txId": result["txId"], "vout": out["vout"], "satoshis": out["satoshis"],
                                       "lockingScriptHex": out["lockingScriptHex"],
                                       "addressHash160": address_hash160, "scriptType": "p2pkh"}

                    remaining -= batch

                    # Update shared progress (every 10 batches to reduce lock contention)
                    async with progress_lock:
                        progress_counter[0] += batch
                        if batch_num % 10 == 0 or remaining == 0:
                            done = existing_count + progress_counter[0]
                            pct = 10 + int(80 * done / total)
                            await ws.send_json({
                                "type": "progress", "phase": "power_charge",
                                "current": done, "total": total, "percent": pct,
                                "status": f"\u4e26\u5217\u5206\u5272\u4e2d... {done:,}/{total:,} ({actual_workers}\u30ef\u30fc\u30ab\u30fc)"
                            })

                return worker_results, cur_fee

            # Calculate per-worker token counts
            worker_counts = []
            for i in range(actual_workers):
                worker_counts.append(per_worker + (1 if i < remainder_tokens else 0))

            await ws.send_json({"type": "progress", "phase": "power_charge", "current": existing_count, "total": total, "percent": 10,
                               "status": f"\u4e26\u5217\u5206\u5272\u958b\u59cb... ({actual_workers}\u30ef\u30fc\u30ab\u30fc x ~{per_worker}\u500b)"})

            # Launch all workers in parallel
            tasks = [split_worker(i, chunks[i], worker_fees[i], worker_counts[i]) for i in range(actual_workers)]
            results = await asyncio.gather(*tasks)

            # Collect results from all workers
            fee_pool = list(extra_fees)  # Transfer reserve outputs
            for worker_results, remaining_fee in results:
                final_stas.extend(worker_results)
                if remaining_fee:
                    fee_pool.append(remaining_fee)

            reserve_sats = sum(u["satoshis"] for u in extra_fees)
            worker_remaining = sum(u["satoshis"] for u in fee_pool) - reserve_sats
            total_remaining = sum(u["satoshis"] for u in fee_pool)
            print(f"[CHARGE] Parallel split complete: {len(final_stas) - existing_count} new tokens from {actual_workers} workers")
            print(f"[CHARGE] Fee remaining: {total_remaining} sats ({reserve_sats} reserve + {worker_remaining} worker remaining)")
            # Budget sufficiency check for transfer phase
            est_transfer_fee = est_fee_per_transfer  # ~7 sats per transfer
            max_transfers = total_remaining // est_transfer_fee if est_transfer_fee > 0 else 0
            if max_transfers < total:
                print(f"[CHARGE] WARNING: Fee budget ({total_remaining} sats) only covers ~{max_transfers}/{total} transfers at ~{est_transfer_fee} sats/tx")

    # Store results
    st.stas_utxos = final_stas[:total]
    st.fee_utxos = fee_pool
    st.utxos_prepared = len(st.stas_utxos)

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
    FEE_RATE = 0.05 if st.mode == "bsvmainnet" else 0.001  # 1 sat/TX minimum for testnet

    await ws.send_json({"type": "phase", "phase": "launch", "total": total})

    build_start = time.time()
    await ws.send_json({"type": "phase", "phase": "build", "total": total})

    broadcast_start = time.time()
    st.tx_broadcast = 0
    st.tx_errors = 0
    st.tx_ids = []

    # Determine concurrency: use parallel groups for large counts
    NUM_GROUPS = min(10, total) if total >= 100 else 1

    # Fee sufficiency check
    total_fee_sats = sum(u["satoshis"] for u in st.fee_utxos)
    estimated_fee_per_tx = 500 if st.mode == "bsvmainnet" else 7  # ~7 sats/transfer for testnet/jpysnet DSTAS
    min_fee_needed = total * estimated_fee_per_tx
    print(f"[LAUNCH] {total} transfers, {len(st.fee_utxos)} fee UTXOs ({total_fee_sats} sats), estimated need: {min_fee_needed} sats, groups: {NUM_GROUPS}")

    if total_fee_sats < estimated_fee_per_tx:
        await ws.send_json({"type": "error", "message": f"Fee不足: {total_fee_sats} sats (最低 {estimated_fee_per_tx} sats 必要)"})
        return

    if NUM_GROUPS > 1:
        # Split fee UTXOs into N groups via a fee-split TX
        # Each group needs its own fee chain
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
            # Use the first fee UTXO's txId as parent for chain-wait
            fee_parent_txid = st.fee_utxos[0]["txId"] if st.fee_utxos else ""
            split_txid = await broadcast_with_chain_wait(split_result["txHex"], st.mode, fee_parent_txid)
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
            # Re-fetch fee UTXOs from wallet in case originals are now invalid
            if "Missing inputs" in str(e) or "already spent" in str(e):
                print(f"[LAUNCH] Fee UTXOs may be stale, re-fetching from wallet...")
                try:
                    raw_utxos = await woc_get_utxos(wallet["address"], st.mode)
                    fresh_fees = []
                    for candidate in sorted(raw_utxos, key=lambda u: u.get("value", 0), reverse=True):
                        if candidate["value"] <= 1:
                            continue
                        try:
                            tx_hex = await woc_get_tx_hex(candidate["tx_hash"], st.mode)
                            tx_info = await stas_service_call("/parse-tx", {"txHex": tx_hex})
                            output = tx_info["outputs"][candidate["tx_pos"]]
                            if output.get("scriptType") == "p2pkh":
                                fresh_fees.append({
                                    "txId": candidate["tx_hash"], "vout": candidate["tx_pos"],
                                    "satoshis": candidate["value"],
                                    "lockingScriptHex": output["lockingScriptHex"],
                                    "addressHash160": address_hash160, "scriptType": "p2pkh",
                                })
                        except Exception:
                            continue
                    if fresh_fees:
                        st.fee_utxos = fresh_fees
                        print(f"[LAUNCH] Re-fetched {len(fresh_fees)} fee UTXOs ({sum(u['satoshis'] for u in fresh_fees)} sats)")
                    else:
                        print(f"[LAUNCH] No fresh fee UTXOs found")
                except Exception as fetch_e:
                    print(f"[LAUNCH] Failed to re-fetch fee UTXOs: {fetch_e}")

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
                        parent_tx = fee_utxo["txId"]
                        txid = await broadcast_with_chain_wait(result["txHex"], st.mode, parent_tx)
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
                print(f"[LAUNCH] Sequential transfer error: {e}")
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
            consecutive_errors = 0
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
                            parent_tx = current_fee["txId"]
                            txid = await broadcast_with_chain_wait(result["txHex"], st.mode, parent_tx)
                            async with lock:
                                st.tx_broadcast += 1
                                st.tx_ids.append(txid)
                            consecutive_errors = 0
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
                        except Exception as e:
                            consecutive_errors += 1
                            print(f"[LAUNCH] Group {group_idx} broadcast error ({consecutive_errors}): {e}")
                            async with lock:
                                st.tx_errors += 1
                            if consecutive_errors >= 10:
                                print(f"[LAUNCH] Group {group_idx} stopping after {consecutive_errors} consecutive errors")
                                async with lock:
                                    remaining = len(stas_list) - stas_list.index(stas_utxo) - 1
                                    st.tx_errors += remaining
                                return
                except Exception as e:
                    err_str = str(e)
                    if "Insufficient satoshis" in err_str:
                        print(f"[LAUNCH] Group {group_idx} fee exhausted after {st.tx_broadcast} transfers")
                        async with lock:
                            remaining = len(stas_list) - stas_list.index(stas_utxo)
                            st.tx_errors += remaining
                        return  # Stop this group - no more fee sats
                    consecutive_errors += 1
                    print(f"[LAUNCH] Group {group_idx} transfer build error ({consecutive_errors}): {e}")
                    async with lock:
                        st.tx_errors += 1
                    if consecutive_errors >= 10:
                        print(f"[LAUNCH] Group {group_idx} stopping after {consecutive_errors} consecutive errors")
                        async with lock:
                            remaining = len(stas_list) - stas_list.index(stas_utxo) - 1
                            st.tx_errors += remaining
                        return

        # Progress reporter task
        async def report_progress():
            while st.running and (st.tx_broadcast + st.tx_errors) < total:
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
