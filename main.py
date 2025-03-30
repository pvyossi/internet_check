import subprocess
import datetime
import time
import os
import csv
import threading
import re
from dotenv import load_dotenv

load_dotenv()

# === 設定 ===
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

# === 診断実行関数（同期） ===
def network_diagnostics():
    commands = {
        'tracert': ['tracert', PING_ADDRESS],  # ← IPで実行！
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
        results[key] = output.replace('\r\n', '\n')
    return results['tracert'], results['ipconfig'], results['nslookup']

# === 診断ログ保存（タイムスタンプのみのディレクトリ名） ===
def network_diagnostics_async(timestamp, reason, ping_output,
                              tracert_result, ipconfig_result, nslookup_result):
    def diagnostics():
        dt = datetime.datetime.strptime(timestamp, '%Y/%m/%d %H:%M:%S')
        folder_name = dt.strftime('%Y%m%d_%H%M%S')
        folder_path = os.path.join(DIAGNOSTICS_LOG_DIR, folder_name)
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

# === 各診断要約 ===
def summarize_ping(ping_output):
    if "TTL=" in ping_output:
        return "応答あり"
    elif "要求がタイムアウトしました" in ping_output:
        return "応答なし（タイムアウト）"
    elif "宛先ホストに到達できません" in ping_output:
        return "応答なし（到達不能）"
    else:
        return "不明な応答"

def summarize_tracert(tracert_output):
    if "を解決できません" in tracert_output or "could not resolve" in tracert_output.lower():
        return "DNS名前解決に失敗（tracert不可）"

    lines = tracert_output.strip().split('\n')
    target_ip = PING_ADDRESS
    last_success = None

    for i, line in enumerate(lines):
        if '*' in line or '要求がタイムアウトしました' in line or 'timed out' in line.lower():
            if last_success:
                hop_num, host, ip = last_success
                return f"{hop_num}ホップ目で停止（名称: {host}, IP: {ip}）"
            return "最初のホップから応答なし"

        match = re.search(r'^\s*(\d+)\s+(.*?)\s+\[(\d+\.\d+\.\d+\.\d+)\]', line)
        if match:
            hop_num = int(match.group(1))
            host = match.group(2).strip()
            ip = match.group(3).strip()
            last_success = (hop_num, host, ip)
        else:
            ip_only = re.search(r'^\s*(\d+)\s+(\d+\.\d+\.\d+\.\d+)', line)
            if ip_only:
                hop_num = int(ip_only.group(1))
                host = "(不明)"
                ip = ip_only.group(2)
                last_success = (hop_num, host, ip)

    last_line = lines[-1] if lines else ''
    if target_ip in last_line:
        return "最終ホップまで到達可能"
    elif last_success:
        hop_num, host, ip = last_success
        return f"{hop_num}ホップ目で停止（名称: {host}, IP: {ip}）"
    else:
        return "最終ホップに未到達"

def summarize_nslookup(nslookup_output):
    if "ターゲット システム名" in nslookup_output and "を解決できません" in nslookup_output:
        return "DNS解決失敗（名前解決不可）"
    if "DNS request timed out" in nslookup_output or "名前を解決できません" in nslookup_output:
        return "DNS解決失敗（タイムアウト）"

    ip_match = re.search(r'Address(?:es)?:\s*(\d+\.\d+\.\d+\.\d+)', nslookup_output)
    if ip_match:
        ip = ip_match.group(1)
        if ip.startswith(('192.', '10.', '127.', '169.254.')):
            return f"DNS解決失敗（ローカルIP応答: {ip}）"
        return f"DNS解決成功 ({DOMAIN_NAME} -> {ip})"

    return "DNS解決失敗（応答なし）"

def summarize_ipconfig(ipconfig_output):
    match = re.search(r'IPv4 アドレス.*?:\s*(\d+\.\d+\.\d+\.\d+)', ipconfig_output)
    return f"IPv4アドレス取得済み ({match.group(1)})" if match else "IPv4アドレス未取得"

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
                failure_reason = "タイムアウト" if "要求がタイムアウトしました" in output else "接続エラー"
                failure_logged = False

            duration = (now - failure_start).total_seconds()
            if not failure_logged and duration > IGNORE_TIMEOUT_SEC:
                failure_time_str = failure_start.strftime('%Y/%m/%d %H:%M:%S')
                print(f"{failure_time_str} 接続失敗: {failure_reason}")

                with open(LOG_FILE, 'a', newline='', encoding='utf-8') as f:
                    csv.writer(f).writerow([failure_time_str, "接続失敗", failure_reason, "-"])

                tracert_result, ipconfig_result, nslookup_result = network_diagnostics()

                print("簡易診断:")
                print(f"・ping    : {summarize_ping(output)}")
                print(f"・tracert : {summarize_tracert(tracert_result)}")
                print(f"・nslookup: {summarize_nslookup(nslookup_result)}")
                print(f"・ipconfig: {summarize_ipconfig(ipconfig_result)}")

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
                duration = int((now - failure_start).total_seconds())
                if failure_logged:
                    now_str = now.strftime('%Y/%m/%d %H:%M:%S')
                    with open(LOG_FILE, 'a', newline='', encoding='utf-8') as f:
                        csv.writer(f).writerow([now_str, "接続復旧", failure_reason, duration])
                    print(f"{now_str} 接続復旧")
                    print(f"{failure_start.strftime('%Y/%m/%d %H:%M:%S')} ～ {now_str} 接続失敗 | 継続秒数: {duration}秒")

        time.sleep(PING_INTERVAL)

if __name__ == '__main__':
    main()
