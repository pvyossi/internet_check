import subprocess
import datetime
import time
import os
import csv
import threading
import re
import requests
from dotenv import load_dotenv

load_dotenv()

# === 設定 ===
PING_ADDRESS = os.getenv('PING_ADDRESS', '8.8.8.8')
DOMAIN_NAME = os.getenv('DOMAIN_NAME', 'dns.google')
PING_INTERVAL = int(os.getenv('PING_INTERVAL', '1'))
IGNORE_TIMEOUT_SEC = int(os.getenv('IGNORE_TIMEOUT_SEC', '5'))

# ログ関係
LOGS_DIR = os.getenv('LOGS_DIR', 'logs')
DIAGNOSTICS_SUBDIR = os.getenv('DIAGNOSTICS_SUBDIR', 'diagnostics')
os.makedirs(LOGS_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOGS_DIR, os.getenv('LOG_FILE', 'network_log.csv'))
SLACK_WEBHOOK_URL = os.getenv('SLACK_WEBHOOK_URL', '')
CLOUD_WATCH_LOG_URL = os.getenv('CLOUD_WATCH_LOG_URL', '')

# OS判定（Windowsかどうか）
IS_WINDOWS = os.name == 'nt'

# === ping関数 ===
def ping():
    try:
        if IS_WINDOWS:
            output = subprocess.check_output(
                ['ping', '-n', '1', '-w', '1000', PING_ADDRESS],
                stderr=subprocess.STDOUT,
                encoding='shift_jis'
            )
        else:
            output = subprocess.check_output(
                ['ping', '-c', '1', '-W', '1', PING_ADDRESS],
                stderr=subprocess.STDOUT,
                encoding='utf-8'
            ).strip()
        return ("ttl=" in output.lower()) or ("TTL=" in output), output.strip()
    except subprocess.CalledProcessError as e:
        return False, e.output.strip()

# === 診断実行関数（同期） ===
def network_diagnostics():
    if IS_WINDOWS:
        commands = {
            'tracert': ['tracert', PING_ADDRESS],
            'ipconfig': ['ipconfig', '/all'],
            'nslookup': ['nslookup', DOMAIN_NAME]
        }
    else:
        tracert_cmd = ['traceroute', PING_ADDRESS]
        try:
            subprocess.check_output(['which', 'traceroute'], stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError:
            try:
                subprocess.check_output(['which', 'tracepath'], stderr=subprocess.STDOUT)
                tracert_cmd = ['tracepath', PING_ADDRESS]
            except subprocess.CalledProcessError:
                tracert_cmd = ['echo', 'traceroute/tracepathコマンドが見つかりません']
        
        nslookup_cmd = ['nslookup', DOMAIN_NAME]
        try:
            subprocess.check_output(['which', 'nslookup'], stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError:
            try:
                subprocess.check_output(['which', 'host'], stderr=subprocess.STDOUT)
                nslookup_cmd = ['host', DOMAIN_NAME]
            except subprocess.CalledProcessError:
                nslookup_cmd = ['echo', 'nslookup/hostコマンドが見つかりません']
        
        commands = {
            'tracert': tracert_cmd,
            'ipconfig': ['ip', 'a'],
            'nslookup': nslookup_cmd
        }
    results = {}
    for key, cmd in commands.items():
        try:
            output = subprocess.check_output(
                cmd, stderr=subprocess.STDOUT,
                encoding='shift_jis' if IS_WINDOWS else 'utf-8', timeout=60
            ).strip()
        except subprocess.CalledProcessError as e:
            output = e.output.strip() if hasattr(e, 'output') and e.output is not None else f"{key}コマンドの実行エラー"
        except subprocess.TimeoutExpired:
            output = f"{key}コマンドがタイムアウトしました。"
        except UnicodeDecodeError:
            output = f"{key}コマンドの出力エンコーディングエラー"
        results[key] = output
    return results['tracert'], results['ipconfig'], results['nslookup']

# === 診断ログ保存（非同期） ===
def network_diagnostics_async(timestamp, reason, ping_output,
                              tracert_result, ipconfig_result, nslookup_result):
    def diagnostics():
        dt = datetime.datetime.strptime(timestamp, '%Y/%m/%d %H:%M:%S')
        folder_name = dt.strftime('%Y%m%d_%H%M%S')
        folder_path = os.path.join(LOGS_DIR, DIAGNOSTICS_SUBDIR, folder_name)
        os.makedirs(folder_path, exist_ok=True)

        with open(os.path.join(folder_path, "ping.txt"), 'w', encoding='utf-8') as f:
            f.write(ping_output)
        with open(os.path.join(folder_path, "tracert.txt"), 'w', encoding='utf-8') as f:
            f.write(tracert_result)
        with open(os.path.join(folder_path, "ipconfig.txt"), 'w', encoding='utf-8') as f:
            f.write(ipconfig_result)
        with open(os.path.join(folder_path, "nslookup.txt"), 'w', encoding='utf-8') as f:
            f.write(nslookup_result)
    threading.Thread(target=diagnostics, daemon=True).start()

def send_slack_notification(message, is_failure=True):
    if not IS_WINDOWS and SLACK_WEBHOOK_URL:
        try:
            payload = {"text": message}
            emoji = "❌" if is_failure else "✅"
            payload["text"] = f"{emoji} {payload['text']}"
            response = requests.post(SLACK_WEBHOOK_URL, json=payload)
            response.raise_for_status()
        except Exception as e:
            print(f"Slack通知エラー: {str(e)}")

def send_cloudwatch_log(message):
    try:
        requests.post(CLOUD_WATCH_LOG_URL, json={"message": message})
    except Exception as e:
        print(f"CloudWatchログ送信エラー: {str(e)}")

# === 各診断要約 ===
def summarize_ping(ping_output):
    if "ttl=" in ping_output.lower():
        return "応答あり"
    elif "timeout" in ping_output.lower():
        return "応答なし（タイムアウト）"
    elif "unreachable" in ping_output.lower():
        return "応答なし（到達不能）"
    else:
        return "不明な応答"

def summarize_tracert(tracert_output):
    if "resolve" in tracert_output.lower():
        return "DNS名前解決に失敗（traceroute不可）"

    lines = tracert_output.strip().split('\n')
    target_ip = PING_ADDRESS
    last_success = None

    for i, line in enumerate(lines):
        if '*' in line or 'timed out' in line.lower():
            if last_success:
                hop_num, host, ip = last_success
                return f"{hop_num}ホップ目で停止（名称: {host}, IP: {ip}）"
            return "最初のホップから応答なし"

        match = re.search(r'^\s*(\d+)\s+([^\s]+)\s+\((\d+\.\d+\.\d+\.\d+)\)', line)
        if match:
            hop_num = int(match.group(1))
            host = match.group(2).strip()
            ip = match.group(3).strip()
            last_success = (hop_num, host, ip)

    if target_ip in lines[-1]:
        return "最終ホップまで到達可能"
    elif last_success:
        hop_num, host, ip = last_success
        return f"{hop_num}ホップ目で停止（名称: {host}, IP: {ip}）"
    else:
        return "最終ホップに未到達"

def summarize_nslookup(nslookup_output):
    if "can't find" in nslookup_output.lower() or "timed out" in nslookup_output.lower():
        return "DNS解決失敗"
    ip_match = re.search(r'Address(?:es)?:\s*(\d+\.\d+\.\d+\.\d+)', nslookup_output)
    if ip_match:
        ip = ip_match.group(1)
        if ip.startswith(('192.', '10.', '127.', '169.254.')):
            return f"DNS解決失敗（ローカルIP応答: {ip})"
        return f"DNS解決成功 ({DOMAIN_NAME} -> {ip})"
    return "DNS解決失敗（応答なし）"

def summarize_ipconfig(ipconfig_output):
    match = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', ipconfig_output)
    if match and not match.group(1).startswith("127."):
        return f"IPv4アドレス取得済み ({match.group(1)})"
    return "IPv4アドレス未取得"

# === メイン処理 ===
def main():
    disconnected = False
    failure_start = None
    failure_reason = ""
    failure_logged = False

    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'w', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(['日時', '状態', '理由', '継続秒数'])

    while True:
        success, output = ping()
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        now_str = now.strftime('%Y/%m/%d %H:%M:%S')

        if not success:
            if not disconnected:
                disconnected = True
                failure_start = now
                failure_reason = "タイムアウト" if "timeout" in output.lower() else "接続エラー"
                failure_logged = False

            duration = (now - failure_start).total_seconds() if failure_start else 0
            if not failure_logged and duration > IGNORE_TIMEOUT_SEC:
                failure_time_str = failure_start.strftime('%Y/%m/%d %H:%M:%S') if failure_start else now.strftime('%Y/%m/%d %H:%M:%S')
                print(f"[接続失敗] {failure_time_str}: {failure_reason}")

                with open(LOG_FILE, 'a', newline='', encoding='utf-8') as f:
                    csv.writer(f).writerow([failure_time_str, "接続失敗", failure_reason, "-"])

                tracert_result, ipconfig_result, nslookup_result = network_diagnostics()

                print("簡易診断:")
                print(f"・ping    : {summarize_ping(output)}")
                print(f"・traceroute : {summarize_tracert(tracert_result)}")
                print(f"・nslookup: {summarize_nslookup(nslookup_result)}")
                print(f"・ipconfig: {summarize_ipconfig(ipconfig_result)}")
                
                diagnosis_summary = f"""
・ping    : {summarize_ping(output)}
・traceroute : {summarize_tracert(tracert_result)}
・nslookup: {summarize_nslookup(nslookup_result)}
・ipconfig: {summarize_ipconfig(ipconfig_result)}
"""

                network_diagnostics_async(
                    failure_time_str,
                    failure_reason,
                    output,
                    tracert_result,
                    ipconfig_result,
                    nslookup_result
                )

                failure_logged = True

        else:
            if disconnected:
                disconnected = False
                duration = int((now - failure_start).total_seconds()) if failure_start else 0
                if failure_logged:
                    now_str = now.strftime('%Y/%m/%d %H:%M:%S')
                    with open(LOG_FILE, 'a', newline='', encoding='utf-8') as f:
                        csv.writer(f).writerow([now_str, "接続復旧", failure_reason, duration])
                    print(f"[接続復旧] {now_str}")
                    print(f"接続失敗期間: {failure_start.strftime('%Y/%m/%d %H:%M:%S') if failure_start else '不明'} ～ {now_str} (継続秒数: {duration}秒)")
                    
                    recovery_message = f"[接続復旧] {now_str}\n接続失敗期間: {failure_start.strftime('%Y/%m/%d %H:%M:%S') if failure_start else '不明'} ～ {now_str} (継続秒数: {duration}秒)"
                    send_slack_notification(recovery_message, is_failure=False)
                    send_cloudwatch_log(recovery_message)

        time.sleep(PING_INTERVAL)

if __name__ == '__main__':
    main()
