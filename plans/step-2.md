Hiện tại repo đã xong phần data/signal của bước 1, nhưng chưa có `wallet.py`, chưa cài `web3`/`eth-account`, config chưa có wallet/execution/risk, và CLI vẫn chỉ hỗ trợ `observe`. Vì vậy nên làm `wallet-safety` cho dev theo thứ tự dưới đây.

Quan trọng: đây là dev sub-phase. Chưa đánh dấu toàn bộ `wallet-safety` là `completed`, vì `LoadCredential`, keystore prod, systemd và rollout trên Pi vẫn để sau.

## Phạm vi dev lần này

Sẽ làm:

- Keystore riêng `data/wallet/trader-dev.json`.
- Tạo/đọc keystore mã hóa.
- Nhận password qua biến môi trường hoặc prompt.
- Kiểm tra Polygon chain, token, router, wallet.
- CLI `wallet-create`, `wallet-status`, `trade-check`.
- CLI `approve-trading`, `revoke-trading`.
- Approval tối đa 10 USDT, không unlimited.
- Guard đảm bảo dev không thể vào `live`.
- Unit test mock toàn bộ RPC/signing boundary.

Chưa làm:

- `trader-prod.json`.
- `systemd LoadCredential`.
- Pi Ubuntu.
- Gọi quote API.
- Swap simulation/broadcast.
- Tự động approve trong lúc swap.
- Reconcile swap transaction.

## Bước 1 — Thêm dependency

Trong [requirements.txt](/home/prana/buy_dips/requirements.txt), thêm bản stable hiện tại:

```text
web3>=7.16,<8.0
eth-account>=0.13.7,<0.14
```

Không dùng Web3 8 beta. Hiện stable là Web3 7.16 và eth-account 0.13.7. [PyPI web3](https://pypi.org/project/web3/), [PyPI eth-account](https://pypi.org/project/eth-account/).

Sau đó:

```bash
source .venv/bin/activate
pip install -r requirements.txt
python -c "import web3, eth_account; print(web3.__version__)"
```

## Bước 2 — Gitignore keystore và log

Bổ sung vào [.gitignore](/home/prana/buy_dips/.gitignore):

```gitignore
data/wallet/
data/logs/
*.keystore.json
*.signed-tx
```

Không cần ignore toàn bộ JSON của project.

Sau khi tạo ví phải kiểm tra:

```bash
git status --short
git check-ignore -v data/wallet/trader-dev.json
```

Keystore phải được ignore.

## Bước 3 — Khóa constants on-chain

Mở rộng [src/trading/constants.py](/home/prana/buy_dips/src/trading/constants.py) với:

```python
POLYGON_CHAIN_ID = 137

POLYGON_USDT_ADDRESS = "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"
POLYGON_PRANA_ADDRESS = "0x928277e774F34272717EADFafC3fd802dAfBD0F5"

SWAP_ROUTER_02_ADDRESSES = frozenset({
    "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
})

USDT_DECIMALS = 6
PRANA_DECIMALS = 9
CANARY_ALLOWANCE_USDT_RAW = 10_000_000
TRADE_AMOUNT_USDT_RAW = 1_000_000
```

Có một chi tiết cần xử lý rõ: cùng địa chỉ `0xc213...` hiện có thể trả metadata `USDT0`, trong khi quote API của plan vẫn nhận `tokenInSymbol="USDT"`. Vì vậy nên tách:

```python
QUOTE_TOKEN_IN_SYMBOL = "USDT"
EXPECTED_USDT_ONCHAIN_SYMBOLS = frozenset({"USDT", "USDT0"})
```

Không dùng một biến `USDT_SYMBOL` cho cả HTTP contract và metadata on-chain.

Chỉ giữ ABI tối thiểu:

```python
ERC20_ABI = [
    # symbol()
    # decimals()
    # balanceOf(address)
    # allowance(address,address)
    # approve(address,uint256)
]
```

Không cần tải full ABI từ explorer.

## Bước 4 — Mở rộng config theo dev

Trong [src/config.py](/home/prana/buy_dips/src/config.py), thêm các model:

```python
class WalletConfig(BaseModel):
    keystore_path: str = "data/wallet/trader-dev.json"
    expected_address: str | None = None
    password_env: str = "KEYSTORE_PASSWORD"


class ExecutionConfig(BaseModel):
    chain_id: int = 137
    rpc_url_env: str = "POLYGON_RPC_URL"
    quote_base_url: str = "https://prana.triethocduongpho.net"
    router_allowlist: list[str] = [
        "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"
    ]
    live_enabled: bool = False


class RiskConfig(BaseModel):
    trade_amount_usdt: Decimal = Decimal("1")
    max_cumulative_usdt: Decimal = Decimal("10")
```

Thêm vào `AppConfig`:

```python
environment: Literal["dev", "prod"] = "dev"
wallet: WalletConfig = Field(default_factory=WalletConfig)
execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
risk: RiskConfig = Field(default_factory=RiskConfig)
```

Validator cần fail nếu:

- `environment == "dev"` nhưng keystore chứa `trader-prod`.
- `chain_id != 137`.
- Router trong YAML không thuộc immutable allowlist.
- Dev dùng quote host loopback của prod.
- `trade_amount_usdt != 1`.
- `max_cumulative_usdt > 10`.
- Dev đặt `live_enabled: true`.

Bổ sung vào [config.example.yaml](/home/prana/buy_dips/config.example.yaml):

```yaml
environment: dev

wallet:
  keystore_path: data/wallet/trader-dev.json
  expected_address: null
  password_env: KEYSTORE_PASSWORD

execution:
  chain_id: 137
  rpc_url_env: POLYGON_RPC_URL
  quote_base_url: https://prana.triethocduongpho.net
  router_allowlist:
    - "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"
  live_enabled: false

risk:
  trade_amount_usdt: "1"
  max_cumulative_usdt: "10"
```

RPC URL và password tuyệt đối không đặt trong YAML.

## Bước 5 — Viết `wallet.py`

Tạo `src/trading/wallet.py` với các hàm nhỏ:

```python
resolve_keystore_password(...)
create_encrypted_keystore(...)
read_keystore_address(...)
load_local_account(...)
validate_keystore_permissions(...)
```

### `resolve_keystore_password`

Dev resolution order:

1. Đọc biến `KEYSTORE_PASSWORD`.
2. Nếu thiếu và đang chạy interactive, dùng `getpass.getpass()`.
3. Nếu non-interactive mà thiếu password: fail ngay.
4. `wallet-create` phải nhập lại lần hai để xác nhận.
5. Không log password.

### `create_encrypted_keystore`

Luồng chuẩn:

1. Resolve path.
2. Tạo thư mục cha mode `0700`.
3. Refuse nếu file đã tồn tại; không có `--force`.
4. `Account.create()`.
5. `Account.encrypt(account.key, password)`.
6. Ghi file tạm với mode `0600`.
7. `fsync`.
8. Atomic `os.replace`.
9. `chmod 0600`.
10. Chỉ trả về/print public address.

Không bao giờ print:

- `account.key`.
- Nội dung keystore.
- Password.
- Signed transaction bytes.

### `load_local_account`

1. Kiểm tra file regular, không phải symlink.
2. Kiểm tra mode không rộng hơn `0600`.
3. Parse JSON.
4. `Account.decrypt(...)`.
5. `Account.from_key(...)`.
6. So sánh address giải mã với trường `address` trong keystore.
7. Nếu config có `wallet.expected_address`, phải match checksum.
8. Sai password/tamper/address mismatch đều fail closed.

Web3 khuyến nghị hosted RPC phải ký locally rồi dùng `send_raw_transaction`, không dựa vào account của RPC node. [Web3 account documentation](https://web3py.readthedocs.io/en/stable/web3.eth.account.html).

## Bước 6 — Thêm CLI tạo và xem wallet

Mở rộng [src/cli.py](/home/prana/buy_dips/src/cli.py):

```text
wallet-create
wallet-status
trade-check
approve-trading
revoke-trading
```

`wallet-create`:

```bash
python -m src.cli --config config.yaml wallet-create
```

Expected output duy nhất:

```text
Wallet address: 0x...
```

Sau đó copy address đó vào:

```yaml
wallet:
  expected_address: "0x..."
```

Kiểm tra:

```bash
stat -c '%a %n' data/wallet/trader-dev.json
python -m src.cli --config config.yaml wallet-status
```

Expected permission:

```text
600 data/wallet/trader-dev.json
```

`wallet-status` không được in balance, private key hay nội dung keystore; chỉ address.

## Bước 7 — Viết contract/RPC checks

Nên tạo `src/trading/contract_checks.py`, không nhét hết vào `wallet.py`.

`trade-check` thực hiện lần lượt:

1. Lấy `POLYGON_RPC_URL` từ env.
2. Tạo `Web3.HTTPProvider` với timeout ngắn.
3. `w3.is_connected()` phải true.
4. `w3.eth.chain_id == 137`.
5. Load keystore và verify expected address.
6. `eth_getCode` cho USDT, PRANA và router; bytecode không được rỗng.
7. USDT `decimals() == 6`.
8. USDT symbol thuộc allowlist đã khóa.
9. PRANA `symbol()` và `decimals()` đúng deployment metadata đã chốt.
10. Đọc wallet POL balance.
11. Đọc USDT balance.
12. Đọc `allowance(wallet, router)`.
13. Không log RPC URL vì URL có thể chứa API key.

Output được phép:

```text
Environment: dev
Chain ID: 137
Wallet: 0x...
USDT balance: 0
POL balance: 0
Router: verified
Allowance: 0 USDT
Live: disabled
```

Không có RPC URL trong output.

## Bước 8 — Implement capped approval

Tạo `src/trading/approval.py`.

`approve-trading` là giao dịch on-chain thật, dù dùng dev wallet. Không chạy nếu chưa chủ động nạp một ít POL.

Thứ tự bắt buộc:

1. Chạy toàn bộ `trade-check`.
2. Target spender phải nằm trong router allowlist.
3. Target allowance luôn là `10_000_000` raw units = 10 USDT.
4. Đọc allowance hiện tại.
5. Nếu đúng 10 USDT: no-op.
6. Nếu allowance khác 0: gửi `approve(router, 0)` trước.
7. Chờ receipt thành công và verify allowance đã về 0.
8. Gửi `approve(router, 10_000_000)`.
9. Chờ receipt thành công.
10. Đọc lại allowance và yêu cầu đúng 10 USDT.

Mỗi transaction phải:

- Dùng nonce `"pending"`.
- Có `chainId=137`.
- `eth_call` trước.
- `estimate_gas` trước.
- Kiểm tra gas limit cấu hình.
- Build transaction.
- Sign local.
- Gửi bằng `send_raw_transaction`.
- Chờ receipt.
- Yêu cầu `receipt.status == 1`.

Nếu timeout sau broadcast:

- In transaction hash.
- Dừng.
- Không tự tạo transaction khác với nonce mới.

Không được dùng:

```python
approve(router, 2**256 - 1)
```

Và không được approve PRANA; token cần approve là USDT.

## Bước 9 — Implement revoke

`revoke-trading`:

1. Chạy contract checks.
2. Đọc allowance.
3. Nếu đã bằng 0: no-op.
4. Build `approve(router, 0)`.
5. Simulate và estimate gas.
6. Sign/broadcast.
7. Chờ receipt.
8. Đọc lại allowance, bắt buộc bằng 0.

Ví dụ:

```bash
python -m src.cli --config config.yaml revoke-trading
```

## Bước 10 — Live-mode guards

Tạo một hàm pure, ví dụ trong `src/trading/risk.py`:

```python
assert_live_mode_allowed(
    config,
    wallet_address,
    confirmation,
)
```

Để vào `live`, tất cả phải đồng thời đúng:

```text
environment == "prod"
execution.live_enabled is True
LIVE_WALLET_CONFIRMATION == "polygon:137:<checksum-wallet-address>"
keystore address == wallet.expected_address
quote host == prod loopback host
```

Vì đang làm dev:

```text
environment=dev
live_enabled=false
```

nên `live` luôn bị từ chối, kể cả người dùng vô tình set confirmation.

Hiện tại chưa nên mở `--mode dry_run/live` trong CLI, vì [runner.py](/home/prana/buy_dips/src/trading/runner.py) mới chỉ persist decision, chưa quote/simulate/broadcast. Nếu mở sớm, CLI sẽ tạo cảm giác sai rằng dry-run/live đã hoạt động.

## Bước 11 — Test bắt buộc

Thêm:

```text
tests/test_wallet.py
tests/test_contract_checks.py
tests/test_approval.py
tests/test_live_guard.py
tests/test_cli_wallet.py
```

Các case tối thiểu:

- Tạo keystore và decrypt lại đúng address.
- File mode đúng `0600`.
- Refuse overwrite.
- Wrong password.
- Tampered keystore.
- Expected address mismatch.
- Password env và prompt.
- Không secret nào xuất hiện trong stdout/stderr.
- Wrong chain ID.
- Empty router/token bytecode.
- Wrong token address/symbol/decimals.
- Allowance 0 → approve 10.
- Allowance 3 → approve 0 → approve 10.
- Allowance 10 → no-op.
- Không bao giờ approve lớn hơn 10 USDT.
- Revoke từ nonzero → 0.
- Revoke khi đã 0 → no-op.
- Failed/reverted receipt.
- Broadcast timeout không retry.
- Dev config luôn reject live.
- `observe` không load/decrypt wallet và không gọi RPC.

Mọi RPC/signing test phải mock; test suite bình thường không chạm Polygon.

Chạy:

```bash
pytest -q
```

## Bước 12 — Quy trình kiểm tra dev thủ công

An toàn nhất là để CLI prompt password:

```bash
python -m src.cli --config config.yaml wallet-create
python -m src.cli --config config.yaml wallet-status
```

Nạp RPC URL tạm thời mà không ghi vào shell history:

```bash
read -rsp "Polygon RPC URL: " POLYGON_RPC_URL
export POLYGON_RPC_URL
printf '\n'
python -m src.cli --config config.yaml trade-check
unset POLYGON_RPC_URL
```

Chỉ khi muốn test approval thật với dev wallet đã được nạp rất ít POL:

```bash
python -m src.cli --config config.yaml approve-trading
python -m src.cli --config config.yaml trade-check
python -m src.cli --config config.yaml revoke-trading
```

Sau revoke, `trade-check` phải báo allowance bằng 0.

## Definition of Done cho dev

Dev wallet-safety được coi là xong khi:

- Keystore dev mã hóa, ignored, mode `0600`.
- Không có raw private key trong repo/env/config.
- Wallet address được pin trong config.
- `trade-check` fail closed khi chain/token/router sai.
- Approval không bao giờ vượt 10 USDT.
- Zero-reset được test.
- Revoke được test.
- Dev không thể vào live.
- `observe` hiện tại vẫn chạy mà không yêu cầu wallet/RPC.
- Toàn bộ test pass.
- Chưa có swap transaction nào được gửi.

Phần prod sau này mới bổ sung `trader-prod`, `/var/lib/...`, `LoadCredential`, dedicated user, systemd service/timer và live confirmation thật.