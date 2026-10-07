# Chuyển `dry_run` sang `live` trên production

Tài liệu này dành cho operator trên Raspberry Pi production. `live` sẽ ký và broadcast giao dịch Polygon thật: mỗi BUY là **20 USDT**, tối đa **1 giao dịch đã ký mỗi ngày UTC**. Chỉ thực hiện khi đã có người review và chấp nhận rủi ro.

## Nguyên tắc bắt buộc

- Không chỉ đổi `--mode dry_run` thành `--mode live`.
- Không dùng lại keystore dev/canary. Prod phải có keystore và password riêng.
- Không dùng `https://prana.triethocduongpho.net` trong prod. Prod phải gọi route server local qua `http://127.0.0.1:4173`.
- Không chạy đồng thời timer `dry_run` và `live`.
- Không xóa database hoặc các row execution để “reset”. Transaction chưa rõ trạng thái phải được reconcile.
- Giữ pause file cho tới khi đã kiểm tra xong config, wallet, route server và service live.

Các bước dưới đây giả sử repo ở `/home/botuser/buy_dips`, service chạy bằng `botuser`, wrapper là `/usr/local/libexec/prana-buy-dips-run`, và credential nằm dưới `/etc/prana-buy-dips/credentials/`.

## 1. Xác nhận dry-run đã đạt gate

Chỉ promote khi tất cả điều kiện sau đúng:

- Observe đã ổn định nhiều ngày; dữ liệu 1h/4h không bị gap lặp lại.
- Dry-run đã chạy nhiều ngày và các BUY kết thúc ở `simulated`.
- Không có `signed`, `broadcast`, `pending`, `confirmed` hoặc transaction hash trong dry-run.
- Không có execution đang ở `started`, `risk_checked`, `quoted` hoặc `allowance_ready`.
- Đã review strategy, zone, reason code và quote/simulation gần nhất.
- Route server prod đã được vận hành riêng và nghe trên `127.0.0.1:4173`.

Kiểm tra timer và execution:

```bash
sudo systemctl list-timers 'prana-buy-dips@*' --all
sudo -u botuser sqlite3 /home/botuser/buy_dips/data/canary.sqlite \
  "SELECT id, mode, status, reason, transaction_hash, updated_at_utc7 FROM trade_executions_readable ORDER BY id DESC LIMIT 30;"
```

Nếu còn execution chưa terminal, dừng quy trình ở đây. Không sửa hoặc xóa row thủ công.

## 2. Dừng dry-run và tạo backup

Pause trước, sau đó disable timer để không có cycle mới chạy trong lúc chuyển config:

```bash
sudo -u botuser touch /home/botuser/buy_dips/data/PAUSE_TRADING
sudo chmod 0600 /home/botuser/buy_dips/data/PAUSE_TRADING

sudo systemctl disable --now prana-buy-dips@dry_run.timer
sudo systemctl stop prana-buy-dips@dry_run.service
sudo systemctl list-timers 'prana-buy-dips@*' --all
```

Tạo backup SQLite và kiểm tra backup đọc được. Dùng path rõ ràng, không overwrite backup cũ:

```bash
stamp="$(date -u '+%Y%m%dT%H%M%SZ')"
sudo -u botuser mkdir -p /home/botuser/buy_dips/data/backups
sudo -u botuser sqlite3 /home/botuser/buy_dips/data/canary.sqlite \
  ".backup '/home/botuser/buy_dips/data/backups/pre-live-${stamp}.sqlite'"
sudo -u botuser sqlite3 \
  "/home/botuser/buy_dips/data/backups/pre-live-${stamp}.sqlite" \
  'PRAGMA quick_check;'
```

Kết quả cuối phải là `ok`. Giữ lại tên file backup để rollback/audit.

## 3. Tạo config prod riêng

Không sửa config canary để biến nó thành prod. Tạo file mới rồi mở bằng editor:

```bash
sudo cp /home/botuser/buy_dips/config.canary.yaml \
  /home/botuser/buy_dips/config.prod.yaml
sudo chown root:botuser /home/botuser/buy_dips/config.prod.yaml
sudo chmod 0640 /home/botuser/buy_dips/config.prod.yaml
sudoedit /home/botuser/buy_dips/config.prod.yaml
```

Trong `config.prod.yaml`, giữ nguyên strategy đã được review và sửa tối thiểu các trường sau:

```yaml
database_path: /home/botuser/buy_dips/data/canary.sqlite
environment: prod

wallet:
  keystore_path: /home/botuser/buy_dips/data/wallet/trader-prod.json
  expected_address: "0xPROD_CHECKSUM_ADDRESS"
  password_env: KEYSTORE_PASSWORD

execution:
  quote_base_url: http://127.0.0.1:4173
  live_enabled: false

risk:
  trade_amount_usdt: "20"
  max_trades_per_utc_day: 1
```

Dùng cùng database hiện tại nếu muốn giữ lại history BUY/cooldown của observe và dry-run. Không tự ý tạo database rỗng cho live; làm vậy có thể khiến strategy không thấy lịch sử BUY cũ. Nếu cần database prod riêng, phải có kế hoạch backfill và operator review riêng trước khi bật live.

Kiểm tra bằng mắt:

- `environment` là `prod`.
- `keystore_path` là `trader-prod.json`, không phải `trader-dev.json`.
- `expected_address` là checksum address của prod wallet.
- `quote_base_url` đúng `http://127.0.0.1:4173`.
- `live_enabled` vẫn là `false` cho tới bước 8.
- Không có password, RPC URL, private key hoặc token trong YAML.

## 4. Tạo và kiểm tra prod keystore/credentials

Prod keystore phải được tạo hoặc đặt trên Pi, không copy private material sang máy dev. Đảm bảo quyền file:

```bash
sudo stat -c '%U %G %a %n' \
  /home/botuser/buy_dips/data/wallet/trader-prod.json
```

Phải là owner `botuser`, mode `0600`. Dùng transient service để kiểm tra address; lệnh chỉ được in public address:

```bash
sudo systemd-run \
  --quiet --wait --pipe --collect \
  --unit=prana-buy-dips-prod-wallet-status \
  --uid=botuser --gid=botuser \
  --working-directory=/home/botuser/buy_dips \
  --setenv=CONFIG_PATH=/home/botuser/buy_dips/config.prod.yaml \
  --property=LoadCredential=keystore_password:/etc/prana-buy-dips/credentials/prod_keystore_password \
  --property=LoadCredential=polygon_rpc_url:/etc/prana-buy-dips/credentials/polygon_rpc_url \
  /usr/local/libexec/prana-buy-dips-run wallet-status
```

Address in ra phải khớp tuyệt đối `wallet.expected_address`. Tạo credential prod riêng, root-only; không overwrite credential canary:

```bash
sudo stat -c '%U %G %a %n' \
  /etc/prana-buy-dips/credentials/prod_keystore_password \
  /etc/prana-buy-dips/credentials/polygon_rpc_url
```

Hai file phải thuộc `root:root`, mode `0600`. Nội dung secret không được in ra terminal, journal hoặc commit.

## 5. Kiểm tra prod wallet, chain và route server

Chạy `trade-check` bằng config prod và credential prod:

```bash
sudo systemd-run --quiet --wait --pipe --collect \
  --unit=prana-buy-dips-prod-trade-check \
  --uid=botuser --gid=botuser \
  --working-directory=/home/botuser/buy_dips \
  --setenv=CONFIG_PATH=/home/botuser/buy_dips/config.prod.yaml \
  --property=LoadCredential=keystore_password:/etc/prana-buy-dips/credentials/prod_keystore_password \
  --property=LoadCredential=polygon_rpc_url:/etc/prana-buy-dips/credentials/polygon_rpc_url \
  /usr/local/libexec/prana-buy-dips-run trade-check
```

Chỉ tiếp tục nếu:

- Chain là Polygon `137`.
- Token/router/bytecode/decimals đúng allowlist.
- Prod wallet có ít nhất `20 USDT` và đủ POL reserve.
- Route server local đang hoạt động và health check nội bộ thành công.
- Allowance hiện tại đã được review. `approve-trading` là giao dịch on-chain thật, kể cả khi app chưa bật live; không chạy nếu chưa có phê duyệt operator.

## 6. Tạo unit live riêng

Unit template hiện tại dùng config canary. Không bật `prana-buy-dips@live.timer` nếu unit đó vẫn trỏ vào `config.canary.yaml`. Tạo service riêng để config và credential prod không thể nhầm với canary:

```bash
sudoedit /etc/systemd/system/prana-buy-dips-prod-live.service
```

Nội dung tối thiểu:

```ini
[Unit]
Description=PRANA buy-dips production live cycle
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
Environment=CONFIG_PATH=/home/botuser/buy_dips/config.prod.yaml
Environment=TRADING_MODE=live

LoadCredential=keystore_password:/etc/prana-buy-dips/credentials/prod_keystore_password
LoadCredential=polygon_rpc_url:/etc/prana-buy-dips/credentials/polygon_rpc_url
LoadCredential=live_trading_confirmation:/etc/prana-buy-dips/credentials/live_trading_confirmation

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
```

Tạo timer riêng, chưa enable:

```bash
sudoedit /etc/systemd/system/prana-buy-dips-prod-live.timer
```

```ini
[Unit]
Description=Run PRANA buy-dips production live cycle hourly

[Timer]
OnCalendar=*-*-* *:00:10 UTC
Persistent=true
AccuracySec=1s
RandomizedDelaySec=0
Unit=prana-buy-dips-prod-live.service

[Install]
WantedBy=timers.target
```

Kiểm tra unit:

```bash
sudo systemctl daemon-reload
sudo systemd-analyze verify \
  /etc/systemd/system/prana-buy-dips-prod-live.service \
  /etc/systemd/system/prana-buy-dips-prod-live.timer
sudo systemctl cat prana-buy-dips-prod-live.service
```

## 7. Chuẩn bị live guard nhưng chưa bật timer

Tạo credential confirmation bằng editor root-only, không đặt giá trị vào command history:

```bash
sudoedit /etc/prana-buy-dips/credentials/live_trading_confirmation
sudo chown root:root /etc/prana-buy-dips/credentials/live_trading_confirmation
sudo chmod 0600 /etc/prana-buy-dips/credentials/live_trading_confirmation
```

Nội dung duy nhất phải là:

```text
polygon:137:0x3573660B92FCD699cA0126E1c2785e687D6484E3
```

Sau đó sửa `config.prod.yaml` thành `live_enabled: true`. Trước khi chạy, đối chiếu lại ba giá trị này trên cùng một dòng kiểm tra:

```bash
sudo -u botuser grep -nE '^(environment:|  keystore_path:|  expected_address:|  quote_base_url:|  live_enabled:)' \
  /home/botuser/buy_dips/config.prod.yaml
```

Không grep hoặc cat credential secret. `live` sẽ tự fail-closed nếu environment, wallet, loopback host hoặc confirmation không khớp.

## 8. Chạy manual một cycle có pause

Pause file vẫn phải tồn tại. Chạy service một lần để kiểm tra wiring, config và credential:

```bash
sudo systemctl start prana-buy-dips-prod-live.service
sudo systemctl status prana-buy-dips-prod-live.service --no-pager
sudo journalctl -u prana-buy-dips-prod-live.service -n 100 --no-pager
```

Khi pause file tồn tại, cycle có thể ghi decision nhưng execution phải bị chặn với `PAUSE_FILE_PRESENT`; không được có signing/broadcast. Nếu service lỗi config, wallet, quote host, confirmation hoặc credential, sửa lỗi rồi chạy lại. Không gỡ pause để “thử cho biết”.

## 9. Gỡ pause và bật live có chủ đích

Đây là bước bắt đầu có thể tạo giao dịch thật. Operator phải xác nhận lại địa chỉ wallet và amount ngay trước lệnh:

```bash
sudo -u botuser rm /home/botuser/buy_dips/data/PAUSE_TRADING
sudo systemctl enable --now prana-buy-dips-prod-live.timer
sudo systemctl list-timers prana-buy-dips-prod-live.timer --all
```

Theo dõi cycle đầu tiên:

```bash
sudo journalctl -u prana-buy-dips-prod-live.service -f -o cat
```

Sau cycle, audit cả decision và execution:

```bash
sudo -u botuser sqlite3 /home/botuser/buy_dips/data/canary.sqlite \
  "SELECT id, mode, status, reason, transaction_hash, updated_at_utc7 FROM trade_executions_readable ORDER BY id DESC LIMIT 10;"
```

Nếu có broadcast, ghi lại transaction hash từ audit an toàn và xác nhận receipt trên Polygon explorer. Không chạy lại cycle nhiều lần để “đợi”; rerun dùng hash đã lưu để reconcile.

## 10. Rollback về dry-run/observe

Khi có bất kỳ nghi ngờ nào, pause trước rồi stop live:

```bash
sudo -u botuser touch /home/botuser/buy_dips/data/PAUSE_TRADING
sudo chmod 0600 /home/botuser/buy_dips/data/PAUSE_TRADING
sudo systemctl disable --now prana-buy-dips-prod-live.timer
sudo systemctl stop prana-buy-dips-prod-live.service
```

Nếu cycle đã broadcast, không revoke/xóa execution trước khi receipt được xác nhận. Re-run một lần sau khi hệ thống ổn định để reconcile hash đang pending nếu cần.

Để quay lại thu thập decision mà không execute:

```bash
sudo systemctl enable --now prana-buy-dips@observe.timer
```

Nếu cần quay lại dry-run, chỉ bật `dry_run` sau khi đã kiểm tra không còn live timer active và vẫn giữ pause cho tới khi review xong:

```bash
sudo systemctl enable --now prana-buy-dips@dry_run.timer
```

## 11. Cập nhật code khi live đang chạy

Dùng `sudo /usr/local/sbin/prana-buy-dips-update` sau khi bản script mới đã được cài. Script chỉ tắt và bật lại `prana-buy-dips-prod-live.timer`. Nó từ chối chạy nếu `observe`, `dry_run`, hoặc `prana-buy-dips@live` còn enabled hoặc đang chạy, và không tự start các unit đó.

Cycle kiểm tra tự động chỉ chạy khi `data/PAUSE_TRADING` đang có. Không có pause file thì script không start service live; giờ UTC kế tiếp mới là cycle đầu trên code mới.

## Checklist bàn giao

- [ ] Dry-run đã đạt gate; không còn execution pending/in-flight.
- [ ] Dry-run timer đã disabled; live timer không chạy đồng thời.
- [ ] SQLite backup đã tạo và `PRAGMA quick_check` trả `ok`.
- [ ] Prod keystore khác dev keystore, file `0600`, address khớp checksum.
- [ ] Prod config dùng `environment: prod`, loopback quote host và `live_enabled: true` chỉ sau review.
- [ ] Prod password/RPC/confirmation là credential riêng, root-only.
- [ ] Route server local đã được kiểm tra.
- [ ] `trade-check` pass; wallet đủ 20 USDT và POL reserve.
- [ ] Manual cycle với pause không broadcast.
- [ ] Operator đã chủ động gỡ pause và bật đúng timer live.
- [ ] Cycle đầu tiên, status/receipt và transaction hash đã được review.

Nếu có bất kỳ mục nào chưa đạt, giữ pause và không bật live.
