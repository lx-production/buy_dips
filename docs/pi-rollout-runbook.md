# Runbook rollout bot trên Raspberry Pi production

Tài liệu này hướng dẫn triển khai repo lên Pi Ubuntu bằng tài khoản SSH `prana` có quyền `sudo`, nhưng tiến trình bot chạy bằng system user riêng `botuser`. Giai đoạn đầu dùng **dev canary wallet trên máy Pi production** trong nhiều ngày:

1. `observe`: chỉ lấy dữ liệu và ghi quyết định, không mở ví hoặc gọi Polygon RPC.
2. `dry_run`: khi có tín hiệu BUY, bot kiểm tra ví/RPC, lấy quote và mô phỏng giao dịch; không ký, không approve và không broadcast.
3. Chỉ sau khi review đầy đủ mới chuẩn bị một **prod wallet riêng** và cân nhắc `live`.

> **Quan trọng:** “Pi production chạy dev canary” không có nghĩa là giao dịch thật. Với `environment: dev`, code bắt buộc `live_enabled: false` và từ chối `--mode live`. Không dùng dev wallet làm prod wallet về sau.

## 1. Kiến trúc và đường dẫn đề xuất

| Thành phần | Giá trị đề xuất |
|---|---|
| Tài khoản SSH/operator | `prana` |
| System user chạy bot | `botuser` (không có shell login) |
| Repo | `/home/botuser/buy_dips` |
| Virtual environment | `/home/botuser/buy_dips/.venv` |
| Config dev canary | `/home/botuser/buy_dips/config.canary.yaml` |
| SQLite | `/home/botuser/buy_dips/data/canary.sqlite` |
| Dev keystore | `/home/botuser/buy_dips/data/wallet/trader-dev.json` |
| Audit log | `/home/botuser/buy_dips/data/logs/trading-canary.jsonl` |
| Credential nguồn | `/etc/prana-buy-dips/credentials/` (root-only) |
| Wrapper | `/usr/local/libexec/prana-buy-dips-run` (root-owned) |
| systemd template service | `/etc/systemd/system/prana-buy-dips@.service` |
| systemd template timer | `/etc/systemd/system/prana-buy-dips@.timer` |

Repo được đặt trong home của `botuser`, không đặt lâu dài dưới `/home/prana`. Như vậy không cần nới quyền traverse/read cho home cá nhân của `prana`.

## 2. Chuẩn bị Pi

SSH vào Pi:

```bash
ssh prana@<PI_HOST_OR_IP>
```

Cập nhật package index và cài các công cụ cần thiết:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip sqlite3
python3 --version
systemd --version
```

Python phải từ `3.10` trở lên. Nên dùng systemd có hỗ trợ `LoadCredential=` (Ubuntu 22.04/systemd 249 trở lên là phù hợp).

Tạo system user không có password và không thể SSH/login shell:

```bash
sudo adduser \
  --system \
  --group \
  --home /home/botuser \
  --shell /usr/sbin/nologin \
  botuser

sudo install -d -o botuser -g botuser -m 0750 /home/botuser
id botuser
getent passwd botuser
```

Không thêm `botuser` vào nhóm `sudo`, `docker`, `adm` hoặc nhóm ví nào khác.

## 3. Clone repo vào Home

### Cách khuyến nghị cho repo public hoặc có deploy key riêng

Thay `<REPO_GIT_URL>` bằng URL thật:

```bash
sudo -u botuser -H git clone <REPO_GIT_URL> /home/botuser/buy_dips
```

Không nhúng GitHub token/PAT trực tiếp vào URL vì URL có thể lọt vào shell history và `.git/config`.

### Nếu repo private và chỉ tài khoản `prana` có quyền clone

Clone tạm trong home của `prana`, sau đó chuyển nguyên repo sang home của bot:

```bash
cd /home/prana
git clone <REPO_GIT_URL> buy_dips
sudo mv /home/prana/buy_dips /home/botuser/buy_dips
sudo chown -R botuser:botuser /home/botuser/buy_dips
```

Không dùng `chmod 711 /home/prana` chỉ để bot đọc repo.

## 4. Cài Python và chạy test trước khi khóa quyền code

```bash
sudo -u botuser -H bash -c '
  set -e
  cd /home/botuser/buy_dips
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
  .venv/bin/pytest -q
'
```

Không tiếp tục rollout nếu test fail.

## 5. Tạo thư mục runtime và config dev canary

Tạo các thư mục chỉ `botuser` truy cập được:

```bash
sudo install -d -o botuser -g botuser -m 0700 /home/botuser/buy_dips/data
sudo install -d -o botuser -g botuser -m 0700 /home/botuser/buy_dips/data/wallet
sudo install -d -o botuser -g botuser -m 0700 /home/botuser/buy_dips/data/logs
```

Tạo config từ mẫu:

```bash
sudo -u botuser cp \
  /home/botuser/buy_dips/config.example.yaml \
  /home/botuser/buy_dips/config.canary.yaml
sudoedit /home/botuser/buy_dips/config.canary.yaml
```

Giữ nguyên các tham số zone/strategy từ file mẫu và đảm bảo các trường vận hành sau có giá trị này:

```yaml
database_path: /home/botuser/buy_dips/data/canary.sqlite
environment: dev

wallet:
  keystore_path: /home/botuser/buy_dips/data/wallet/trader-dev.json
  expected_address: null
  password_env: KEYSTORE_PASSWORD

execution:
  chain_id: 137
  rpc_url_env: POLYGON_RPC_URL
  quote_base_url: https://prana.triethocduongpho.net
  router_allowlist:
    - "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"
  live_enabled: false
  slippage_bps: 50
  quote_timeout_seconds: 10
  quote_min_deadline_seconds: 30
  max_swap_gas: 750000
  max_approval_gas: 100000
  receipt_timeout_seconds: 120

risk:
  trade_amount_usdt: "1"
  max_trades_per_utc_day: 3
  max_cumulative_usdt: "10"
  min_pol_reserve: "0.01"
  pause_file: /home/botuser/buy_dips/data/PAUSE_TRADING

logging:
  level: INFO
  file_path: /home/botuser/buy_dips/data/logs/trading-canary.jsonl
  max_bytes: 2000000
  backup_count: 5

price_feed:
  timeframe: 1h
  fetch_limit: 168
```

Không đặt password, private key, mnemonic, RPC URL hoặc Git token trong YAML.

## 6. Secrets cần thiết

| Secret | Cần ở mode nào | Ý nghĩa |
|---|---|---|
| `KEYSTORE_PASSWORD` | `dry_run`, `live`, wallet commands | Giải mã keystore cục bộ |
| `POLYGON_RPC_URL` | `dry_run`, `live`, `trade-check`, approve/revoke | Polygon JSON-RPC URL; URL có thể chứa API key |
| `LIVE_TRADING_CONFIRMATION` | Chỉ `live` | Phải đúng `polygon:137:<checksum wallet address>` |

Không cần Binance API key: feed dùng endpoint Binance Spot public. Quote API hiện cũng không yêu cầu secret cấu hình; verification token đến từ response và không được lưu.

Giai đoạn dev canary **không tạo** `LIVE_TRADING_CONFIRMATION`.

Tạo thư mục credential root-only:

```bash
sudo install -d -o root -g root -m 0700 /etc/prana-buy-dips
sudo install -d -o root -g root -m 0700 /etc/prana-buy-dips/credentials
```

Nhập keystore password mà không đưa giá trị vào command history:

```bash
sudo bash -c '
  umask 077
  IFS= read -r -s -p "Dev keystore password: " secret
  printf "\n"
  test -n "$secret"
  printf "%s" "$secret" > /etc/prana-buy-dips/credentials/keystore_password
  unset secret
'
```

Nhập Polygon RPC URL theo cách tương tự:

```bash
sudo bash -c '
  umask 077
  IFS= read -r -s -p "Polygon RPC URL: " secret
  printf "\n"
  test -n "$secret"
  printf "%s" "$secret" > /etc/prana-buy-dips/credentials/polygon_rpc_url
  unset secret
'
```

Khóa quyền và chỉ kiểm tra metadata, không in nội dung:

```bash
sudo chown root:root /etc/prana-buy-dips/credentials/keystore_password
sudo chown root:root /etc/prana-buy-dips/credentials/polygon_rpc_url
sudo chmod 0600 /etc/prana-buy-dips/credentials/keystore_password
sudo chmod 0600 /etc/prana-buy-dips/credentials/polygon_rpc_url
sudo stat -c '%U %G %a %n' /etc/prana-buy-dips/credentials/*
```

`LoadCredential=` sẽ copy các file này vào credential directory tạm dưới `/run/credentials/...` cho đúng service. Bot không đọc trực tiếp `/etc/prana-buy-dips/credentials`.

## 7. Tạo wrapper chuyển systemd credentials sang env

Code hiện nhận `KEYSTORE_PASSWORD` và `POLYGON_RPC_URL` qua environment. Wrapper dưới đây chỉ đọc credential runtime do systemd cấp, export trong tiến trình con rồi `exec` bot. Secret không nằm trong unit, YAML hoặc command line.

```bash
sudo install -d -o root -g root -m 0755 /usr/local/libexec
sudoedit /usr/local/libexec/prana-buy-dips-run
```

Nội dung file:

```bash
#!/usr/bin/env bash
set -euo pipefail

: "${CREDENTIALS_DIRECTORY:?systemd credential directory is missing}"
: "${CONFIG_PATH:=/home/botuser/buy_dips/config.canary.yaml}"
: "${TRADING_MODE:=observe}"

export KEYSTORE_PASSWORD="$(<"${CREDENTIALS_DIRECTORY}/keystore_password")"
export POLYGON_RPC_URL="$(<"${CREDENTIALS_DIRECTORY}/polygon_rpc_url")"

if [[ -f "${CREDENTIALS_DIRECTORY}/live_trading_confirmation" ]]; then
  export LIVE_TRADING_CONFIRMATION="$(<"${CREDENTIALS_DIRECTORY}/live_trading_confirmation")"
fi

case "${1:-trade-once}" in
  trade-once)
    exec /home/botuser/buy_dips/.venv/bin/python \
      -m src.cli --config "${CONFIG_PATH}" \
      trade-once --mode "${TRADING_MODE}"
    ;;
  wallet-create|wallet-status|trade-check|approve-trading|revoke-trading)
    exec /home/botuser/buy_dips/.venv/bin/python \
      -m src.cli --config "${CONFIG_PATH}" "$1"
    ;;
  *)
    printf 'Unsupported bot command\n' >&2
    exit 64
    ;;
esac
```

Khóa owner/quyền:

```bash
sudo chown root:root /usr/local/libexec/prana-buy-dips-run
sudo chmod 0755 /usr/local/libexec/prana-buy-dips-run
sudo bash -n /usr/local/libexec/prana-buy-dips-run
```

## 8. Tạo dev keystore bằng transient systemd service

Lúc này `expected_address` trong YAML vẫn là `null`. Chạy wallet-create với credential runtime:

```bash
sudo systemd-run \
  --quiet --wait --pipe --collect \
  --unit=prana-buy-dips-wallet-create \
  --uid=botuser --gid=botuser \
  --working-directory=/home/botuser/buy_dips \
  --setenv=CONFIG_PATH=/home/botuser/buy_dips/config.canary.yaml \
  --property=LoadCredential=keystore_password:/etc/prana-buy-dips/credentials/keystore_password \
  --property=LoadCredential=polygon_rpc_url:/etc/prana-buy-dips/credentials/polygon_rpc_url \
  /usr/local/libexec/prana-buy-dips-run wallet-create
```

Command chỉ in public checksum address. Copy address đó vào:

```yaml
wallet:
  expected_address: "0x...checksum address vừa in..."
```

Sau đó kiểm tra metadata của keystore, không mở nội dung:

```bash
sudo chown botuser:botuser /home/botuser/buy_dips/data/wallet/trader-dev.json
sudo chmod 0600 /home/botuser/buy_dips/data/wallet/trader-dev.json
sudo stat -c '%U %G %a %n' /home/botuser/buy_dips/data/wallet/trader-dev.json
```

## 9. Backfill và khởi tạo dữ liệu

Các lệnh này chỉ dùng Binance public, không cần secret:

```bash
sudo -u botuser -H bash -c '
  set -e
  cd /home/botuser/buy_dips
  .venv/bin/python -m src.cli --config config.canary.yaml init-db
  .venv/bin/python -m src.cli --config config.canary.yaml backfill --timeframe 1h
  .venv/bin/python -m src.cli --config config.canary.yaml backfill --timeframe 4h
'
```

4h backfill là bước warm-up an toàn. Cycle hourly về sau chỉ fetch Binance `1h` và tự derive candle `4h` đã đóng.

Kiểm tra nhanh dữ liệu:

```bash
sudo -u botuser sqlite3 /home/botuser/buy_dips/data/canary.sqlite \
  "SELECT timeframe, COUNT(*), datetime(MIN(open_time)/1000,'unixepoch'), datetime(MAX(open_time)/1000,'unixepoch') FROM candles GROUP BY timeframe;"
```

## 10. Khóa quyền repo sau khi setup

Bot chỉ cần ghi vào `data/`; source code, venv và config nên read-only đối với `botuser` khi chạy:

```bash
sudo chown -R root:botuser /home/botuser/buy_dips
sudo find /home/botuser/buy_dips -type d -exec chmod 0750 {} +
sudo find /home/botuser/buy_dips -type f -exec chmod 0640 {} +
sudo find /home/botuser/buy_dips/.venv/bin -type f -exec chmod 0750 {} +

sudo chown -R botuser:botuser /home/botuser/buy_dips/data
sudo chmod 0700 /home/botuser/buy_dips/data
sudo chmod 0700 /home/botuser/buy_dips/data/wallet
sudo chmod 0700 /home/botuser/buy_dips/data/logs
sudo find /home/botuser/buy_dips/data/wallet -maxdepth 1 -type f -exec chmod 0600 {} +
sudo chmod 0640 /home/botuser/buy_dips/config.canary.yaml
```

`root:botuser` cho phép service đọc code/config, nhưng chỉ root sửa chúng. `data/` vẫn thuộc `botuser` để SQLite, log, pause file và keystore hoạt động.

## 11. systemd service đề xuất

Tạo template service:

```bash
sudoedit /etc/systemd/system/prana-buy-dips@.service
```

Nội dung:

```ini
[Unit]
Description=PRANA buy-dips hourly cycle (%i)
Documentation=file:///home/botuser/buy_dips/docs/pi-rollout-runbook.md
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=botuser
Group=botuser
WorkingDirectory=/home/botuser/buy_dips
UMask=0077

Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=CONFIG_PATH=/home/botuser/buy_dips/config.canary.yaml
Environment=TRADING_MODE=%i

LoadCredential=keystore_password:/etc/prana-buy-dips/credentials/keystore_password
LoadCredential=polygon_rpc_url:/etc/prana-buy-dips/credentials/polygon_rpc_url

ExecStart=/usr/local/libexec/prana-buy-dips-run trade-once
TimeoutStartSec=15min

NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/botuser/buy_dips/data
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
RestrictSUIDSGID=true
LockPersonality=true
CapabilityBoundingSet=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=multi-user.target
```

Không thêm `Restart=always` cho oneshot trading cycle. Khi một cycle fail-closed, operator review log và timer thử lại ở giờ kế tiếp; không tạo vòng retry nhanh ngoài ý muốn.

Chỉ các instance `observe`, `dry_run`, `live` là hợp lệ với CLI. Trong rollout dev canary chỉ dùng hai instance đầu.

## 12. systemd timer đề xuất

Tạo template timer:

```bash
sudoedit /etc/systemd/system/prana-buy-dips@.timer
```

Nội dung:

```ini
[Unit]
Description=Run PRANA buy-dips %i cycle hourly

[Timer]
OnCalendar=*-*-* *:00:10 UTC
Persistent=true
AccuracySec=1s
RandomizedDelaySec=0
Unit=prana-buy-dips@%i.service

[Install]
WantedBy=timers.target
```

Cycle chạy khoảng 10 giây sau mỗi giờ UTC. `Persistent=true` chạy bù một cycle sau khi Pi bật lại; logic database/decision có tính idempotent.

Nạp và kiểm tra unit:

```bash
sudo systemctl daemon-reload
sudo systemd-analyze verify \
  /etc/systemd/system/prana-buy-dips@.service \
  /etc/systemd/system/prana-buy-dips@.timer
sudo systemctl cat prana-buy-dips@.service
sudo systemctl cat prana-buy-dips@.timer
```

## 13. Rollout pha A — observe nhiều ngày

Đặt pause file trước khi bắt đầu. Nó không cản `observe`, nhưng là dây an toàn nếu operator vô tình bật `dry_run`:

```bash
sudo -u botuser touch /home/botuser/buy_dips/data/PAUSE_TRADING
sudo chmod 0600 /home/botuser/buy_dips/data/PAUSE_TRADING
```

Chạy thủ công một cycle observe:

```bash
sudo systemctl start prana-buy-dips@observe.service
sudo systemctl status prana-buy-dips@observe.service --no-pager
sudo journalctl -u prana-buy-dips@observe.service -n 100 --no-pager
```

Service oneshot ở trạng thái `inactive (dead)` sau khi thành công là bình thường. Kết quả command phải cho thấy decision row, decision/reason và trạng thái rebuild zone; không được có execution/broadcast.

Bật timer observe:

```bash
sudo systemctl enable --now prana-buy-dips@observe.timer
systemctl list-timers 'prana-buy-dips@*'
```

Theo dõi hằng ngày, đề xuất ít nhất 3–7 ngày:

```bash
sudo systemctl status prana-buy-dips@observe.timer --no-pager
sudo journalctl -u prana-buy-dips@observe.service --since '24 hours ago' --no-pager -o cat | jq -C -R 'fromjson? // .'
sudo -u botuser sqlite3 /home/botuser/buy_dips/data/canary.sqlite \
  "SELECT decision, reason_code, candle_open_time_utc7 FROM decisions_readable WHERE mode='observe' ORDER BY candle_open_time DESC LIMIT 48;"
```

Checklist trước khi qua pha B:

- Timer chạy đúng mỗi giờ và không có hai timer mode cùng bật.
- Binance fetch ổn định; không có gap 1h hoặc overdue incomplete 4h lặp lại.
- Mỗi candle giờ chỉ có một decision observe.
- Zone watermark tiến đều theo mỗi candle 4h hoàn tất.
- Audit log không chứa password, RPC URL, calldata hoặc private material.
- Disk, clock/NTP và network của Pi ổn định.

Kiểm tra clock:

```bash
timedatectl status
df -h /home/botuser/buy_dips/data
```

## 14. Kiểm tra wallet/RPC trước dry_run

Chạy `wallet-status` và `trade-check` qua transient unit để vẫn dùng `LoadCredential=`:

```bash
sudo systemd-run \
  --quiet --wait --pipe --collect \
  --unit=prana-buy-dips-wallet-status \
  --uid=botuser --gid=botuser \
  --working-directory=/home/botuser/buy_dips \
  --setenv=CONFIG_PATH=/home/botuser/buy_dips/config.canary.yaml \
  --property=LoadCredential=keystore_password:/etc/prana-buy-dips/credentials/keystore_password \
  --property=LoadCredential=polygon_rpc_url:/etc/prana-buy-dips/credentials/polygon_rpc_url \
  /usr/local/libexec/prana-buy-dips-run wallet-status

sudo systemd-run \
  --quiet --wait --pipe --collect \
  --unit=prana-buy-dips-trade-check \
  --uid=botuser --gid=botuser \
  --working-directory=/home/botuser/buy_dips \
  --setenv=CONFIG_PATH=/home/botuser/buy_dips/config.canary.yaml \
  --property=LoadCredential=keystore_password:/etc/prana-buy-dips/credentials/keystore_password \
  --property=LoadCredential=polygon_rpc_url:/etc/prana-buy-dips/credentials/polygon_rpc_url \
  /usr/local/libexec/prana-buy-dips-run trade-check
```

Address in ra phải trùng tuyệt đối `wallet.expected_address`. `trade-check` phải xác nhận chain 137, router/token contracts, balance và allowance.

`dry_run` yêu cầu ví có ít nhất 1 USDT và đủ POL reserve. Mô phỏng exact swap có thể cần allowance đã có sẵn. Lệnh `approve-trading` là **giao dịch Polygon thật**, ngay cả khi app config đang là dev; không chạy lệnh đó chỉ để “thử”. Nếu thật sự cần allowance để dry-run simulation thành công, operator phải review/fund dev wallet và chủ động approve capped 10 USDT như một thao tác on-chain riêng.

## 15. Rollout pha B — dry_run nhiều ngày

Đảm bảo timer observe dừng trước:

```bash
sudo systemctl disable --now prana-buy-dips@observe.timer
systemctl list-timers 'prana-buy-dips@*'
```

Gỡ pause khi đã sẵn sàng cho quote/simulation:

```bash
sudo -u botuser rm /home/botuser/buy_dips/data/PAUSE_TRADING
```

Chạy một dry-run cycle thủ công rồi mới bật timer:

```bash
sudo systemctl start prana-buy-dips@dry_run.service
sudo systemctl status prana-buy-dips@dry_run.service --no-pager
sudo journalctl -u prana-buy-dips@dry_run.service -n 100 --no-pager

sudo systemctl enable --now prana-buy-dips@dry_run.timer
systemctl list-timers 'prana-buy-dips@*'
```

Phần lớn giờ có thể là HOLD và khi đó bot không mở ví/RPC. Chỉ BUY mới tạo `trade_executions` và đi qua quote/simulation.

Theo dõi ít nhất vài ngày và review cả decisions lẫn execution lifecycle:

```bash
sudo -u botuser sqlite3 /home/botuser/buy_dips/data/canary.sqlite \
  "SELECT id, decision, reason_code, candle_open_time_utc7 FROM decisions_readable WHERE mode='dry_run' ORDER BY id DESC LIMIT 48;"

sudo -u botuser sqlite3 /home/botuser/buy_dips/data/canary.sqlite \
  "SELECT id, decision_id, mode, status, reason, transaction_hash, updated_at_utc7 FROM trade_executions_readable ORDER BY id DESC LIMIT 30;"
```

Trong dev canary:

- `trade_executions.mode` chỉ được là `dry_run`.
- Thành công kết thúc ở `simulated`.
- Không được có trạng thái `signed`, `broadcast`, `pending`, `confirmed` hoặc transaction hash.
- Nếu có `started`, `risk_checked` hoặc `quoted` bị treo, dừng timer và điều tra; không sửa/xóa row thủ công.

## 16. Dừng khẩn cấp và rollback mode

Dừng mọi timer/cycle:

```bash
sudo systemctl disable --now prana-buy-dips@observe.timer
sudo systemctl disable --now prana-buy-dips@dry_run.timer
sudo systemctl stop prana-buy-dips@observe.service
sudo systemctl stop prana-buy-dips@dry_run.service
```

Chặn execution nhưng vẫn có thể quay lại observe:

```bash
sudo -u botuser touch /home/botuser/buy_dips/data/PAUSE_TRADING
sudo chmod 0600 /home/botuser/buy_dips/data/PAUSE_TRADING
sudo systemctl enable --now prana-buy-dips@observe.timer
```

Không xóa database, keystore hoặc execution row để “reset”. Với transaction chưa rõ trạng thái, phải reconcile transaction hash trước.

## 17. Gate trước khi cân nhắc live thật

Không đổi sang live chỉ bằng cách thay timer instance. Tối thiểu phải hoàn tất riêng các việc sau:

1. Tạo **prod keystore mới trên Pi**, không tái sử dụng `trader-dev.json`.
2. Tạo `config.prod.yaml` với `environment: prod`, prod checksum address và `quote_base_url: http://127.0.0.1:4173`.
3. Cài/chứng minh local route server đang nghe `127.0.0.1:4173`; bot repo này không tự start route server.
4. Giữ `live_enabled: false`, chạy `trade-check`, review balances/allowance và backup SQLite trước.
5. Tạo credential password riêng cho prod keystore. Không overwrite dev secret cho đến khi dev canary đã dừng và có rollback plan.
6. Chỉ sau operator review mới đặt `live_enabled: true` và tạo `LIVE_TRADING_CONFIRMATION` đúng `polygon:137:<prod checksum address>` qua `LoadCredential=`.
7. Bắt đầu với pause file hiện diện; kiểm tra manual một cycle, gỡ pause có chủ đích, rồi mới bật `prana-buy-dips@live.timer`.

Live guard còn bắt buộc đúng chain 137, wallet pinned, local quote host, 1 USDT/trade, tối đa 3 attempts/ngày UTC và cumulative cap 10 USDT. Tuy vậy các guard phần mềm không thay thế review vận hành.

## 18. Cập nhật repo về sau

Script update trong repo tự làm tuần tự các bước an toàn sau:

- Chặn hai lần deploy chạy đồng thời và từ chối chạy nếu timer `live` đang active/enabled.
- Ghi nhớ timer canary đang enabled, tắt timer đó và chờ oneshot đang chạy kết thúc; script không kill cycle giữa lúc ghi DB.
- Tạo SQLite backup nhất quán có timestamp trong `data/backups/`, rồi chạy `PRAGMA quick_check`.
- Dừng nếu tracked file có local change; pull chỉ bằng `git pull --ff-only`.
- Tạm trao ownership repo cho `botuser`; chỉ chạy pip khi `requirements.txt` thực sự thay đổi; chạy full pytest.
- Khóa lại source/config/venv theo mục **Khóa quyền repo**, nhưng giữ `data/` writable cho `botuser`.
- Cập nhật bản script root-owned, chạy một observe cycle thủ công, rồi bật lại đúng timer đã enabled trước deploy.

Cài command root-owned một lần sau khi repo đã có file script:

```bash
sudo install -o root -g root -m 0750 \
  /home/botuser/buy_dips/scripts/prana-buy-dips-update \
  /usr/local/sbin/prana-buy-dips-update
```

Những lần update sau chỉ cần:

```bash
sudo /usr/local/sbin/prana-buy-dips-update
```

Hoặc gọi từ Mac:

```bash
ssh -t rp5 'sudo /usr/local/sbin/prana-buy-dips-update'
```

Script hiện dành riêng cho canary DB/config và timer `observe`/`dry_run` trong runbook này. Nó chủ động từ chối update khi timer `live` active/enabled; deployment live về sau cần script riêng gắn đúng prod config, DB và rollback policy.

Nếu không có timer nào enabled trước update, script chạy verify observe nhưng vẫn để tất cả timer disabled. Nếu bất kỳ bước nào fail, script cố khóa lại quyền repo và **không bật lại timer**; nó không tự rollback Git commit, vì vậy phải đọc lỗi và kiểm tra code/ownership/service trước khi bật timer thủ công. Script không sửa config, credential, pause file hoặc systemd unit, vì vậy không cần `daemon-reload`.

Các backup có timestamp không bị tự xóa để tránh script tự quyết định retention. Theo dõi dung lượng `data/backups/` và chỉ xóa các bản cũ sau khi đã xác nhận bản deploy mới ổn định.

## 19. Checklist cuối

- [ ] `botuser` không có shell login và không có sudo.
- [ ] Repo ở `/home/botuser/buy_dips`, không cần mở quyền `/home/prana`.
- [ ] Keystore là `0600`, wallet/log/data directories là `0700`.
- [ ] Credential source là root-owned `0600`; không có secret trong YAML, unit hoặc `.env`.
- [ ] `config.canary.yaml` dùng `environment: dev`, public dev quote URL và `live_enabled: false`.
- [ ] Full pytest pass trên Pi.
- [ ] 1h và 4h history đã backfill.
- [ ] Chỉ một trong `observe.timer` / `dry_run.timer` được enable.
- [ ] Observe ổn định nhiều ngày trước dry-run.
- [ ] Dry-run không có signing/broadcast/transaction hash.
- [ ] Prod live về sau dùng keystore, config và credentials riêng.
