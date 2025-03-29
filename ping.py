import subprocess
import datetime
import time
import os
import csv
import threading
import uuid
import re
from dotenv import load_dotenv

# .env読み込み
load_dotenv()

# === 設定 (.envから読み込み) ===
PING_ADDRESS = os.getenv('PING_ADDRESS', '8.8.8.8')
DOMAIN_NAME = os.getenv('DOMAIN_NAME', 'dns.google')
PING_INTERVAL = int(os.getenv('PING_INTERVAL', '1'))
IGNORE_TIMEOUT_SEC = int(os.getenv('IGNORE_TIMEOUT_SEC', '5'))
LOG_FILE = os.getenv('LOG_FILE', 'network_log.csv')
DIAGNOSTICS_LOG_DIR = os.getenv('DIAGNOSTICS_LOG_DIR', 'diagnostics_logs')
os.makedirs(DIAGNOSTICS_LOG_DIR, exist_ok=True)

# === ping関数 ===
def ping():
    try:
        output = subprocess.check_output(
            ['ping', '-n', '1', '-w', '1000', PING_ADDRESS],
            stderr=subprocess.STDOUT,
            encoding='shift_jis'
        )
        return ("TTL=" in output), output.strip()
    except subprocess.CalledProcessError as e:
        return False, e.output.strip()

# === ネットワーク診断 ===
def network_diagnostics():
    commands = {
        'tracert': ['tracert', DOMAIN_NAME],
        'ipconfig': ['ipconfig', '/all'],
        'nslookup': ['nslookup', DOMAIN_NAME]
    }
    results = {}
    for key, cmd in commands.items():
        try:
            output = subprocess.check_output(
                cmd, stderr=subprocess.STDOUT, encoding='shift_jis', timeout=60
            ).strip()
        except subprocess.CalledProcessError as e:
            output = e.output.strip()
        except subprocess.TimeoutExpired:
            output = f"{key}コマンドがタイムアウトしました。"
        results[key] = output.replace('\r\n', '; ')
    return results['tracert'], results['ipconfig'], results['nslookup']

# === 非同期診断ログ ===
def network_diagnostics_async(timestamp, reason, ping_output):
    def diagnostics():
        tracert_result, ipconfig_result, nslookup_result = network_diagnostics()
        filename = f"{timestamp.replace('/', '').replace(' ', '_').replace(':', '')}_{uuid.uuid4().hex}.csv"
        filepath = os.path.join(DIAGNOSTICS_LOG_DIR, filename)

        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                timestamp, "接続失敗", reason, ping_output,
                tracert_result, ipconfig_result, nslookup_result
            ])
    threading.Thread(target=diagnostics, daemon=True).start()

# === 診断情報要約関数 ===
def summarize_tracert(tracert_output):
    lines = tracert_output.split('; ')
    for line in lines:
        if '*' in line or '要求がタイムアウトしました' in line:
            hop = line.strip().split(' ')[0]
            return f"{hop}以降に到達不能"
    return "最終ホップまで到達可能"

def summarize_nslookup(nslookup_output):
    if 'Addresses:' in nslookup_output or 'Address:' in nslookup_output:
        ip_match = re.search(r'Address(?:es)?:\s*(\d+\.\d+\.\d+\.\d+)', nslookup_output)
        if ip_match:
            ip = ip_match.group(1)
            return f"DNS解決成功 ({DOMAIN_NAME} -> {ip})"
    return "DNS解決失敗"

def summarize_ipconfig(ipconfig_output):
    ipv4_match = re.search(r'IPv4 アドレス.*?:\s*(\d+\.\d+\.\d+\.\d+)', ipconfig_output)
    if ipv4_match:
        ip = ipv4_match.group(1)
        return f"IPv4アドレス取得済み ({ip})"
    return "IPv4アドレス未取得"

# === メイン処理 ===
def main():
    disconnected = False
    start_time = None
    reason = ""
    diagnostics_started = False

    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['日時', '状態', '理由', '詳細', '継続秒数'])

    while True:
        success, output = ping()
        current_time = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        formatted_time = current_time.strftime('%Y/%m/%d %H:%M:%S')

        if not success:
            if not disconnected:
                disconnected = True
                start_time = current_time
                reason = "タイムアウト" if "要求がタイムアウトしました" in output else "接続エラー"
                diagnostics_started = False
        else:
            if disconnected:
                disconnected = False
                end_time = current_time
                duration_seconds = int((end_time - start_time).total_seconds())

                if duration_seconds > IGNORE_TIMEOUT_SEC:
                    failure_time_str = start_time.strftime('%Y/%m/%d %H:%M:%S')

                    with open(LOG_FILE, 'a', newline='', encoding='utf-8') as f:
                        writer = csv.writer(f)
                        writer.writerow([failure_time_str, "接続失敗", reason, "-", "-"])
                        writer.writerow([formatted_time, "接続復旧", reason, output, duration_seconds])

                    print(f"{failure_time_str} 接続失敗: {reason}")
                    print(f"{formatted_time} 接続復旧")
                    print(f"{failure_time_str} ～ {formatted_time} 接続失敗 | 継続秒数: {duration_seconds}秒")

                    if not diagnostics_started:
                        network_diagnostics_async(failure_time_str, reason, output)
                        diagnostics_started = True

                    diagnostics_files = sorted(os.listdir(DIAGNOSTICS_LOG_DIR))
                    if diagnostics_files:
                        latest_file = os.path.join(DIAGNOSTICS_LOG_DIR, diagnostics_files[-1])
                        with open(latest_file, encoding='utf-8') as diag_f:
                            reader = csv.reader(diag_f)
                            diag_row = next(reader)

                            print("簡易診断:")
                            print(f"・tracert : {summarize_tracert(diag_row[4])}")
                            print(f"・nslookup: {summarize_nslookup(diag_row[6])}")
                            print(f"・ipconfig: {summarize_ipconfig(diag_row[5])}")

        time.sleep(PING_INTERVAL)

if __name__ == '__main__':
    main()
