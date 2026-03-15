import { useState, useEffect, useRef, useCallback } from 'react'
import { Link } from 'react-router-dom'
import './App.css'

const API_URL = import.meta.env.VITE_API_URL || ''

export type AppMode = 'localtest' | 'bsvtestnet' | 'bsvmainnet'

type Phase = 'idle' | 'power_charge' | 'build' | 'broadcast' | 'confirm' | 'done'

interface ProgressData {
  phase: Phase
  current: number
  total: number
  percent: number
  tps?: number
  errors?: number
  status?: string
}

interface Stats {
  txBuilt: number
  txBroadcast: number
  txConfirmed: number
  txErrors: number
  tps: number
  buildDuration: number
  broadcastDuration: number
  totalDuration: number
  senderAddress: string
  receiverAddress: string
}

interface WalletInfo {
  address: string
  balanceBsv: number
  balanceSatoshis: number
  funded: boolean
}

// Estimated times per section based on transfer count and mode (in seconds)
// Based on actual testnet measurements: 10 transfers ~ 15s charge, 3s launch, 8s confirm
function getEstimatedTimes(mode: AppMode, count: number): { charge: string; launch: string; confirm: string; total: string } {
  if (mode === 'localtest') {
    const chargeSec = Math.ceil(count / 50000) * 2
    const launchSec = 10
    const confirmSec = Math.ceil(count / 50000) * 2
    const totalSec = chargeSec + launchSec + confirmSec
    return {
      charge: `${chargeSec < 60 ? `${chargeSec}秒` : `${Math.ceil(chargeSec / 60)}分`}`,
      launch: '10秒',
      confirm: `${confirmSec < 60 ? `${confirmSec}秒` : `${Math.ceil(confirmSec / 60)}分`}`,
      total: `${totalSec < 60 ? `${totalSec}秒` : `${Math.ceil(totalSec / 60)}分`}`,
    }
  }
  // Real blockchain modes: per-TX overhead varies by concurrency
  // With 10 concurrent groups, throughput is ~10x single-chain
  const concurrency = count >= 1000 ? 10 : 1
  const chargeSec = Math.ceil(15 + (count / Math.min(count, 500)) * 3) // batched issuance
  const launchSec = Math.ceil(3 + (count * 0.5) / concurrency) // parallel transfer groups
  const confirmSec = Math.ceil(5 + (count * 0.3) / concurrency)
  const totalSec = chargeSec + launchSec + confirmSec
  const fmt = (s: number) => {
    if (s < 60) return `約${s}秒`
    if (s < 3600) return `約${Math.ceil(s / 60)}分`
    const h = Math.floor(s / 3600)
    const m = Math.ceil((s % 3600) / 60)
    return `約${h}時間${m > 0 ? m + '分' : ''}`
  }
  return { charge: fmt(chargeSec), launch: fmt(launchSec), confirm: fmt(confirmSec), total: fmt(totalSec) }
}

const MODE_CONFIG = {
  localtest: {
    label: 'Local Test',
    sublabel: 'シミュレーション',
    color: 'bg-gray-600',
    textColor: 'text-gray-300',
    borderColor: 'border-gray-600',
    transferOptions: [1000, 10000, 100000, 1000000],
    needsWallet: false,
    explorerBaseUrl: 'https://test.whatsonchain.com',
    bitailsBaseUrl: 'https://test.bitails.io',
  },
  bsvtestnet: {
    label: 'Testnet',
    sublabel: 'テストネット',
    color: 'bg-yellow-600',
    textColor: 'text-yellow-300',
    borderColor: 'border-yellow-600',
    transferOptions: [10, 100, 1000, 10000],
    needsWallet: true,
    explorerBaseUrl: 'https://test.whatsonchain.com',
    bitailsBaseUrl: 'https://test.bitails.io',
  },
  bsvmainnet: {
    label: 'Mainnet',
    sublabel: 'メインネット',
    color: 'bg-red-600',
    textColor: 'text-red-300',
    borderColor: 'border-red-600',
    transferOptions: [10, 100, 1000, 10000, 100000, 1000000],
    needsWallet: true,
    explorerBaseUrl: 'https://whatsonchain.com',
    bitailsBaseUrl: 'https://bitails.io',
  },
}

function App({ mode }: { mode: AppMode }) {
  const config = MODE_CONFIG[mode]
  const [phase, setPhase] = useState<Phase>('idle')
  const [totalTransfers, setTotalTransfers] = useState(config.transferOptions[config.transferOptions.length - 1])
  const [progress, setProgress] = useState<ProgressData | null>(null)
  const [stats, setStats] = useState<Stats>({
    txBuilt: 0, txBroadcast: 0, txConfirmed: 0, txErrors: 0,
    tps: 0, buildDuration: 0, broadcastDuration: 0, totalDuration: 0,
    senderAddress: '', receiverAddress: '',
  })
  const [connected, setConnected] = useState(false)
  const [configured, setConfigured] = useState(false)
  const [chargeComplete, setChargeComplete] = useState(false)
  const [utxosPrepared, setUtxosPrepared] = useState(0)
  const [launchComplete, setLaunchComplete] = useState(false)
  const [transfersSent, setTransfersSent] = useState(0)
  const [txIds, setTxIds] = useState<string[]>([])

  // Wallet state for testnet/mainnet
  const [wallet, setWallet] = useState<WalletInfo | null>(null)
  const [walletLoading, setWalletLoading] = useState(false)
  const [wifInput, setWifInput] = useState(mode === 'bsvtestnet' ? 'cQi4Q2u1eQzovYvupSQQrEh9Rimh6cEio9wYzkbrQNkp1adCeY6F' : '')
  const [walletError, setWalletError] = useState('')
  const [phaseError, setPhaseError] = useState('')

  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const connectWs = useCallback(() => {
    if (wsRef.current) {
      wsRef.current.onclose = null
      wsRef.current.close()
      wsRef.current = null
    }
    if (reconnectTimer.current) {
      clearTimeout(reconnectTimer.current)
      reconnectTimer.current = null
    }

    const wsUrl = API_URL
      ? API_URL.replace('http', 'ws') + `/ws/cannon?mode=${mode}`
      : `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws/cannon?mode=${mode}`
    const ws = new WebSocket(wsUrl)

    ws.onopen = () => {
      setConnected(true)
    }

    ws.onclose = () => {
      // Only reconnect if this is still the active connection
      if (wsRef.current === ws) {
        setConnected(false)
        reconnectTimer.current = setTimeout(connectWs, 2000)
      }
    }

    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data)
      switch (msg.type) {
        case 'configured':
          setConfigured(true)
          setChargeComplete(false)
          setLaunchComplete(false)
          setStats(prev => ({
            ...prev,
            senderAddress: msg.sender_address,
            receiverAddress: msg.receiver_address,
          }))
          break

        case 'phase':
          setPhase(msg.phase)
          break

        case 'progress':
          setProgress({
            phase: msg.phase,
            current: msg.current,
            total: msg.total,
            percent: msg.percent,
            tps: msg.tps,
            errors: msg.errors,
            status: msg.status,
          })
          if (msg.phase === 'power_charge') {
            setStats(prev => ({ ...prev }))
          }
          if (msg.tps) {
            setStats(prev => ({ ...prev, tps: msg.tps, txBroadcast: msg.current, txErrors: msg.errors || 0 }))
          }
          if (msg.phase === 'build') {
            setStats(prev => ({ ...prev, txBuilt: msg.current }))
          }
          if (msg.phase === 'confirm') {
            setStats(prev => ({ ...prev, txConfirmed: msg.current }))
          }
          break

        case 'phase_complete':
          if (msg.phase === 'power_charge') {
            setChargeComplete(true)
            setUtxosPrepared(msg.utxos_prepared || 0)
            setPhase('idle')
          }
          if (msg.phase === 'launch') {
            setLaunchComplete(true)
            setTransfersSent(msg.tx_broadcast || 0)
            setPhase('idle')
            setStats(prev => ({
              ...prev,
              txBuilt: msg.tx_built,
              txBroadcast: msg.tx_broadcast,
              buildDuration: msg.build_duration,
              broadcastDuration: msg.broadcast_duration,
              tps: msg.avg_tps,
            }))
          }
          if (msg.phase === 'confirm') {
            setStats(prev => ({ ...prev, txConfirmed: msg.tx_confirmed }))
          }
          break

        case 'complete':
          setPhase('done')
          setStats(prev => ({
            ...prev,
            totalDuration: msg.total_duration,
            txBroadcast: msg.tx_broadcast,
            txConfirmed: msg.tx_confirmed,
            txErrors: msg.tx_errors,
            tps: msg.tps || prev.tps,
            buildDuration: msg.build_duration || prev.buildDuration,
            broadcastDuration: msg.broadcast_duration || prev.broadcastDuration,
          }))
          if (msg.tx_ids) {
            setTxIds(msg.tx_ids)
          }
          break

        case 'wallet_created':
          setWallet({
            address: msg.address,
            balanceBsv: msg.balance_bsv || 0,
            balanceSatoshis: msg.balance_satoshis || 0,
            funded: msg.funded || false,
          })
          setWalletLoading(false)
          setWalletError('')
          break

        case 'wallet_balance':
          setWallet(prev => prev ? {
            ...prev,
            balanceBsv: msg.balance_bsv,
            balanceSatoshis: msg.balance_satoshis,
            funded: msg.funded,
          } : null)
          setWalletLoading(false)
          break

        case 'wallet_error':
          setWalletError(msg.message)
          setWalletLoading(false)
          break

        case 'error':
          setPhaseError(msg.message)
          setPhase('idle')
          break

        case 'stopped':
          setPhase('idle')
          break
      }
    }

    wsRef.current = ws
  }, [mode])

  useEffect(() => {
    connectWs()
    return () => {
      if (reconnectTimer.current) {
        clearTimeout(reconnectTimer.current)
        reconnectTimer.current = null
      }
      if (wsRef.current) {
        wsRef.current.onclose = null
        wsRef.current.close()
        wsRef.current = null
      }
    }
  }, [connectWs])

  const handleConfigure = () => {
    wsRef.current?.send(JSON.stringify({
      action: 'configure',
      total_transfers: totalTransfers,
    }))
  }

  const handleCharge = () => {
    setProgress(null)
    setPhaseError('')
    setStats({
      txBuilt: 0, txBroadcast: 0, txConfirmed: 0, txErrors: 0,
      tps: 0, buildDuration: 0, broadcastDuration: 0, totalDuration: 0,
      senderAddress: stats.senderAddress, receiverAddress: stats.receiverAddress,
    })
    wsRef.current?.send(JSON.stringify({ action: 'charge' }))
  }

  const handleLaunch = () => {
    setProgress(null)
    wsRef.current?.send(JSON.stringify({ action: 'launch' }))
  }

  const handleConfirm = () => {
    setProgress(null)
    wsRef.current?.send(JSON.stringify({ action: 'confirm' }))
  }

  const handleStop = () => {
    wsRef.current?.send(JSON.stringify({ action: 'stop' }))
  }

  const handleCreateWallet = () => {
    setWalletLoading(true)
    setWalletError('')
    wsRef.current?.send(JSON.stringify({ action: 'create_wallet' }))
  }

  const handleImportWallet = () => {
    if (!wifInput.trim()) return
    setWalletLoading(true)
    setWalletError('')
    wsRef.current?.send(JSON.stringify({ action: 'import_wallet', wif: wifInput.trim() }))
  }

  const handleCheckBalance = () => {
    setWalletLoading(true)
    wsRef.current?.send(JSON.stringify({ action: 'check_balance' }))
  }

  const formatNumber = (n: number) => n.toLocaleString()
  const isRunning = phase !== 'idle' && phase !== 'done'
  const canProceed = mode === 'localtest' || (wallet?.funded ?? false)

  return (
    <div className="min-h-screen bg-gray-950 text-white">
      {/* Header */}
      <header className="border-b border-gray-800 bg-gray-950/80 backdrop-blur-sm sticky top-0 z-10">
        <div className="max-w-6xl mx-auto px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Link to="/" className="w-10 h-10 rounded-lg bg-gradient-to-br from-orange-500 to-red-600 flex items-center justify-center font-bold text-lg hover:scale-105 transition-transform">
              S
            </Link>
            <div>
              <h1 className="text-xl font-bold tracking-tight">STAS CANNON</h1>
              <p className="text-xs text-gray-500">高速STAS送金システム</p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <span className={`text-[10px] font-bold uppercase tracking-widest px-2.5 py-1 rounded ${config.color} text-white`}>
              {config.label}
            </span>
            <div className="flex items-center gap-2">
              <div className={`w-2 h-2 rounded-full ${connected ? 'bg-green-500' : 'bg-red-500'}`} />
              <span className="text-xs text-gray-500">{connected ? '接続済み' : '未接続'}</span>
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-6 py-8 space-y-8">
        {/* Wallet Setup (testnet/mainnet only) */}
        {config.needsWallet && (
          <section className={`bg-gray-900 rounded-2xl border ${config.borderColor}/30 p-6 space-y-4`}>
            <div className="flex items-center gap-3">
              <div className={`w-8 h-8 rounded-full text-sm font-bold flex items-center justify-center ${wallet ? 'bg-green-500' : config.color} text-white`}>
                {wallet ? '\u2713' : '\uD83D\uDCB3'}
              </div>
              <h2 className="text-lg font-semibold">ウォレット設定</h2>
              <span className={`text-xs ${config.textColor}`}>{config.sublabel}</span>
            </div>

            {!wallet ? (
              <div className="space-y-4">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  <button
                    onClick={handleCreateWallet}
                    disabled={!connected || walletLoading}
                    className={`px-6 py-3 bg-gradient-to-r ${mode === 'bsvtestnet' ? 'from-yellow-500 to-amber-600 shadow-yellow-500/20' : 'from-red-500 to-rose-600 shadow-red-500/20'} hover:opacity-90 rounded-lg font-bold transition-all disabled:opacity-50 disabled:cursor-not-allowed shadow-lg`}
                  >
                    {walletLoading ? '作成中...' : '新規ウォレット生成'}
                  </button>
                  <div className="flex gap-2">
                    <input
                      type="text"
                      value={wifInput}
                      onChange={(e) => setWifInput(e.target.value)}
                      placeholder="WIF秘密鍵を入力..."
                      className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-sm text-white focus:outline-none focus:border-orange-500"
                    />
                    <button
                      onClick={handleImportWallet}
                      disabled={!connected || walletLoading || !wifInput.trim()}
                      className="px-4 py-2.5 bg-gray-700 hover:bg-gray-600 rounded-lg font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed text-sm"
                    >
                      インポート
                    </button>
                  </div>
                </div>
                {walletError && (
                  <div className="text-red-400 text-sm bg-red-950/30 border border-red-800/30 rounded-lg px-4 py-2">
                    {walletError}
                  </div>
                )}
              </div>
            ) : (
              <div className="space-y-4">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-sm">
                  <div className="bg-gray-800/50 rounded-lg px-4 py-3">
                    <span className="text-gray-500 text-xs block mb-1">アドレス</span>
                    <span className="font-mono text-xs break-all">{wallet.address}</span>
                  </div>
                  <div className="bg-gray-800/50 rounded-lg px-4 py-3">
                    <span className="text-gray-500 text-xs block mb-1">残高</span>
                    <span className={`font-bold text-lg ${wallet.funded ? 'text-green-400' : 'text-red-400'}`}>
                      {wallet.balanceBsv.toFixed(8)} {mode === 'bsvtestnet' ? 'tBSV' : 'BSV'}
                    </span>
                    <span className="text-gray-500 text-xs ml-2">({wallet.balanceSatoshis.toLocaleString()} sat)</span>
                  </div>
                </div>

                <div className="flex flex-wrap gap-3">
                  <button
                    onClick={handleCheckBalance}
                    disabled={walletLoading}
                    className="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg text-sm font-medium transition-colors disabled:opacity-50"
                  >
                    {walletLoading ? '確認中...' : '残高更新'}
                  </button>
                  {mode === 'bsvtestnet' && (
                    <a
                      href="https://faucet.bitcoincloud.net/"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="px-4 py-2 bg-yellow-600/20 border border-yellow-600/30 hover:bg-yellow-600/30 rounded-lg text-sm font-medium text-yellow-400 transition-colors"
                    >
                      tBSV フォーセット &rarr;
                    </a>
                  )}
                  {!wallet.funded && (
                    <p className="text-yellow-400 text-xs self-center">
                      {mode === 'bsvtestnet' ? 'tBSV' : 'BSV'}を上記アドレスに送金してください
                    </p>
                  )}
                </div>
              </div>
            )}
          </section>
        )}

        {/* Configuration Panel */}
        <section className="bg-gray-900 rounded-2xl border border-gray-800 p-6">
          <h2 className="text-lg font-semibold mb-4">設定</h2>
          {mode !== 'localtest' && (
            <div className="text-xs text-gray-500 bg-gray-800/30 rounded-lg px-3 py-2 mb-3">
              合計推定時間: {getEstimatedTimes(mode, totalTransfers).total}
              <span className="text-gray-600 ml-1">(チャージ {getEstimatedTimes(mode, totalTransfers).charge} + 送金 {getEstimatedTimes(mode, totalTransfers).launch} + 確認 {getEstimatedTimes(mode, totalTransfers).confirm})</span>
            </div>
          )}
          <div className="flex flex-col sm:flex-row gap-4 items-end">
            <div className="flex-1">
              <label className="block text-sm text-gray-400 mb-1">送金件数</label>
              <select
                value={totalTransfers}
                onChange={(e) => setTotalTransfers(Number(e.target.value))}
                disabled={isRunning || chargeComplete}
                className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-white focus:outline-none focus:border-orange-500 disabled:opacity-50"
              >
                  {config.transferOptions.map(v => (
                    <option key={v} value={v}>{v.toLocaleString()}</option>
                  ))}
              </select>
            </div>
            <button
              onClick={handleConfigure}
              disabled={isRunning || !connected || chargeComplete || !canProceed}
              className="px-6 py-2.5 bg-gray-700 hover:bg-gray-600 rounded-lg font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              初期化
            </button>
            {isRunning && (
              <button
                onClick={handleStop}
                className="px-8 py-2.5 bg-gray-700 hover:bg-gray-600 rounded-lg font-medium transition-colors"
              >
                停止
              </button>
            )}
          </div>

          {!canProceed && config.needsWallet && (
            <p className="mt-3 text-yellow-400 text-sm">
              先にウォレットを設定し、{mode === 'bsvtestnet' ? 'tBSV' : 'BSV'}をチャージしてください
            </p>
          )}

          {configured && stats.senderAddress && (
            <div className="mt-4 grid grid-cols-1 sm:grid-cols-2 gap-3 text-sm">
              <div className="bg-gray-800/50 rounded-lg px-4 py-2">
                <span className="text-gray-500">Sender: </span>
                <span className="font-mono text-xs">{stats.senderAddress}</span>
              </div>
              <div className="bg-gray-800/50 rounded-lg px-4 py-2">
                <span className="text-gray-500">Receiver: </span>
                <span className="font-mono text-xs">{stats.receiverAddress}</span>
              </div>
            </div>
          )}
        </section>

        {/* Step 1: Charge */}
        {configured && (
          <section className="bg-gray-900 rounded-2xl border border-gray-800 p-6 space-y-4">
            <div className="flex items-center gap-3">
              <div className={`w-8 h-8 rounded-full text-sm font-bold flex items-center justify-center ${
                chargeComplete ? 'bg-green-500 text-white' :
                phase === 'power_charge' ? 'bg-orange-500 text-white animate-pulse' :
                'bg-yellow-500 text-white'
              }`}>
                {chargeComplete ? '✓' : '1'}
              </div>
              <h3 className="text-xl font-bold">チャージ</h3>
              {chargeComplete && (
                <span className="text-green-400 text-sm">{formatNumber(utxosPrepared)} UTXOs 準備完了</span>
              )}
            </div>

            {phase === 'power_charge' && progress && (
              <>
                <div className="flex items-center justify-between text-sm">
                  <span className="text-gray-400">{progress.status || 'UTXO分割中...'}</span>
                  <span className="font-bold tabular-nums">{progress.percent.toFixed(1)}%</span>
                </div>
                <div className="w-full bg-gray-800 rounded-full h-3 overflow-hidden">
                  <div
                    className="h-full rounded-full transition-all duration-300 bg-gradient-to-r from-yellow-500 to-orange-500"
                    style={{ width: `${Math.min(progress.percent, 100)}%` }}
                  />
                </div>
              </>
            )}

            {phaseError && (
              <div className="bg-red-900/30 border border-red-700 rounded-lg p-3 text-red-400 text-sm">
                エラー: {phaseError}
              </div>
            )}

            {!chargeComplete && phase !== 'power_charge' && (
              <>
                <div className="text-xs text-gray-500 bg-gray-800/50 rounded-lg px-3 py-2">
                  推定所要時間: {getEstimatedTimes(mode, totalTransfers).charge}
                  <span className="text-gray-600 ml-2">(トークン発行 + UTXO分割)</span>
                </div>
                <button
                  onClick={handleCharge}
                  disabled={!connected || isRunning}
                  className="w-full px-8 py-3 bg-gradient-to-r from-yellow-500 to-orange-500 hover:from-yellow-400 hover:to-orange-400 rounded-lg font-bold text-lg transition-all disabled:opacity-50 disabled:cursor-not-allowed shadow-lg shadow-yellow-500/20"
                >
                  ⚡ チャージ開始
                </button>
              </>
            )}
          </section>
        )}

        {/* Step 2: Transfer */}
        {chargeComplete && (
          <section className="bg-gray-900 rounded-2xl border border-gray-800 p-6 space-y-4">
            <div className="flex items-center gap-3">
              <div className={`w-8 h-8 rounded-full text-sm font-bold flex items-center justify-center ${
                launchComplete ? 'bg-green-500 text-white' :
                (phase === 'build' || phase === 'broadcast') ? 'bg-orange-500 text-white animate-pulse' :
                'bg-red-500 text-white'
              }`}>
                {launchComplete ? '✓' : '2'}
              </div>
              <h3 className="text-xl font-bold">
                {mode === 'localtest' ? '送金時間（10s）' : '送金'}
              </h3>
              {launchComplete && (
                <span className="text-green-400 text-sm">{formatNumber(transfersSent)} 送信完了</span>
              )}
            </div>

            {(phase === 'build' || phase === 'broadcast') && progress && (
              <>
                <div className="flex items-center justify-between text-sm">
                  <span className="text-gray-400">
                    {phase === 'build' ? 'トランザクション構築中...' : '送信中...'}
                  </span>
                  <span className="font-bold tabular-nums">{progress.percent.toFixed(1)}%</span>
                </div>
                <div className="w-full bg-gray-800 rounded-full h-3 overflow-hidden">
                  <div
                    className={`h-full rounded-full transition-all duration-300 ${
                      phase === 'build' ? 'bg-gradient-to-r from-blue-500 to-cyan-500' :
                      'bg-gradient-to-r from-red-500 to-orange-500'
                    }`}
                    style={{ width: `${Math.min(progress.percent, 100)}%` }}
                  />
                </div>
                <div className="text-sm text-gray-500">
                  {formatNumber(progress.current)} / {formatNumber(progress.total)}
                </div>
                {phase === 'broadcast' && stats.tps > 0 && (
                  <div className="flex items-baseline gap-2">
                    <span className="text-4xl font-bold text-orange-400 tabular-nums">
                      {formatNumber(Math.round(stats.tps))}
                    </span>
                    <span className="text-gray-500">TPS</span>
                  </div>
                )}
              </>
            )}

            {!launchComplete && phase !== 'build' && phase !== 'broadcast' && (
              <>
                <div className="text-xs text-gray-500 bg-gray-800/50 rounded-lg px-3 py-2">
                  推定所要時間: {getEstimatedTimes(mode, totalTransfers).launch}
                  <span className="text-gray-600 ml-2">(TX構築 + ブロードキャスト)</span>
                </div>
                <button
                  onClick={handleLaunch}
                  disabled={!connected || isRunning}
                  className="w-full px-8 py-3 bg-gradient-to-r from-orange-500 to-red-600 hover:from-orange-400 hover:to-red-500 rounded-lg font-bold text-lg transition-all disabled:opacity-50 disabled:cursor-not-allowed shadow-lg shadow-orange-500/20"
                >
                  {mode === 'localtest' ? '🚀 送金開始（10秒間）' : '🚀 送金開始'}
                </button>
              </>
            )}
          </section>
        )}

        {/* Step 3: Result */}
        {launchComplete && phase !== 'done' && (
          <section className="bg-gray-900 rounded-2xl border border-gray-800 p-6 space-y-4">
            <div className="flex items-center gap-3">
              <div className={`w-8 h-8 rounded-full text-sm font-bold flex items-center justify-center ${
                phase === 'confirm' ? 'bg-orange-500 text-white animate-pulse' : 'bg-emerald-500 text-white'
              }`}>
                3
              </div>
              <h3 className="text-xl font-bold">Result</h3>
            </div>

            {phase === 'confirm' && progress && (
              <>
                <div className="flex items-center justify-between text-sm">
                  <span className="text-gray-400">確認中...</span>
                  <span className="font-bold tabular-nums">{progress.percent.toFixed(1)}%</span>
                </div>
                <div className="w-full bg-gray-800 rounded-full h-3 overflow-hidden">
                  <div
                    className="h-full rounded-full transition-all duration-300 bg-gradient-to-r from-green-500 to-emerald-500"
                    style={{ width: `${Math.min(progress.percent, 100)}%` }}
                  />
                </div>
                <div className="text-sm text-gray-500">
                  {formatNumber(progress.current)} / {formatNumber(progress.total)} confirmed
                </div>
              </>
            )}

            {phase !== 'confirm' && (
              <>
                <div className="text-xs text-gray-500 bg-gray-800/50 rounded-lg px-3 py-2">
                  推定所要時間: {getEstimatedTimes(mode, totalTransfers).confirm}
                  <span className="text-gray-600 ml-2">(オンチェーン検証)</span>
                </div>
                <button
                  onClick={handleConfirm}
                  disabled={!connected || isRunning}
                  className="w-full px-8 py-3 bg-gradient-to-r from-green-500 to-emerald-600 hover:from-green-400 hover:to-emerald-500 rounded-lg font-bold text-lg transition-all disabled:opacity-50 disabled:cursor-not-allowed shadow-lg shadow-green-500/20"
                >
                  📊 結果を確認
                </button>
              </>
            )}
          </section>
        )}

        {/* Completion Display */}
        {phase === 'done' && (
          <section className="bg-gradient-to-br from-gray-900 via-gray-900 to-orange-950/30 rounded-2xl border border-orange-500/30 p-8 text-center space-y-6">
            <div>
              <p className="text-gray-400 text-lg mb-2">送金完了</p>
              <div className="text-6xl sm:text-8xl font-black text-transparent bg-clip-text bg-gradient-to-r from-orange-400 to-red-500">
                {formatNumber(stats.txConfirmed)}円
              </div>
              <p className="text-gray-500 mt-2">
                {formatNumber(stats.txConfirmed)} STAS tokens transferred
              </p>
            </div>

            {/* Stats Grid */}
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 max-w-3xl mx-auto">
              <StatBox label="送信数" value={formatNumber(stats.txBroadcast)} />
              <StatBox label="確認数" value={formatNumber(stats.txConfirmed)} />
              <StatBox label="平均TPS" value={formatNumber(Math.round(stats.tps))} />
              <StatBox label="合計時間" value={`${stats.totalDuration.toFixed(1)}s`} />
              <StatBox label="構築時間" value={`${stats.buildDuration.toFixed(1)}s`} />
              <StatBox label="送信時間" value={`${stats.broadcastDuration.toFixed(1)}s`} />
              <StatBox label="エラー" value={formatNumber(stats.txErrors)} />
              <StatBox label="成功率" value={stats.txBroadcast > 0 ? `${((stats.txBroadcast - stats.txErrors) / stats.txBroadcast * 100).toFixed(2)}%` : '—'} />
            </div>

            {/* Transaction Explorer Links */}
            {txIds.length > 0 && (
              <div className="text-left max-w-3xl mx-auto w-full space-y-3">
                <h3 className="text-sm font-semibold text-gray-400 uppercase tracking-wider">トランザクション詳細</h3>
                <p className="text-xs text-gray-500">
                  {formatNumber(stats.txBroadcast)} 件中 {txIds.length} 件を表示 — {mode === 'bsvmainnet' ? 'WoC / Bitails メインネット' : 'WoC / Bitails テストネット'}エクスプローラーで確認
                </p>
                <div className="max-h-64 overflow-y-auto space-y-1 rounded-lg bg-gray-800/50 p-3">
                  {txIds.map((txid, i) => (
                    <div key={txid} className="flex items-center gap-2 text-xs font-mono group hover:bg-gray-700/50 rounded px-2 py-1">
                      <span className="text-gray-600 w-8 text-right shrink-0">#{i + 1}</span>
                      <span className="text-gray-400 truncate flex-1">{txid}</span>
                      <a
                        href={`${config.explorerBaseUrl}/tx/${txid}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-blue-400 hover:text-blue-300 shrink-0 hover:underline"
                      >
                        WoC
                      </a>
                      <a
                        href={`${config.bitailsBaseUrl}/tx/${txid}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-emerald-400 hover:text-emerald-300 shrink-0 hover:underline"
                      >
                        Bitails
                      </a>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <button
              onClick={() => { setPhase('idle'); setProgress(null); setConfigured(false); setChargeComplete(false); setLaunchComplete(false); setTxIds([]); }}
              className="px-8 py-3 bg-gray-800 hover:bg-gray-700 rounded-lg font-medium transition-colors"
            >
              リセット
            </button>
          </section>
        )}

        {/* Idle State - hidden on mobile after mode selection */}
        {phase === 'idle' && !configured && (
          <section className="hidden md:block text-center py-20 space-y-6">
            <div className="text-8xl">🚀</div>
            <h2 className="text-3xl font-bold">STAS CANNON</h2>
            <p className="text-gray-400 max-w-md mx-auto">
              {mode === 'localtest'
                ? 'BSV STASトークンを高速で送金するシステム。秒間10万件の送金を10秒間実行し、合計100万件のトークン送金を実現します。'
                : mode === 'bsvtestnet'
                ? 'BSVテストネットで実際のSTASトークンを送金します。tBSVを使用するため、実コストなしでテスト可能です。'
                : 'BSVメインネットで実際のSTASトークンを送金します。実BSVが必要です。'}
            </p>
            <div className="flex flex-col items-center gap-2 text-sm text-gray-600">
              {config.needsWallet && <span>0. ウォレット設定 — BSVチャージ</span>}
              <span>1. パワーチャージ — UTXO分割準備</span>
              <span>2. 発射 — {mode === 'localtest' ? '100K TPS × 10秒' : 'STAS送金実行'}</span>
              <span>3. 確認 — 送金完了確認</span>
            </div>
            <Link
              to="/"
              className="inline-block px-6 py-2 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm font-medium transition-colors text-gray-400"
            >
              &larr; モード選択に戻る
            </Link>
          </section>
        )}
      </main>

      {/* Footer */}
      <footer className="border-t border-gray-800 mt-16">
        <div className="max-w-6xl mx-auto px-6 py-4 text-center text-xs text-gray-600">
          STAS Cannon — High-Throughput BSV STAS Token Transfer System — Go + FastAPI + React
        </div>
      </footer>
    </div>
  )
}

function StatBox({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-gray-800/50 rounded-xl p-4">
      <div className="text-xs text-gray-500 mb-1">{label}</div>
      <div className="text-lg font-bold tabular-nums">{value}</div>
    </div>
  )
}

export default App
