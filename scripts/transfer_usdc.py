#!/usr/bin/env python3
"""Transfer USDC.e from Bottie Gnosis Safe (funder) to external address on Polygon.

The funder address is a Gnosis Safe v1.3.0 proxy. The PRIVATE_KEY controls
the single owner (wallet). We sign a Safe execTransaction to transfer USDC.
"""
import os, sys, json

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
        from eth_account.messages import defunct_hash_message
    except ImportError:
        print(json.dumps({"ok": False, "error": "web3/eth_account not installed"}))
        sys.exit(1)

    USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    SAFE_ADDR = "0x89dcA91b49AfB7bEfb953a7658a1B83fC7Ab8F42"  # funder = Gnosis Safe
    RPC = "https://polygon-bor-rpc.publicnode.com"
    CHAIN_ID = 137

    w3 = Web3(Web3.HTTPProvider(RPC))

    if not pk.startswith("0x"):
        pk = "0x" + pk
    account = Account.from_key(pk)
    owner = account.address  # 0x8FE9... = Safe owner

    safe = Web3.to_checksum_address(SAFE_ADDR)

    # Check USDC balance on the Safe
    usdc_abi = [
        {"constant": True, "inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    ]
    usdc_contract = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=usdc_abi)
    balance = usdc_contract.functions.balanceOf(safe).call()
    balance_usdc = balance / 1_000_000
    amount_raw = int(amount_usdc * 1_000_000)

    if amount_raw > balance:
        print(json.dumps({"ok": False, "error": f"Insufficient balance: ${balance_usdc:.2f} < ${amount_usdc:.2f}"}))
        sys.exit(1)

    # Build the inner USDC transfer calldata: transfer(to, amount)
    transfer_selector = w3.keccak(text="transfer(address,uint256)")[:4]
    to_checksum = Web3.to_checksum_address(to_addr)
    inner_data = transfer_selector + \
        bytes.fromhex(to_checksum[2:].lower().rjust(64, "0")) + \
        amount_raw.to_bytes(32, "big")

    # Get Safe nonce
    safe_abi = [
        {"constant": True, "inputs": [], "name": "nonce", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
        {"constant": True, "inputs": [], "name": "domainSeparator", "outputs": [{"name": "", "type": "bytes32"}], "type": "function"},
        {"constant": False, "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
            {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"},
            {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"},
            {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "signatures", "type": "bytes"},
        ], "name": "execTransaction", "outputs": [{"name": "success", "type": "bool"}], "type": "function"},
    ]
    safe_contract = w3.eth.contract(address=safe, abi=safe_abi)

    safe_nonce = safe_contract.functions.nonce().call()
    domain_separator = safe_contract.functions.domainSeparator().call()

    # Compute Safe transaction hash (EIP-712)
    SAFE_TX_TYPEHASH = w3.keccak(text="SafeTx(address to,uint256 value,bytes data,uint8 operation,uint256 safeTxGas,uint256 baseGas,uint256 gasPrice,address gasToken,address refundReceiver,uint256 nonce)")

    # execTransaction params
    tx_to = Web3.to_checksum_address(USDC_E)
    tx_value = 0
    tx_data = inner_data
    tx_operation = 0  # CALL
    tx_safe_gas = 0
    tx_base_gas = 0
    tx_gas_price = 0
    tx_gas_token = "0x0000000000000000000000000000000000000000"
    tx_refund_receiver = "0x0000000000000000000000000000000000000000"

    # Encode the struct hash
    encoded = w3.codec.encode(
        ["bytes32", "address", "uint256", "bytes32", "uint8", "uint256", "uint256", "uint256", "address", "address", "uint256"],
        [SAFE_TX_TYPEHASH, tx_to, tx_value, w3.keccak(tx_data), tx_operation, tx_safe_gas, tx_base_gas, tx_gas_price,
         Web3.to_checksum_address(tx_gas_token), Web3.to_checksum_address(tx_refund_receiver), safe_nonce]
    )
    safe_tx_hash = w3.keccak(encoded)

    # EIP-712 final hash
    final_hash = w3.keccak(b"\x19\x01" + domain_separator + safe_tx_hash)

    # Sign with owner key (web3 v7 = unsafe_sign_hash, v5 = signHash)
    sign_fn = getattr(account, "unsafe_sign_hash", None) or getattr(account, "signHash")
    sig = sign_fn(final_hash)
    # Pack signature: r (32) + s (32) + v (1)
    signature = sig.r.to_bytes(32, "big") + sig.s.to_bytes(32, "big") + sig.v.to_bytes(1, "big")

    # Send execTransaction via the owner EOA
    tx = safe_contract.functions.execTransaction(
        Web3.to_checksum_address(USDC_E),
        tx_value,
        tx_data,
        tx_operation,
        tx_safe_gas,
        tx_base_gas,
        tx_gas_price,
        Web3.to_checksum_address(tx_gas_token),
        Web3.to_checksum_address(tx_refund_receiver),
        signature,
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
    tx_hex = tx_hash.hex()

    print(json.dumps({"ok": True, "tx_hash": tx_hex, "amount": amount_usdc, "to": to_addr, "from": SAFE_ADDR}))

if __name__ == "__main__":
    main()
