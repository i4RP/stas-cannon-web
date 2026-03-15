# 秒間100万件送金モード（High-Throughput Mode）開発計画

## 概要

現在のStas Cannonは最大100K TPS（Local Testモード）をサポートしている。
本計画では **1,000,000 TPS（秒間100万件）** の送金モードを追加する。

---

## 1. フロントエンド変更

### 1.1 ModeSelect.tsx - 新モード追加
- 4番目のモード「High-Throughput」を追加（既存3モードの上位版）
- アイコン: Zap or Flame（Lucide React）
- ラベル: "High-Throughput" / "超高速モード"
- 説明: "1,000,000 TPS - 秒間100万件送金デモ"

### 1.2 App.tsx - モード設定追加
- `MODE_CONFIG`に`highthroughput`モードを追加:
  ```
  highthroughput: {
    label: "High-Throughput",
    walletRequired: false,
    maxTransfers: 10_000_000,
    transferOptions: [1_000_000, 5_000_000, 10_000_000],
    description: "秒間100万件の超高速送金シミュレーション"
  }
  ```
- 新モード用のUI表示（TPS目標値の表示、リアルタイムゲージなど）
- 1M TPS達成時の演出効果（パーティクルエフェクトやカウンター）

### 1.3 パフォーマンスダッシュボード強化
- リアルタイムTPSグラフ（Rechartsで秒単位の推移表示）
- 目標TPS（1M）vs 実績TPSのゲージ表示
- ピークTPS、平均TPS、総送金件数のサマリーカード

---

## 2. バックエンド変更

### 2.1 main.py - High-Throughputシミュレーションロジック

#### 新関数: `run_high_throughput_power_charge()`
- 超大規模UTXO準備のシミュレーション
- 100並列グループの事前準備（現在は最大10）
- 進捗をWebSocket経由でリアルタイム送信

#### 新関数: `run_high_throughput_launch()`
- **並列バッチ処理**: 100並列ワーカー × 各10,000件/秒
- **マイクロバッチ**: 1,000件単位でバッチ処理し、1秒あたり1,000バッチ実行
- asyncioベースの並列実行でCPU効率を最大化
- 1秒ごとにTPS計測・報告
- 目標: 1,000,000 TPS × 設定秒数

#### 新関数: `run_high_throughput_confirm()`
- バッチ単位の確認処理シミュレーション
- 最終統計の集計と報告

### 2.2 WebSocket拡張
- `high_throughput_stats`メッセージタイプ追加:
  ```json
  {
    "type": "high_throughput_stats",
    "current_tps": 1000000,
    "peak_tps": 1050000,
    "total_transferred": 5000000,
    "elapsed_seconds": 5,
    "worker_stats": [...]
  }
  ```
- 高頻度更新（100ms間隔）でリアルタイム性を維持

### 2.3 パフォーマンス最適化
- WebSocketメッセージのバッファリング（更新頻度の制御）
- メモリ効率的なカウンター管理（個別TX IDの保持を省略）
- 大規模数値のフォーマット処理

---

## 3. 実装ステップ（優先順）

### Step 1: バックエンド - High-Throughputシミュレーションエンジン
- `main.py`にhigh-throughput用の3フェーズ関数を追加
- WebSocketハンドラーに新モードのルーティングを追加
- 1M TPS達成のためのasyncioベース並列シミュレーション実装

### Step 2: フロントエンド - モード選択とUI
- `ModeSelect.tsx`に新モードカードを追加
- `App.tsx`のモード設定とルーティングを更新
- 基本的な進捗表示を実装

### Step 3: フロントエンド - パフォーマンスダッシュボード
- リアルタイムTPSチャート（Recharts）
- 目標達成ゲージとサマリー統計
- 1M TPS達成時の視覚的演出

### Step 4: テストと調整
- 各種ブラウザでのパフォーマンステスト
- WebSocket負荷テスト（高頻度メッセージ）
- UIのレスポンシブ対応確認

---

## 4. 技術的考慮事項

### シミュレーション精度
- `asyncio.sleep`の精度限界（Pythonでは~1ms）を考慮
- バッチ単位で処理カウントを進め、実時間に合わせてTPS計算
- 実際のCPU処理時間を含めた現実的なTPS計測

### WebSocket負荷
- 1M TPS時に毎秒数百万のイベントは送信不可
- **サンプリング戦略**: 100ms間隔で集約統計のみ送信（秒10回）
- フロントエンドでの補間アニメーション

### メモリ管理
- 1000万件のTX IDを保持しない（カウンターのみ）
- ストリーミング統計で集計

---

## 5. ファイル変更一覧

| ファイル | 変更内容 |
|---------|---------|
| `stas-cannon-frontend/src/ModeSelect.tsx` | High-Throughputモードカード追加 |
| `stas-cannon-frontend/src/App.tsx` | モード設定、UI、WebSocket処理追加 |
| `stas-cannon-backend/app/main.py` | シミュレーションエンジン、WebSocket拡張 |
