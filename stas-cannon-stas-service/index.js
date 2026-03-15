/**
 * STAS Cannon - Node.js STAS Service Layer
 *
 * Wraps dxs-stas-sdk to provide HTTP API for:
 * - Token issuance (Contract TX + Issue TX)
 * - STAS Split TX (1 -> up to 4 outputs)
 * - STAS Transfer TX
 * - P2PKH Split TX (for fee UTXOs)
 *
 * The SDK only supports mainnet addresses internally.
 * For testnet, we use the same private keys but let the SDK
 * generate mainnet-format addresses. The raw TX hex works on
 * any network since locking scripts use hash160 (network-agnostic).
 */

const express = require("express");
const {
  Address,
  PrivateKey,
  TokenScheme,
  OutPoint,
  BuildDstasIssueTxs,
  BuildDstasTransferTx,
  BuildDstasSplitTx,
} = require("dxs-stas-sdk");
const { ScriptType } = require("dxs-stas-sdk/dist/bitcoin/script-type");
const {
  TransactionBuilder,
} = require("dxs-stas-sdk/dist/transaction/build/transaction-builder");
const {
  TransactionReader,
} = require("dxs-stas-sdk/dist/transaction/read/transaction-reader");
const { fromHex, toHex } = require("dxs-stas-sdk/dist/bytes");
const { hash160 } = require("dxs-stas-sdk/dist/hashes");
const {
  LockingScriptReader,
} = require("dxs-stas-sdk/dist/script/read/locking-script-reader");

const app = express();
app.use(express.json({ limit: "100mb" }));

// Request logging middleware
app.use((req, res, next) => {
  const start = Date.now();
  console.log(`[${new Date().toISOString()}] ${req.method} ${req.path} - START`);
  res.on('finish', () => {
    console.log(`[${new Date().toISOString()}] ${req.method} ${req.path} - ${res.statusCode} (${Date.now() - start}ms)`);
  });
  next();
});

const PORT = process.env.STAS_SERVICE_PORT || 3001;

// --- Helpers ---

/**
 * Create a PrivateKey from raw hex bytes (32 bytes).
 * The SDK always derives a mainnet address from the public key.
 */
function privkeyFromHex(hexStr) {
  const bytes = fromHex(hexStr);
  return new PrivateKey(bytes);
}

/**
 * Create an Address from a hash160 hex string.
 * Always returns a mainnet-format address (SDK limitation).
 */
function addressFromHash160Hex(h160Hex) {
  return Address.fromHash160Hex(h160Hex);
}

/**
 * Build an OutPoint from UTXO data.
 */
function buildOutPoint(utxo) {
  const address = addressFromHash160Hex(utxo.addressHash160);
  const lockingScript = fromHex(utxo.lockingScriptHex);
  const scriptType =
    utxo.scriptType === "dstas"
      ? ScriptType.dstas
      : utxo.scriptType === "p2stas"
        ? ScriptType.p2stas
        : ScriptType.p2pkh;

  return new OutPoint(
    utxo.txId,
    utxo.vout,
    lockingScript,
    utxo.satoshis,
    address,
    scriptType,
  );
}

/**
 * Build a TokenScheme for STAS token.
 */
function buildTokenScheme(params) {
  return new TokenScheme(
    params.name || "STAS",
    params.tokenId,
    params.symbol || "STAS",
    params.satoshisPerToken || 1,
    {
      freeze: params.freeze !== undefined ? params.freeze : false,
      confiscation:
        params.confiscation !== undefined ? params.confiscation : false,
      isDivisible:
        params.isDivisible !== undefined ? params.isDivisible : true,
    },
  );
}

// --- Health Check ---
app.get("/healthz", (_req, res) => {
  res.json({ status: "ok", service: "stas-cannon-stas-service" });
});

// --- Issue: Contract TX + Issue TX ---
/**
 * POST /issue
 * Body: {
 *   privkeyHex: string,          // 32-byte private key hex
 *   fundingUtxo: { txId, vout, satoshis, lockingScriptHex, addressHash160, scriptType },
 *   scheme: { name, tokenId, symbol, satoshisPerToken, freeze, confiscation, isDivisible },
 *   destinations: [{ satoshis, toHash160 }],
 *   feeRate?: number
 * }
 * Returns: { contractTxHex, issueTxHex, contractTxId, issueTxId }
 */
app.post("/issue", (req, res) => {
  try {
    const { privkeyHex, fundingUtxo, scheme, destinations, feeRate } = req.body;

    const owner = privkeyFromHex(privkeyHex);
    const fundingOutPoint = buildOutPoint(fundingUtxo);
    const tokenScheme = buildTokenScheme(scheme);

    const dests = destinations.map((d) => ({
      Satoshis: d.satoshis,
      To: addressFromHash160Hex(d.toHash160),
    }));

    const result = BuildDstasIssueTxs({
      fundingPayment: { OutPoint: fundingOutPoint, Owner: owner },
      scheme: tokenScheme,
      destinations: dests,
      feeRate: feeRate || 0.1,
    });

    // Parse TXIDs from raw hex
    const contractTx = TransactionReader.readHex(result.contractTxHex);
    const issueTx = TransactionReader.readHex(result.issueTxHex);

    // Extract issue TX outputs info for tracking
    const issueOutputs = issueTx.Outputs.map((out, idx) => ({
      vout: idx,
      satoshis: out.Satoshis,
      scriptType: out.ScriptType === ScriptType.dstas ? "dstas" : "p2pkh",
      lockingScriptHex: toHex(out.LockingScript),
      addressHash160: out.Address ? toHex(out.Address.Hash160) : null,
    }));

    res.json({
      contractTxHex: result.contractTxHex,
      issueTxHex: result.issueTxHex,
      contractTxId: contractTx.Id,
      issueTxId: issueTx.Id,
      issueOutputs,
    });
  } catch (err) {
    res.status(400).json({ error: err.message, devMessage: err.devMessage || null });
  }
});

// --- Split DSTAS ---
/**
 * POST /split
 * Body: {
 *   privkeyHex: string,
 *   stasUtxo: { txId, vout, satoshis, lockingScriptHex, addressHash160, scriptType },
 *   feeUtxo: { txId, vout, satoshis, lockingScriptHex, addressHash160, scriptType },
 *   destinations: [{ satoshis, toHash160 }],
 *   scheme: { ... },
 *   feeRate?: number
 * }
 * Returns: { txHex, txId, outputs }
 */
app.post("/split", (req, res) => {
  try {
    const { privkeyHex, stasUtxo, feeUtxo, destinations, scheme, feeRate } =
      req.body;

    const owner = privkeyFromHex(privkeyHex);
    const stasOutPoint = buildOutPoint(stasUtxo);
    const feeOutPoint = buildOutPoint(feeUtxo);
    const tokenScheme = buildTokenScheme(scheme);

    const dests = destinations.map((d) => ({
      Satoshis: d.satoshis,
      To: addressFromHash160Hex(d.toHash160),
    }));

    const txHex = BuildDstasSplitTx({
      stasPayment: { OutPoint: stasOutPoint, Owner: owner },
      feePayment: { OutPoint: feeOutPoint, Owner: owner },
      destinations: dests,
      Scheme: tokenScheme,
      feeRate: feeRate || 0.1,
    });

    const tx = TransactionReader.readHex(txHex);
    const outputs = tx.Outputs.map((out, idx) => ({
      vout: idx,
      satoshis: out.Satoshis,
      scriptType:
        out.ScriptType === ScriptType.dstas
          ? "dstas"
          : out.ScriptType === ScriptType.p2stas
            ? "p2stas"
            : "p2pkh",
      lockingScriptHex: toHex(out.LockingScript),
      addressHash160: out.Address ? toHex(out.Address.Hash160) : null,
    }));

    res.json({ txHex, txId: tx.Id, outputs });
  } catch (err) {
    console.error(`[SPLIT ERROR] message=${err.message}, devMessage=${err.devMessage || 'N/A'}`);
    res.status(400).json({ error: err.message, devMessage: err.devMessage || null });
  }
});

// --- Transfer DSTAS ---
/**
 * POST /transfer
 * Body: {
 *   privkeyHex: string,
 *   stasUtxo: { txId, vout, satoshis, lockingScriptHex, addressHash160, scriptType },
 *   feeUtxo: { txId, vout, satoshis, lockingScriptHex, addressHash160, scriptType },
 *   toHash160: string,
 *   scheme: { ... },
 *   feeRate?: number
 * }
 * Returns: { txHex, txId, outputs }
 */
app.post("/transfer", (req, res) => {
  try {
    const { privkeyHex, stasUtxo, feeUtxo, toHash160, scheme, feeRate } =
      req.body;

    const owner = privkeyFromHex(privkeyHex);
    const stasOutPoint = buildOutPoint(stasUtxo);
    const feeOutPoint = buildOutPoint(feeUtxo);
    const tokenScheme = buildTokenScheme(scheme);
    const toAddress = addressFromHash160Hex(toHash160);

    const txHex = BuildDstasTransferTx({
      stasPayment: { OutPoint: stasOutPoint, Owner: owner },
      feePayment: { OutPoint: feeOutPoint, Owner: owner },
      destination: {
        Satoshis: stasOutPoint.Satoshis,
        To: toAddress,
      },
      Scheme: tokenScheme,
      feeRate: feeRate || 0.1,
    });

    const tx = TransactionReader.readHex(txHex);
    const outputs = tx.Outputs.map((out, idx) => ({
      vout: idx,
      satoshis: out.Satoshis,
      scriptType:
        out.ScriptType === ScriptType.dstas
          ? "dstas"
          : out.ScriptType === ScriptType.p2stas
            ? "p2stas"
            : "p2pkh",
      lockingScriptHex: toHex(out.LockingScript),
      addressHash160: out.Address ? toHex(out.Address.Hash160) : null,
    }));

    res.json({ txHex, txId: tx.Id, outputs });
  } catch (err) {
    res.status(400).json({ error: err.message });
  }
});

// --- Batch Transfer (multiple transfers in one call) ---
/**
 * POST /batch-transfer
 * Body: {
 *   privkeyHex: string,
 *   transfers: [{
 *     stasUtxo: { ... },
 *     feeUtxo: { ... },
 *     toHash160: string
 *   }],
 *   scheme: { ... },
 *   feeRate?: number
 * }
 * Returns: { results: [{ txHex, txId, outputs }] }
 */
app.post("/batch-transfer", (req, res) => {
  try {
    const { privkeyHex, transfers, scheme, feeRate } = req.body;
    const owner = privkeyFromHex(privkeyHex);
    const tokenScheme = buildTokenScheme(scheme);
    const rate = feeRate || 0.1;

    const results = transfers.map((t) => {
      const stasOutPoint = buildOutPoint(t.stasUtxo);
      const feeOutPoint = buildOutPoint(t.feeUtxo);
      const toAddress = addressFromHash160Hex(t.toHash160);

      const txHex = BuildDstasTransferTx({
        stasPayment: { OutPoint: stasOutPoint, Owner: owner },
        feePayment: { OutPoint: feeOutPoint, Owner: owner },
        destination: {
          Satoshis: stasOutPoint.Satoshis,
          To: toAddress,
        },
        Scheme: tokenScheme,
        feeRate: rate,
      });

      const tx = TransactionReader.readHex(txHex);
      const outputs = tx.Outputs.map((out, idx) => ({
        vout: idx,
        satoshis: out.Satoshis,
        scriptType:
          out.ScriptType === ScriptType.dstas
            ? "dstas"
            : out.ScriptType === ScriptType.p2stas
              ? "p2stas"
              : "p2pkh",
        lockingScriptHex: toHex(out.LockingScript),
        addressHash160: out.Address ? toHex(out.Address.Hash160) : null,
      }));

      return { txHex, txId: tx.Id, outputs };
    });

    res.json({ results });
  } catch (err) {
    res.status(400).json({ error: err.message });
  }
});

// --- P2PKH Split (for splitting fee UTXOs) ---
/**
 * POST /p2pkh-split
 * Body: {
 *   privkeyHex: string,
 *   utxo: { txId, vout, satoshis, lockingScriptHex, addressHash160, scriptType },
 *   outputs: [{ satoshis }],
 *   feeRate?: number
 * }
 * Returns: { txHex, txId, outputs }
 */
app.post("/p2pkh-split", (req, res) => {
  try {
    const { privkeyHex, utxo, outputs: outputDefs, feeRate } = req.body;
    const owner = privkeyFromHex(privkeyHex);
    const outPoint = buildOutPoint(utxo);
    const rate = feeRate || 0.1;

    const txBuilder = TransactionBuilder.init().addInput(outPoint, owner);

    const ownerAddress = outPoint.Address;
    let totalOutputSats = 0;
    for (const od of outputDefs) {
      txBuilder.addP2PkhOutput(od.satoshis, ownerAddress);
      totalOutputSats += od.satoshis;
    }

    const feeOutputIdx = txBuilder.Outputs.length;
    const changeBudget = outPoint.Satoshis - totalOutputSats;

    if (changeBudget > 0) {
      txBuilder.addChangeOutputWithFee(
        ownerAddress,
        changeBudget,
        rate,
        feeOutputIdx,
      );
    }

    const txHex = txBuilder.sign().toHex();
    const tx = TransactionReader.readHex(txHex);
    const txOutputs = tx.Outputs.map((out, idx) => ({
      vout: idx,
      satoshis: out.Satoshis,
      scriptType: "p2pkh",
      lockingScriptHex: toHex(out.LockingScript),
      addressHash160: out.Address ? toHex(out.Address.Hash160) : null,
    }));

    res.json({ txHex, txId: tx.Id, outputs: txOutputs });
  } catch (err) {
    res.status(400).json({ error: err.message });
  }
});

/**
 * POST /consolidate
 * Merge multiple P2PKH UTXOs into a single output.
 * Body: {
 *   privkeyHex: string,
 *   utxos: [{ txId, vout, satoshis, lockingScriptHex, addressHash160, scriptType }],
 *   feeRate?: number
 * }
 * Returns: { txHex, txId, outputs }
 */
app.post("/consolidate", (req, res) => {
  try {
    const { privkeyHex, utxos, feeRate } = req.body;
    const owner = privkeyFromHex(privkeyHex);
    const rate = feeRate || 0.1;

    const txBuilder = TransactionBuilder.init();
    let totalInputSats = 0;
    for (const utxo of utxos) {
      const outPoint = buildOutPoint(utxo);
      txBuilder.addInput(outPoint, owner);
      totalInputSats += utxo.satoshis;
    }

    const ownerAddress = owner.Address;
    // Add single output with all funds minus fee
    const feeOutputIdx = 0;
    txBuilder.addChangeOutputWithFee(ownerAddress, totalInputSats, rate, feeOutputIdx);

    const txHex = txBuilder.sign().toHex();
    const tx = TransactionReader.readHex(txHex);
    const txOutputs = tx.Outputs.map((out, idx) => ({
      vout: idx,
      satoshis: out.Satoshis,
      scriptType: "p2pkh",
      lockingScriptHex: toHex(out.LockingScript),
      addressHash160: out.Address ? toHex(out.Address.Hash160) : null,
    }));

    res.json({ txHex, txId: tx.Id, outputs: txOutputs });
  } catch (err) {
    res.status(400).json({ error: err.message });
  }
});

// --- Split Fee: Consolidate UTXOs and split into N equal outputs ---
/**
 * POST /split-fee
 * Consolidates multiple P2PKH UTXOs and splits into N equal outputs.
 * Used for creating independent fee chains for concurrent transfers.
 * Body: {
 *   privkeyHex: string,
 *   utxos: [{ txId, vout, satoshis, lockingScriptHex, addressHash160, scriptType }],
 *   numOutputs: number,
 *   feeRate?: number
 * }
 * Returns: { txHex, txId, outputs }
 */
app.post("/split-fee", (req, res) => {
  try {
    const { privkeyHex, utxos, numOutputs, feeRate } = req.body;
    const owner = privkeyFromHex(privkeyHex);
    const rate = feeRate || 0.1;
    const num = numOutputs || 2;

    const txBuilder = TransactionBuilder.init();
    let totalInputSats = 0;
    for (const utxo of utxos) {
      const outPoint = buildOutPoint(utxo);
      txBuilder.addInput(outPoint, owner);
      totalInputSats += utxo.satoshis;
    }

    const ownerAddress = owner.Address;
    // Estimate fee: ~34 bytes per output + ~148 bytes per input + 10 overhead
    const estimatedTxSize = utxos.length * 148 + num * 34 + 10;
    const estimatedFee = Math.ceil(estimatedTxSize * rate) + 100;
    const availableSats = totalInputSats - estimatedFee;
    const perOutputSats = Math.floor(availableSats / num);

    if (perOutputSats < 1) {
      throw new Error(`Insufficient funds: ${totalInputSats} sats for ${num} outputs (need ~${estimatedFee} fee)`);
    }

    // Add N equal outputs
    for (let i = 0; i < num; i++) {
      txBuilder.addP2PkhOutput(perOutputSats, ownerAddress);
    }

    // Any remainder goes as change (covers fee)
    const remainder = availableSats - perOutputSats * num;
    if (remainder > 0) {
      txBuilder.addP2PkhOutput(remainder, ownerAddress);
    }

    const txHex = txBuilder.sign().toHex();
    const tx = TransactionReader.readHex(txHex);
    const txOutputs = tx.Outputs.map((out, idx) => ({
      vout: idx,
      satoshis: out.Satoshis,
      scriptType: "p2pkh",
      lockingScriptHex: toHex(out.LockingScript),
      addressHash160: out.Address ? toHex(out.Address.Hash160) : null,
    }));

    res.json({ txHex, txId: tx.Id, outputs: txOutputs });
  } catch (err) {
    console.error(`[SPLIT-FEE ERROR] ${err.message}`);
    res.status(400).json({ error: err.message });
  }
});

// --- Utility: Derive address info from private key ---
/**
 * POST /derive
 * Body: { privkeyHex: string }
 * Returns: { addressHash160, pubkeyHex, mainnetAddress }
 */
app.post("/derive", (req, res) => {
  try {
    const { privkeyHex } = req.body;
    const pk = privkeyFromHex(privkeyHex);
    res.json({
      addressHash160: toHex(pk.Address.Hash160),
      pubkeyHex: toHex(pk.PublicKey),
      mainnetAddress: pk.Address.Value,
    });
  } catch (err) {
    res.status(400).json({ error: err.message });
  }
});

// --- Utility: Parse transaction hex ---
/**
 * POST /parse-tx
 * Body: { txHex: string }
 * Returns: { txId, inputs, outputs }
 */
app.post("/parse-tx", (req, res) => {
  try {
    const { txHex } = req.body;
    const tx = TransactionReader.readHex(txHex);

    const inputs = tx.Inputs.map((inp, idx) => ({
      index: idx,
      prevTxId: inp.PreviousOutPoint
        ? toHex(
            new Uint8Array(inp.PreviousOutPoint.slice(0, 32)).reverse(),
          )
        : null,
      prevVout: inp.PreviousOutPoint
        ? new DataView(inp.PreviousOutPoint.buffer).getUint32(32, true)
        : null,
    }));

    const outputs = tx.Outputs.map((out, idx) => {
      let scriptTypeStr = "unknown";
      try {
        const reader = LockingScriptReader.read(out.LockingScript);
        if (reader.ScriptType === ScriptType.dstas) scriptTypeStr = "dstas";
        else if (reader.ScriptType === ScriptType.p2stas)
          scriptTypeStr = "p2stas";
        else if (reader.ScriptType === ScriptType.p2pkh)
          scriptTypeStr = "p2pkh";
      } catch {
        scriptTypeStr = "unknown";
      }

      return {
        vout: idx,
        satoshis: out.Satoshis,
        scriptType: scriptTypeStr,
        lockingScriptHex: toHex(out.LockingScript),
        addressHash160: out.Address ? toHex(out.Address.Hash160) : null,
      };
    });

    res.json({ txId: tx.Id, inputs, outputs });
  } catch (err) {
    res.status(400).json({ error: err.message });
  }
});

// --- Start Server ---
app.listen(PORT, () => {
  console.log(`STAS Service running on port ${PORT}`);
});
