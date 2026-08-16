from __future__ import annotations


EXCHANGE = "binance"
SYMBOL = "BTCUSDT"
HOURLY_TIMEFRAME = "1h"
ZONE_TIMEFRAME = "4h"
DETECTOR_VERSION = "support_structure_v1"
STRATEGY_VERSION = "support_close_v1"
FINGERPRINT_VERSION = "zf1"

ONE_HOUR_MS = 3_600_000
FOUR_HOURS_MS = 14_400_000
ONE_DAY_MS = 86_400_000

POLYGON_CHAIN_ID = 137

POLYGON_USDT_ADDRESS = "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"
POLYGON_PRANA_ADDRESS = "0x928277e774F34272717EADFafC3fd802dAfBD0F5"

SWAP_ROUTER_02_ADDRESSES = frozenset(
    {
        "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
    }
)

USDT_DECIMALS = 6
PRANA_DECIMALS = 9
CANARY_ALLOWANCE_USDT_RAW = 10_000_000
TRADE_AMOUNT_USDT_RAW = 1_000_000

QUOTE_TOKEN_IN_SYMBOL = "USDT"
QUOTE_TOKEN_OUT_SYMBOL = "PRANA"
EXPECTED_USDT_ONCHAIN_SYMBOLS = frozenset({"USDT", "USDT0"})
EXPECTED_PRANA_ONCHAIN_SYMBOL = "PRANA"

# Keep the ABI deliberately small so wallet checks and approvals cannot call unrelated methods.
ERC20_ABI = [
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "owner", "type": "address"},
            {"internalType": "address", "name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "spender", "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]
