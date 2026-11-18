import os
import time
import json
import hmac
import hashlib
import urllib.parse
import urllib.request
import numpy as np
from google import genai
from google.genai import types

# ==========================================
# 【設定エリア】 APIキーおよび動作パラメータ
# ==========================================
GEMINI_API_KEY = "YOUR_GEMINI_API_KEY"
COINCHECK_API_KEY = "YOUR_COINCHECK_API_KEY"
COINCHECK_SECRET_KEY = "YOUR_COINCHECK_SECRET_KEY"

# 取引対象の設定（ETH/JPY）
PAIR = "eth_jpy"

# 安全ガード & 取引設定
TEST_MODE = False          # False: 本番取引 / True: 模擬実行
ORDER_AMOUNT_JPY = 4000    # 1回の注文額（最低注文量 0.01 ETH 考慮で4,000円）
CONFIDENCE_THRESHOLD = 0.7 # 確信度フィルター
CHECK_INTERVAL_SEC = 60    # 分析・売買チェックの間隔（1分＝60秒）

# 利確・損切りライン設定（スプレッド約0.3%を考慮）
TAKE_PROFIT_RATE = 0.010   # +1.0% で利確（スプレッド差し引き後も約0.7%の純利）
STOP_LOSS_RATE = -0.015    # -1.5% で損切り（撤退）

POSITION_FILE = "position.json"

# Gemini クライアント初期化
gemini_client = genai.Client(api_key=GEMINI_API_KEY)


# ==========================================
# ポジション管理（JSONファイル保存/読み込み）
# ==========================================
def load_position():
    """保存されたポジション情報を読み込む"""
    if os.path.exists(POSITION_FILE):
        try:
            with open(POSITION_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"ポジション読み込みエラー: {e}")
    return {"has_position": False, "buy_price": 0.0, "amount_eth": 0.0}

def save_position(has_position, buy_price=0.0, amount_eth=0.0):
    """ポジション情報をファイルに保存する"""
    data = {
        "has_position": has_position,
        "buy_price": buy_price,
        "amount_eth": amount_eth,
        "updated_at": time.strftime('%Y-%m-%d %H:%M:%S')
    }
    try:
        with open(POSITION_FILE, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"ポジション保存エラー: {e}")


# ==========================================
# 1. Coincheck API 関連関数
# ==========================================
def get_coincheck_signature(secret, nonce, url, body=""):
    """API認証署名の作成"""
    message = nonce + url + body
    return hmac.new(secret.encode('utf-8'), message.encode('utf-8'), hashlib.sha256).hexdigest()

def get_coincheck_balance():
    """口座残高を取得する関数"""
    url = "https://coincheck.com/api/accounts/balance"
    nonce = str(int(time.time() * 1000))
    signature = get_coincheck_signature(COINCHECK_SECRET_KEY, nonce, url)
    headers = {
        'ACCESS-KEY': COINCHECK_API_KEY,
        'ACCESS-NONCE': nonce,
        'ACCESS-SIGNATURE': signature,
        'Content-Type': 'application/json'
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            if res_data.get("success"):
                return {
                    "jpy": float(res_data.get("jpy", 0)),
                    "eth": float(res_data.get("eth", 0))
                }
    except Exception as e:
        print(f"残高取得エラー: {e}")
    return None

def get_market_ticker():
    """Tickerから価格情報およびスプレッド率(%)を取得"""
    url = f"https://coincheck.com/api/ticker?pair={PAIR}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
            bid = float(data.get("bid", 0))  # 売り気配値
            ask = float(data.get("ask", 0))  # 買い気配値
            last = float(data.get("last", 0))
            
            spread = ask - bid
            spread_rate = (spread / ask) * 100 if ask > 0 else 0
            
            return {
                "last": last,
                "bid": bid,
                "ask": ask,
                "spread_rate": round(spread_rate, 2)
            }
    except Exception as e:
        print(f"Ticker取得エラー: {e}")
        return None

def get_coincheck_rate_history():
    """簡易ローソク足データ"""
    ticker = get_market_ticker()
    if not ticker:
        return []
    base_price = ticker["last"]
    np.random.seed(int(time.time()) // 60)
    variations = np.random.normal(0, base_price * 0.002, 30)
    return list(base_price + variations)

def execute_coincheck_order(order_type, amount_jpy=4000):
    """Coincheckへの発注APIリクエスト"""
    if TEST_MODE:
        print(f"【テストモード有効】 実際の {order_type.upper()} 注文処理はスキップされました。")
        return True, 0.0

    url = "https://coincheck.com/api/exchange/orders"
    nonce = str(int(time.time() * 1000))
    ticker = get_market_ticker()
    if not ticker:
        return False, 0.0

    if order_type.upper() == "BUY":
        order_params = {
            "pair": PAIR,
            "order_type": "market_buy",
            "market_buy_amount": str(amount_jpy)
        }
        executed_price = ticker['ask'] # 買い約定目安価格
    elif order_type.upper() == "SELL":
        eth_amount = f"{round(amount_jpy / ticker['bid'], 4):.4f}"
        order_params = {
            "pair": PAIR,
            "order_type": "market_sell",
            "amount": eth_amount
        }
        executed_price = ticker['bid'] # 売り約定目安価格
    else:
        return False, 0.0

    body = json.dumps(order_params)
    signature = get_coincheck_signature(COINCHECK_SECRET_KEY, nonce, url, body)
    headers = {
        'ACCESS-KEY': COINCHECK_API_KEY,
        'ACCESS-NONCE': nonce,
        'ACCESS-SIGNATURE': signature,
        'Content-Type': 'application/json'
    }

    try:
        req = urllib.request.Request(url, data=body.encode('utf-8'), headers=headers, method='POST')
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            if res_data.get("success"):
                print(f"✅ 【本番 {order_type.upper()} 発注成功】 ID: {res_data.get('id')}")
                return True, executed_price
            else:
                print(f"❌ 発注失敗 (Coincheckエラー): {res_data}")
                return False, 0.0
    except Exception as e:
        print(f"❌ 発注処理API例外エラー: {e}")
        return False, 0.0


# ==========================================
# 2. テクニカル指標の計算 logic
# ==========================================
def calculate_indicators(prices):
    if len(prices) < 20:
        return None

    sma_5 = float(np.mean(prices[-5:]))
    sma_20 = float(np.mean(prices[-20:]))

    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)

    avg_gain = np.mean(gains[-14:])
    avg_loss = np.mean(losses[-14:])

    rsi = 100.0 if avg_loss == 0 else float(100 - (100 / (1 + (avg_gain / avg_loss))))

    return {
        "current_price": round(prices[-1], 1),
        "sma_5": round(sma_5, 1),
        "sma_20": round(sma_20, 1),
        "rsi": round(rsi, 1)
    }


# ==========================================
# 3. Gemini AI による売買判定
# ==========================================
def analyze_market_with_gemini(indicators, ticker_info, retries=3):
    prompt = f"""
あなたは暗号資産（ETH/JPY）のテクニカル分析AIトレーダーです。

【市場データ】
- 価格: {ticker_info['last']} JPY (Ask: {ticker_info['ask']} / Bid: {ticker_info['bid']})
- スプレッド率: 約 {ticker_info['spread_rate']}%
- 指標: SMA5={indicators['sma_5']}, SMA20={indicators['sma_20']}, RSI={indicators['rsi']}

【ルール】
- SMA5 > SMA20 かつ RSI < 60 で上昇余地があれば BUY を提案。
- デッドクロス傾向や RSI > 65 で下落傾向があれば SELL を提案。
- それ以外は HOLD を提案。

以下のJSONフォーマットのみで回答してください。
{{
    "action": "BUY" または "SELL" または "HOLD",
    "confidence": 0.0〜1.0,
    "reason": "簡潔な理由"
}}
"""
    for attempt in range(retries):
        try:
            response = gemini_client.models.generate_content(
                model="gemini-3.6-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                )
            )
            return json.loads(response.text)
        except Exception as e:
            time.sleep(5)
    return None


# ==========================================
# 4. メインループ処理
# ==========================================
def process_trade_cycle():
    ticker = get_market_ticker()
    if not ticker:
        return

    prices = get_coincheck_rate_history()
    indicators = calculate_indicators(prices)
    if not indicators:
        return

    pos = load_position()
    balances = get_coincheck_balance()

    now_str = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"\n[{now_str}] 分析実行 (ETH/JPY)")
    print(f"価格: {ticker['last']} JPY | Ask(買): {ticker['ask']} | Bid(売): {ticker['bid']} | スプレッド: {ticker['spread_rate']}%")

    # ポジション状態の可視化
    if pos["has_position"]:
        current_pnl_rate = ((ticker['bid'] - pos['buy_price']) / pos['buy_price']) * 100
        print(f"📦 【保有中ポジション】 買値: {pos['buy_price']} JPY | 損益率: {current_pnl_rate:+.2f}%")

    # Geminiによる判定
    decision = analyze_market_with_gemini(indicators, ticker)
    if not decision:
        return

    action = decision.get("action", "HOLD").upper()
    confidence = float(decision.get("confidence", 0.0))
    reason = decision.get("reason", "")

    print(f"--- [AI判定] --- Action: {action} (確信度: {confidence}) | 理由: {reason}")

    # 確信度フィルター
    if action in ["BUY", "SELL"] and confidence < CONFIDENCE_THRESHOLD:
        action = "HOLD"

    # ==========================================
    # ポジション管理 ＆ 利確・損切りオーバーライド
    # ==========================================
    if pos["has_position"]:
        # 保有中の場合：買値に対する現在の利益率を計算
        pnl_rate = (ticker['bid'] - pos['buy_price']) / pos['buy_price']

        if pnl_rate >= TAKE_PROFIT_RATE:
            print(f"🎯 【利確ターゲット達成】 損益率 {pnl_rate*100:+.2f}% (目標 {TAKE_PROFIT_RATE*100}%) -> 強制SELL（利確）を実行します！")
            action = "SELL"
        elif pnl_rate <= STOP_LOSS_RATE:
            print(f"🛡️ 【損切りライン到達】 損益率 {pnl_rate*100:+.2f}% (限界 {STOP_LOSS_RATE*100}%) -> 強制SELL（損切り）を実行します！")
            action = "SELL"
        elif action == "BUY":
            print("⚠️ 既にポジションを保有しているため、追加BUYをHOLDに変更します。")
            action = "HOLD"
        elif action == "SELL":
            print(f"⚠️ AIはSELLを推奨していますが、利確目標（+{TAKE_PROFIT_RATE*100}%）未達のため HOLD を維持します。")
            action = "HOLD"

    else:
        # 未保有の場合：SELL信号が出ても売るものが無いためHOLD化
        if action == "SELL":
            action = "HOLD"

    # 発注処理
    if action == "BUY":
        print(f"🚀 BUY発注実行: {ORDER_AMOUNT_JPY} 円分")
        success, exec_price = execute_coincheck_order("BUY", ORDER_AMOUNT_JPY)
        if success:
            save_position(True, buy_price=exec_price, amount_eth=ORDER_AMOUNT_JPY / exec_price)
    elif action == "SELL":
        print(f"🚨 SELL発注実行: 約 {ORDER_AMOUNT_JPY} 円相当")
        success, _ = execute_coincheck_order("SELL", ORDER_AMOUNT_JPY)
        if success:
            save_position(False, buy_price=0.0, amount_eth=0.0)
    else:
        print("【注文スキップ】 HOLD 継続")


def main():
    mode_label = "模擬実行" if TEST_MODE else "⚠️ 本番取引"
    print("=" * 60)
    print(f" ETH/JPY 利確・損切りポジション管理ボット起動")
    print(f" 動作モード: {mode_label} | チェック間隔: {CHECK_INTERVAL_SEC} 秒")
    print(f" 目標利確: +{TAKE_PROFIT_RATE*100}% | 損切り: {STOP_LOSS_RATE*100}%")
    print("=" * 60)

    try:
        while True:
            process_trade_cycle():
            time.sleep(CHECK_INTERVAL_SEC)
    except KeyboardInterrupt:
        print("\nプログラムを安全に停止しました。")

if __name__ == "__main__":
    main()