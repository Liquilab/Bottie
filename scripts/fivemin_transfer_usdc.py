#!/usr/bin/env python3
"""Withdraw USDC from GIYN Gnosis Safe via Polymarket bridge relay.

Adapted from /opt/bottie/scripts/transfer_usdc.py — same flow, different Safe.

Flow:
1. Call PM bridge API → get relay deposit address
2. Send USDC.e from Safe to relay deposit address via execTransaction
3. Relay converts USDC.e → native USDC and delivers to recipient
"""
import os, sys, json, urllib.request

USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_NATIVE_POLYGON = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
SAFE_ADDR = "0x8A3A19AeC04eeB6E3C183ee5750D06fe5c08066a"  # GIYN funder (bottie-test)
RPC = "https://polygon-bor-rpc.publicnode.com"
CHAIN_ID = 137
BRIDGE_URL = "https://bridge.polymarket.com/withdraw"


def get_relay_deposit_address(recipient_addr):
    payload = json.dumps({
        "address": SAFE_ADDR,
        "toChainId": str(CHAIN_ID),
        "toTokenAddress": USDC_NATIVE_POLYGON,
        "recipientAddr": recipient_addr,
    }).encode()

    req = urllib.request.Request(BRIDGE_URL, data=payload, headers={
        "Content-Type": "application/json",
        "User-Agent": "Bottie-Fivemin-Skim/1.0",
    })
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())

    evm_addr = resp.get("address", {}).get("evm")
    if not evm_addr:
        raise ValueError(f"Bridge API returned no EVM deposit address: {resp}")
    return evm_addr


def send_usdc_from_safe(w3, account, deposit_addr, amount_raw):
    from web3 import Web3

    safe = Web3.to_checksum_address(SAFE_ADDR)
    owner = account.address
    ZERO = Web3.to_checksum_address("0x0000000000000000000000000000000000000000")

    transfer_selector = w3.keccak(text="transfer(address,uint256)")[:4]
    to_checksum = Web3.to_checksum_address(deposit_addr)
    inner_data = (transfer_selector
                  + bytes.fromhex(to_checksum[2:].lower().rjust(64, "0"))
                  + amount_raw.to_bytes(32, "big"))

    safe_abi = [
        {"constant": True, "inputs": [], "name": "nonce", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
        {"constant": True, "inputs": [], "name": "domainSeparator", "outputs": [{"name": "", "type": "bytes32"}], "type": "function"},
        {"constant": False, "inputs": [
            {"name": "to", "type": "address"}, {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"}, {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"}, {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"}, {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"}, {"name": "signatures", "type": "bytes"},
        ], "name": "execTransaction", "outputs": [{"name": "success", "type": "bool"}], "type": "function"},
    ]
    safe_contract = w3.eth.contract(address=safe, abi=safe_abi)

    safe_nonce = safe_contract.functions.nonce().call()
    domain_separator = safe_contract.functions.domainSeparator().call()

    SAFE_TX_TYPEHASH = w3.keccak(text="SafeTx(address to,uint256 value,bytes data,uint8 operation,uint256 safeTxGas,uint256 baseGas,uint256 gasPrice,address gasToken,address refundReceiver,uint256 nonce)")

    usdc_addr = Web3.to_checksum_address(USDC_E)
    encoded = w3.codec.encode(
        ["bytes32", "address", "uint256", "bytes32", "uint8", "uint256", "uint256", "uint256", "address", "address", "uint256"],
        [SAFE_TX_TYPEHASH, usdc_addr, 0, w3.keccak(inner_data), 0, 0, 0, 0, ZERO, ZERO, safe_nonce]
    )
    safe_tx_hash = w3.keccak(encoded)
    final_hash = w3.keccak(b"\x19\x01" + domain_separator + safe_tx_hash)

    sign_fn = getattr(account, "unsafe_sign_hash", None) or getattr(account, "signHash")
    sig = sign_fn(final_hash)
    signature = sig.r.to_bytes(32, "big") + sig.s.to_bytes(32, "big") + sig.v.to_bytes(1, "big")

    tx = safe_contract.functions.execTransaction(
        usdc_addr, 0, inner_data, 0, 0, 0, 0, ZERO, ZERO, signature,
    ).build_transaction({
        "from": Web3.to_checksum_address(owner),
        "nonce": w3.eth.get_transaction_count(Web3.to_checksum_address(owner)),
        "gas": 200_000,
        "gasPrice": w3.eth.gas_price,
        "chainId": CHAIN_ID,
    })

    signed_tx = account.sign_transaction(tx)
    raw = getattr(signed_tx, "raw_transaction", None) or signed_tx.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw)
    return tx_hash.hex()


def main():
    if len(sys.argv) != 3:
        print(json.dumps({"ok": False, "error": "Usage: fivemin_transfer_usdc.py <to_address> <amount_usdc>"}))
        sys.exit(1)

    to_addr = sys.argv[1]
    amount_usdc = float(sys.argv[2])

    if amount_usdc <= 0 or amount_usdc > 10000:
        print(json.dumps({"ok": False, "error": f"Invalid amount: {amount_usdc}"}))
        sys.exit(1)

    pk = os.environ.get("PRIVATE_KEY", "")
    if not pk:
        print(json.dumps({"ok": False, "error": "PRIVATE_KEY not set"}))
        sys.exit(1)

    try:
        from web3 import Web3
        from eth_account import Account
    except ImportError:
        print(json.dumps({"ok": False, "error": "web3/eth_account not installed"}))
        sys.exit(1)

    w3 = Web3(Web3.HTTPProvider(RPC))

    if not pk.startswith("0x"):
        pk = "0x" + pk
    account = Account.from_key(pk)
    safe = Web3.to_checksum_address(SAFE_ADDR)

    usdc_abi = [{"constant": True, "inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"}]
    usdc_contract = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=usdc_abi)
    balance = usdc_contract.functions.balanceOf(safe).call()
    balance_usdc = balance / 1_000_000
    amount_raw = int(amount_usdc * 1_000_000)

    if amount_raw > balance:
        print(json.dumps({"ok": False, "error": f"Insufficient balance: ${balance_usdc:.2f} < ${amount_usdc:.2f}"}))
        sys.exit(1)

    try:
        deposit_addr = get_relay_deposit_address(to_addr)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"Bridge API failed: {e}"}))
        sys.exit(1)

    try:
        tx_hex = send_usdc_from_safe(w3, account, deposit_addr, amount_raw)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"Safe transaction failed: {e}"}))
        sys.exit(1)

    print(json.dumps({
        "ok": True,
        "tx_hash": tx_hex,
        "amount": amount_usdc,
        "to": to_addr,
        "relay_deposit": deposit_addr,
        "from": SAFE_ADDR,
    }))


if __name__ == "__main__":
    main()
