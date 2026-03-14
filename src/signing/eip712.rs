use alloy_primitives::{keccak256, Address, B256, U256};
use alloy_signer::Signer;
use alloy_signer_local::PrivateKeySigner;

use crate::config::{CHAIN_ID, EXCHANGE_ADDRESS, NEG_RISK_EXCHANGE_ADDRESS};

const ORDER_DOMAIN_NAME: &str = "Polymarket CTF Exchange";
const ORDER_DOMAIN_VERSION: &str = "1";
const AUTH_DOMAIN_NAME: &str = "ClobAuthDomain";
const AUTH_DOMAIN_VERSION: &str = "1";
const CLOB_AUTH_MESSAGE: &str = "This message attests that I control the given wallet";

fn order_type_hash() -> B256 {
    keccak256(
        "Order(uint256 salt,address maker,address signer,address taker,uint256 tokenId,\
         uint256 makerAmount,uint256 takerAmount,uint256 expiration,uint256 nonce,\
         uint256 feeRateBps,uint8 side,uint8 signatureType)"
            .as_bytes(),
    )
}

fn clob_auth_type_hash() -> B256 {
    keccak256(
        "ClobAuth(address address,string timestamp,uint256 nonce,string message)".as_bytes(),
    )
}

fn eip712_domain_type_hash() -> B256 {
    keccak256(
        "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
            .as_bytes(),
    )
}

fn eip712_domain_type_hash_no_contract() -> B256 {
    keccak256("EIP712Domain(string name,string version,uint256 chainId)".as_bytes())
}

fn encode_u256(val: U256) -> [u8; 32] {
    val.to_be_bytes()
}

fn encode_address(addr: Address) -> [u8; 32] {
    let mut buf = [0u8; 32];
    buf[12..].copy_from_slice(addr.as_slice());
    buf
}

pub fn order_domain_separator(neg_risk: bool) -> B256 {
    let exchange = if neg_risk {
        NEG_RISK_EXCHANGE_ADDRESS
    } else {
        EXCHANGE_ADDRESS
    };
    let exchange_addr: Address = exchange.parse().expect("invalid exchange address");

    let mut data = Vec::with_capacity(5 * 32);
    data.extend_from_slice(eip712_domain_type_hash().as_slice());
    data.extend_from_slice(keccak256(ORDER_DOMAIN_NAME.as_bytes()).as_slice());
    data.extend_from_slice(keccak256(ORDER_DOMAIN_VERSION.as_bytes()).as_slice());
    data.extend_from_slice(&encode_u256(U256::from(CHAIN_ID)));
    data.extend_from_slice(&encode_address(exchange_addr));
    keccak256(&data)
}

pub fn auth_domain_separator() -> B256 {
    let mut data = Vec::with_capacity(4 * 32);
    data.extend_from_slice(eip712_domain_type_hash_no_contract().as_slice());
    data.extend_from_slice(keccak256(AUTH_DOMAIN_NAME.as_bytes()).as_slice());
    data.extend_from_slice(keccak256(AUTH_DOMAIN_VERSION.as_bytes()).as_slice());
    data.extend_from_slice(&encode_u256(U256::from(CHAIN_ID)));
    keccak256(&data)
}

#[derive(Debug, Clone)]
pub struct OrderData {
    pub salt: U256,
    pub maker: Address,
    pub signer: Address,
    pub taker: Address,
    pub token_id: U256,
    pub maker_amount: U256,
    pub taker_amount: U256,
    pub expiration: U256,
    pub nonce: U256,
    pub fee_rate_bps: U256,
    pub side: u8,
    pub signature_type: u8,
}

impl OrderData {
    pub fn struct_hash(&self) -> B256 {
        let mut data = Vec::with_capacity(13 * 32);
        data.extend_from_slice(order_type_hash().as_slice());
        data.extend_from_slice(&encode_u256(self.salt));
        data.extend_from_slice(&encode_address(self.maker));
        data.extend_from_slice(&encode_address(self.signer));
        data.extend_from_slice(&encode_address(self.taker));
        data.extend_from_slice(&encode_u256(self.token_id));
        data.extend_from_slice(&encode_u256(self.maker_amount));
        data.extend_from_slice(&encode_u256(self.taker_amount));
        data.extend_from_slice(&encode_u256(self.expiration));
        data.extend_from_slice(&encode_u256(self.nonce));
        data.extend_from_slice(&encode_u256(self.fee_rate_bps));
        data.extend_from_slice(&encode_u256(U256::from(self.side)));
        data.extend_from_slice(&encode_u256(U256::from(self.signature_type)));
        keccak256(&data)
    }
}

pub fn order_message_hash(order: &OrderData, neg_risk: bool) -> B256 {
    let domain = order_domain_separator(neg_risk);
    let struct_hash = order.struct_hash();

    let mut signable = Vec::with_capacity(66);
    signable.push(0x19);
    signable.push(0x01);
    signable.extend_from_slice(domain.as_slice());
    signable.extend_from_slice(struct_hash.as_slice());
    keccak256(&signable)
}

pub async fn sign_order(
    signer: &PrivateKeySigner,
    order: &OrderData,
    neg_risk: bool,
) -> anyhow::Result<String> {
    let msg_hash = order_message_hash(order, neg_risk);
    let sig = signer.sign_hash(&msg_hash).await?;
    Ok(format!("0x{}", hex::encode(sig.as_bytes())))
}

pub struct ClobAuthData {
    pub address: Address,
    pub timestamp: String,
    pub nonce: U256,
}

impl ClobAuthData {
    pub fn struct_hash(&self) -> B256 {
        let mut data = Vec::with_capacity(5 * 32);
        data.extend_from_slice(clob_auth_type_hash().as_slice());
        data.extend_from_slice(&encode_address(self.address));
        data.extend_from_slice(keccak256(self.timestamp.as_bytes()).as_slice());
        data.extend_from_slice(&encode_u256(self.nonce));
        data.extend_from_slice(keccak256(CLOB_AUTH_MESSAGE.as_bytes()).as_slice());
        keccak256(&data)
    }
}

pub fn auth_message_hash(auth: &ClobAuthData) -> B256 {
    let domain = auth_domain_separator();
    let struct_hash = auth.struct_hash();

    let mut signable = Vec::with_capacity(66);
    signable.push(0x19);
    signable.push(0x01);
    signable.extend_from_slice(domain.as_slice());
    signable.extend_from_slice(struct_hash.as_slice());
    keccak256(&signable)
}

pub async fn sign_clob_auth(
    signer: &PrivateKeySigner,
    auth: &ClobAuthData,
) -> anyhow::Result<String> {
    let msg_hash = auth_message_hash(auth);
    let sig = signer.sign_hash(&msg_hash).await?;
    Ok(format!("0x{}", hex::encode(sig.as_bytes())))
}
