#!/usr/bin/env python3
"""Transfer USDC.e from Bottie funder wallet to external address on Polygon."""
import os, sys, json, urllib.request

def main():
    if len(sys.argv) != 3:
        print(json.dumps({"ok": False, "error": "Usage: transfer_usdc.py <to_address> <amount_usdc>"}))
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

    USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    RPC = "https://polygon-bor-rpc.publicnode.com"
    w3 = Web3(Web3.HTTPProvider(RPC))

    if not pk.startswith("0x"):
        pk = "0x" + pk
    account = Account.from_key(pk)
    funder = account.address

    # Check USDC balance first
    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=[
        {"constant": True, "inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
        {"constant": False, "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    ])

    balance = usdc.functions.balanceOf(Web3.to_checksum_address(funder)).call()
    balance_usdc = balance / 1_000_000
    amount_raw = int(amount_usdc * 1_000_000)

    if amount_raw > balance:
        print(json.dumps({"ok": False, "error": f"Insufficient balance: ${balance_usdc:.2f} < ${amount_usdc:.2f}"}))
        sys.exit(1)

    # Build and send transfer
    to_checksum = Web3.to_checksum_address(to_addr)
    nonce = w3.eth.get_transaction_count(Web3.to_checksum_address(funder))
    gas_price = w3.eth.gas_price

    tx = usdc.functions.transfer(to_checksum, amount_raw).build_transaction({
        "from": Web3.to_checksum_address(funder),
        "nonce": nonce,
        "gas": 100_000,
        "gasPrice": gas_price,
        "chainId": 137,
    })

    signed = account.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw)
    tx_hex = tx_hash.hex()

    print(json.dumps({"ok": True, "tx_hash": tx_hex, "amount": amount_usdc, "to": to_addr, "from": funder}))

if __name__ == "__main__":
    main()
