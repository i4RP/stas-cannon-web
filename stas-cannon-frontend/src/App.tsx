import { useState, useEffect, useRef, useCallback } from 'react'
import './App.css'

const API_URL = import.meta.env.VITE_API_URL || ''

type Phase = 'idle' | 'power_charge' | 'build' | 'broadcast' | 'confirm' | 'done'

interface ProgressData {
  phase: Phase
  current: number
  total: number
  percent: number
  tps?: number
  errors?: number
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

function App() {
  const [phase, setPhase] = useState<Phase>('idle')
  const [totalTransfers, setTotalTransfers] = useState(1_000_000)
  const [progress, setProgress] = useState<ProgressData | null>(null)
  const [stats, setStats] = useState<Stats>({
    txBuilt: 0, txBroadcast: 0, txConfirmed: 0, txErrors: 0,
    tps: 0, buildDuration: 0, broadcastDuration: 0, totalDuration: 0,
    senderAddress: '', receiverAddress: '',
  })
  const [connected, setConnected] = useState(false)
  const [configured, setConfigured] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const connectWs = useCallback(() => {
    // Close any existing connection first
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
      ? API_URL.replace('http', 'ws') + '/ws/cannon'
      : `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws/cannon`
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
          if (msg.phase === 'launch') {
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
          break

        case 'stopped':
          setPhase('idle')
          break
      }
    }

    wsRef.current = ws
  }, [])

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

  const handleStart = () => {
    setProgress(null)
    setStats({
      txBuilt: 0, txBroadcast: 0, txConfirmed: 0, txErrors: 0,
      tps: 0, buildDuration: 0, broadcastDuration: 0, totalDuration: 0,
      senderAddress: stats.senderAddress, receiverAddress: stats.receiverAddress,
    })
    wsRef.current?.send(JSON.stringify({ action: 'start' }))
  }

  const handleStop = () => {
    wsRef.current?.send(JSON.stringify({ action: 'stop' }))
  }

  const formatNumber = (n: number) => n.toLocaleString()
  const isRunning = phase !== 'idle' && phase !== 'done'

  const getPhaseLabel = (p: Phase): string => {
    switch (p) {
      case 'idle': return '待機中'
      case 'power_charge': return 'パワーチャージ'
      case 'build': return 'トランザクション構築'
      case 'broadcast': return '発射中'
      case 'confirm': return '確認中'
      case 'done': return '完了'
      default: return ''
    }
  }

  const getPhaseColor = (p: Phase): string => {
    switch (p) {
      case 'power_charge': return 'text-yellow-400'
      case 'build': return 'text-blue-400'
      case 'broadcast': return 'text-red-400'
      case 'confirm': return 'text-green-400'
      case 'done': return 'text-emerald-400'
      default: return 'text-gray-400'
    }
  }

  return (
    <div className="min-h-screen bg-gray-950 text-white">
      {/* Header */}
      <header className="border-b border-gray-800 bg-gray-950/80 backdrop-blur-sm sticky top-0 z-10">
        <div className="max-w-6xl mx-auto px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-gradient-to-br from-orange-500 to-red-600 flex items-center justify-center font-bold text-lg">
              S
            </div>
            <div>
              <h1 className="text-xl font-bold tracking-tight">STAS CANNON</h1>
              <p className="text-xs text-gray-500">高速STAS送金システム</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <div className={`w-2 h-2 rounded-full ${connected ? 'bg-green-500' : 'bg-red-500'}`} />
            <span className="text-xs text-gray-500">{connected ? '接続済み' : '未接続'}</span>
          </div>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-6 py-8 space-y-8">
        {/* Configuration Panel */}
        <section className="bg-gray-900 rounded-2xl border border-gray-800 p-6">
          <h2 className="text-lg font-semibold mb-4">設定</h2>
          <div className="flex flex-col sm:flex-row gap-4 items-end">
            <div className="flex-1">
              <label className="block text-sm text-gray-400 mb-1">送金件数</label>
              <select
                value={totalTransfers}
                onChange={(e) => setTotalTransfers(Number(e.target.value))}
                disabled={isRunning}
                className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-white focus:outline-none focus:border-orange-500 disabled:opacity-50"
              >
                <option value={1000}>1,000</option>
                <option value={10000}>10,000</option>
                <option value={100000}>100,000</option>
                <option value={1000000}>1,000,000</option>
              </select>
            </div>
            <button
              onClick={handleConfigure}
              disabled={isRunning || !connected}
              className="px-6 py-2.5 bg-gray-700 hover:bg-gray-600 rounded-lg font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              初期化
            </button>
            {!isRunning && configured && (
              <button
                onClick={handleStart}
                disabled={!connected}
                className="px-8 py-2.5 bg-gradient-to-r from-orange-500 to-red-600 hover:from-orange-400 hover:to-red-500 rounded-lg font-bold transition-all disabled:opacity-50 disabled:cursor-not-allowed shadow-lg shadow-orange-500/20"
              >
                発射
              </button>
            )}
            {isRunning && (
              <button
                onClick={handleStop}
                className="px-8 py-2.5 bg-gray-700 hover:bg-gray-600 rounded-lg font-medium transition-colors"
              >
                停止
              </button>
            )}
          </div>

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

        {/* Phase Display */}
        {phase !== 'idle' && (
          <section className="space-y-6">
            {/* Current Phase Banner */}
            <div className="bg-gray-900 rounded-2xl border border-gray-800 p-6">
              <div className="flex items-center justify-between mb-4">
                <div>
                  <span className="text-sm text-gray-500">現在のフェーズ</span>
                  <h3 className={`text-2xl font-bold ${getPhaseColor(phase)}`}>
                    {getPhaseLabel(phase)}
                  </h3>
                </div>
                {progress && (
                  <div className="text-right">
                    <div className="text-3xl font-bold tabular-nums">
                      {progress.percent.toFixed(1)}%
                    </div>
                    <div className="text-sm text-gray-500">
                      {formatNumber(progress.current)} / {formatNumber(progress.total)}
                    </div>
                  </div>
                )}
              </div>

              {/* Progress Bar */}
              {progress && (
                <div className="w-full bg-gray-800 rounded-full h-3 overflow-hidden">
                  <div
                    className={`h-full rounded-full transition-all duration-300 ${
                      phase === 'power_charge' ? 'bg-gradient-to-r from-yellow-500 to-orange-500' :
                      phase === 'build' ? 'bg-gradient-to-r from-blue-500 to-cyan-500' :
                      phase === 'broadcast' ? 'bg-gradient-to-r from-red-500 to-orange-500' :
                      phase === 'confirm' ? 'bg-gradient-to-r from-green-500 to-emerald-500' :
                      'bg-gray-600'
                    }`}
                    style={{ width: `${Math.min(progress.percent, 100)}%` }}
                  />
                </div>
              )}

              {/* TPS Display */}
              {(phase === 'broadcast' || phase === 'done') && stats.tps > 0 && (
                <div className="mt-4 flex items-baseline gap-2">
                  <span className="text-4xl font-bold text-orange-400 tabular-nums">
                    {formatNumber(Math.round(stats.tps))}
                  </span>
                  <span className="text-gray-500">TPS</span>
                </div>
              )}
            </div>

            {/* Phase Steps */}
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
              <PhaseCard
                number={1}
                title="パワーチャージ"
                subtitle="Power Charge"
                active={phase === 'power_charge'}
                complete={phase !== 'power_charge'}
                value={progress?.phase === 'power_charge' ? formatNumber(progress.current) : (phase !== 'power_charge' ? formatNumber(totalTransfers) : '—')}
                unit="UTXOs"
              />
              <PhaseCard
                number={2}
                title="発射"
                subtitle="Launch"
                active={phase === 'build' || phase === 'broadcast'}
                complete={phase === 'confirm' || phase === 'done'}
                value={stats.txBroadcast > 0 ? formatNumber(stats.txBroadcast) : (stats.txBuilt > 0 ? formatNumber(stats.txBuilt) : '—')}
                unit={phase === 'build' ? 'built' : 'sent'}
              />
              <PhaseCard
                number={3}
                title="確認"
                subtitle="Confirm"
                active={phase === 'confirm'}
                complete={phase === 'done'}
                value={stats.txConfirmed > 0 ? formatNumber(stats.txConfirmed) : '—'}
                unit="confirmed"
              />
            </div>
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

            <button
              onClick={() => { setPhase('idle'); setProgress(null); setConfigured(false); }}
              className="px-8 py-3 bg-gray-800 hover:bg-gray-700 rounded-lg font-medium transition-colors"
            >
              リセット
            </button>
          </section>
        )}

        {/* Idle State */}
        {phase === 'idle' && !configured && (
          <section className="text-center py-20 space-y-6">
            <div className="text-8xl">🚀</div>
            <h2 className="text-3xl font-bold">STAS CANNON</h2>
            <p className="text-gray-400 max-w-md mx-auto">
              BSV STASトークンを高速で送金するシステム。
              秒間10万件の送金を10秒間実行し、合計100万件のトークン送金を実現します。
            </p>
            <div className="flex flex-col items-center gap-2 text-sm text-gray-600">
              <span>1. パワーチャージ — UTXO分割準備</span>
              <span>2. 発射 — 100K TPS × 10秒</span>
              <span>3. 確認 — 送金完了確認</span>
            </div>
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

function PhaseCard({ number, title, subtitle, active, complete, value, unit }: {
  number: number; title: string; subtitle: string;
  active: boolean; complete: boolean; value: string; unit: string;
}) {
  return (
    <div className={`rounded-xl border p-5 transition-all ${
      active ? 'bg-gray-800/80 border-orange-500/50 shadow-lg shadow-orange-500/10' :
      complete ? 'bg-gray-800/40 border-green-500/30' :
      'bg-gray-900 border-gray-800'
    }`}>
      <div className="flex items-center gap-2 mb-2">
        <div className={`w-6 h-6 rounded-full text-xs font-bold flex items-center justify-center ${
          active ? 'bg-orange-500 text-white' :
          complete ? 'bg-green-500 text-white' :
          'bg-gray-700 text-gray-400'
        }`}>
          {complete ? '✓' : number}
        </div>
        <span className="text-sm text-gray-400">{subtitle}</span>
      </div>
      <h4 className="font-bold text-lg mb-1">{title}</h4>
      <div className="flex items-baseline gap-1">
        <span className={`text-2xl font-bold tabular-nums ${active ? 'text-orange-400' : complete ? 'text-green-400' : 'text-gray-600'}`}>
          {value}
        </span>
        <span className="text-xs text-gray-500">{unit}</span>
      </div>
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
