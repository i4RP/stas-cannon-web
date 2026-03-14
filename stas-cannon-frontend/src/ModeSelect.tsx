import { Link } from 'react-router-dom'
import './App.css'

const modes = [
  {
    path: '/localtest',
    title: 'Local Test',
    subtitle: 'シミュレーションモード',
    description: 'ローカル環境でのシミュレーション。実際のブロックチェーントランザクションは発生しません。開発・デモ用。',
    color: 'from-gray-500 to-gray-600',
    borderColor: 'border-gray-600/30',
    bgHover: 'hover:border-gray-500/50',
    badge: 'SIMULATION',
    badgeColor: 'bg-gray-600',
    icon: '🖥️',
    features: ['シミュレーション送金', '即時実行', 'BSV不要', '最大1,000,000件'],
  },
  {
    path: '/bsvtestnet',
    title: 'BSV Testnet',
    subtitle: 'テストネットモード',
    description: 'BSVテストネット上で実際のSTASトークン送金を実行。tBSVを使用するため、実コストなしでテスト可能。',
    color: 'from-yellow-500 to-amber-600',
    borderColor: 'border-yellow-600/30',
    bgHover: 'hover:border-yellow-500/50',
    badge: 'TESTNET',
    badgeColor: 'bg-yellow-600',
    icon: '🧪',
    features: ['実トランザクション', 'tBSV使用', 'テストネットエクスプローラー対応', '最大10,000件'],
  },
  {
    path: '/bsvmainnet',
    title: 'BSV Mainnet',
    subtitle: 'メインネットモード',
    description: 'BSVメインネット上で実際のSTASトークン送金を実行。実BSVが必要です。本番環境での送金。',
    color: 'from-red-500 to-rose-600',
    borderColor: 'border-red-600/30',
    bgHover: 'hover:border-red-500/50',
    badge: 'MAINNET',
    badgeColor: 'bg-red-600',
    icon: '🔴',
    features: ['実トランザクション', '実BSV使用', 'メインネットエクスプローラー対応', '最大1,000件'],
  },
]

function ModeSelect() {
  return (
    <div className="min-h-screen bg-gray-950 text-white">
      {/* Header */}
      <header className="border-b border-gray-800 bg-gray-950/80 backdrop-blur-sm">
        <div className="max-w-6xl mx-auto px-6 py-4 flex items-center gap-3">
          <div className="w-10 h-10 rounded-lg bg-gradient-to-br from-orange-500 to-red-600 flex items-center justify-center font-bold text-lg">
            S
          </div>
          <div>
            <h1 className="text-xl font-bold tracking-tight">STAS CANNON</h1>
            <p className="text-xs text-gray-500">高速STAS送金システム</p>
          </div>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-6 py-12 space-y-12">
        {/* Hero */}
        <div className="text-center space-y-4">
          <div className="text-6xl">🚀</div>
          <h2 className="text-3xl font-bold">モードを選択</h2>
          <p className="text-gray-400 max-w-lg mx-auto">
            BSV STASトークンを高速送金。用途に応じてモードを選択してください。
          </p>
        </div>

        {/* Mode Cards */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
          {modes.map((mode) => (
            <Link
              key={mode.path}
              to={mode.path}
              className={`group block bg-gray-900 rounded-2xl border ${mode.borderColor} ${mode.bgHover} p-6 space-y-4 transition-all hover:scale-[1.02] hover:shadow-lg`}
            >
              <div className="flex items-center justify-between">
                <span className="text-3xl">{mode.icon}</span>
                <span className={`text-[10px] font-bold uppercase tracking-widest px-2 py-0.5 rounded ${mode.badgeColor} text-white`}>
                  {mode.badge}
                </span>
              </div>

              <div>
                <h3 className={`text-xl font-bold bg-gradient-to-r ${mode.color} text-transparent bg-clip-text`}>
                  {mode.title}
                </h3>
                <p className="text-sm text-gray-400 mt-0.5">{mode.subtitle}</p>
              </div>

              <p className="text-xs text-gray-500 leading-relaxed">
                {mode.description}
              </p>

              <ul className="space-y-1.5">
                {mode.features.map((f) => (
                  <li key={f} className="flex items-center gap-2 text-xs text-gray-400">
                    <span className="w-1 h-1 rounded-full bg-gray-600 shrink-0" />
                    {f}
                  </li>
                ))}
              </ul>

              <div className={`text-sm font-semibold bg-gradient-to-r ${mode.color} text-transparent bg-clip-text group-hover:underline`}>
                開始 →
              </div>
            </Link>
          ))}
        </div>

        {/* Info */}
        <div className="text-center text-xs text-gray-600 space-y-1">
          <p>テストネット・メインネットモードではウォレット設定とBSV残高が必要です</p>
          <p>ローカルテストモードは設定不要ですぐに利用できます</p>
        </div>
      </main>

      <footer className="border-t border-gray-800 mt-16">
        <div className="max-w-6xl mx-auto px-6 py-4 text-center text-xs text-gray-600">
          STAS Cannon — High-Throughput BSV STAS Token Transfer System
        </div>
      </footer>
    </div>
  )
}

export default ModeSelect
