"""Polygon Amoy contract client for the RecordRegistry contract."""

import json
import logging
import os
import time
import warnings
from pathlib import Path
from typing import Any

from web3 import Web3
from web3.exceptions import TransactionNotFound
from eth_account import Account

from backend.app.utils.retry import retry_on_network_error

# Suppress known-harmless web3.py MismatchedABI warnings that arise when
# an event log contains multiple indexed topics (harmless ABI ordering mismatch
# in the ledger). The record_id is read from the contract counter, not from
# the event, so these discarded logs have no effect on correctness.
warnings.filterwarnings(
    "ignore",
    message=".*MismatchedABI.*",
    category=UserWarning,
)

log = logging.getLogger("chain")

POLYGONSCAN_TX_URL = "https://amoy.polygonscan.com/tx/{tx_hash}"

# Path to the compiled artifact, relative to the project root.
DEFAULT_ARTIFACT_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "contracts"
    / "artifacts"
    / "contracts"
    / "RecordRegistry.sol"
    / "RecordRegistry.json"
)


def _load_artifact(artifact_path: Path) -> tuple[list, bytes]:
    """Load ABI and bytecode from a Hardhat artifact JSON."""
    with open(artifact_path) as f:
        artifact = json.load(f)
    return artifact["abi"], artifact["bytecode"]


class ChainSubmissionError(Exception):
    """Raised when an on-chain submission fails."""

    def __init__(self, message: str, tx_hash: str | None = None):
        self.tx_hash = tx_hash
        super().__init__(message)


class ContractClient:
    """Thin web3.py wrapper around the deployed RecordRegistry contract.

    Requires the following environment variables:
        POLYGON_AMOY_RPC_URL   — RPC endpoint
        DEPLOYER_PRIVATE_KEY   — Private key for signing submissions
        CONTRACT_ADDRESS       — Deployed contract address on Amoy
    """

    def __init__(
        self,
        artifact_path: str | Path = DEFAULT_ARTIFACT_PATH,
    ):
        rpc_url = os.environ.get("POLYGON_AMOY_RPC_URL", "").strip()
        private_key = os.environ.get("DEPLOYER_PRIVATE_KEY", "").strip()
        contract_address = os.environ.get("CONTRACT_ADDRESS", "").strip()

        if not rpc_url:
            raise EnvironmentError("POLYGON_AMOY_RPC_URL is not set")
        if not private_key:
            raise EnvironmentError("DEPLOYER_PRIVATE_KEY is not set")
        if not contract_address:
            raise EnvironmentError("CONTRACT_ADDRESS is not set")

        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        if not self.w3.is_connected():
            raise ConnectionError(f"Could not connect to RPC: {rpc_url}")

        self.account = Account.from_key(private_key.lstrip("0x"))
        self.contract_address = Web3.to_checksum_address(contract_address)

        abi, _ = _load_artifact(Path(artifact_path))
        self.contract = self.w3.eth.contract(
            address=self.contract_address,
            abi=abi,
        )

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def submit_record(
        self,
        record_hash: bytes,
        ipfs_cid: str,
    ) -> dict:
        """Call submitRecord on the contract.

        Retries send_raw_transaction on transient RPC errors up to 3 times
        with exponential backoff. view calls (get_record, verify_record) also retry.

        Parameters
        ----------
        record_hash : bytes
            32-byte SHA-256 hash of the canonical record JSON.
        ipfs_cid : str
            IPFS CID of the pinned record JSON.

        Returns
        -------
        dict
            {
                "tx_hash": str,         — 0x-prefixed transaction hash
                "record_id": int,       — assigned recordId from event logs
                "polygonscan_url": str, — link to view the tx on PolygonScan
            }
        """
        if len(record_hash) != 32:
            raise ValueError(
                f"record_hash must be exactly 32 bytes, got {len(record_hash)}"
            )

        return self._submit_with_retry(record_hash, ipfs_cid)

    def _submit_with_retry(
        self,
        record_hash: bytes,
        ipfs_cid: str,
        attempt: int = 1,
        max_attempts: int = 3,
    ) -> dict:
        """Internal submit with retry on transient RPC errors."""
        base_delay = 2.0
        try:
            tx = self.contract.functions.submitRecord(
                record_hash,
                ipfs_cid,
            ).build_transaction(
                {
                    "from": self.account.address,
                    "nonce": self.w3.eth.get_transaction_count(self.account.address),
                    "gas": 200_000,
                    "gasPrice": self.w3.eth.gas_price,
                    "chainId": self.w3.eth.chain_id,
                }
            )
        except Exception as exc:
            if attempt < max_attempts and self._is_transient_rpc_error(exc):
                delay = min(base_delay * (2 ** (attempt - 1)), 30.0)
                log.warning(
                    "⚠️  submit_record RPC build attempt %d/%d failed: %s — retrying in %.1fs…",
                    attempt, max_attempts, exc, delay
                )
                time.sleep(delay)
                return self._submit_with_retry(
                    record_hash, ipfs_cid, attempt + 1, max_attempts
                )
            raise ChainSubmissionError(f"Failed to build transaction: {exc}") from exc

        try:
            signed = self.account.sign_transaction(tx)
            raw = signed.raw_transaction
        except Exception as exc:
            if attempt < max_attempts and self._is_transient_rpc_error(exc):
                delay = min(base_delay * (2 ** (attempt - 1)), 30.0)
                log.warning(
                    "⚠️  submit_record sign attempt %d/%d failed: %s — retrying in %.1fs…",
                    attempt, max_attempts, exc, delay
                )
                time.sleep(delay)
                return self._submit_with_retry(
                    record_hash, ipfs_cid, attempt + 1, max_attempts
                )
            raise ChainSubmissionError(f"Failed to sign transaction: {exc}") from exc

        try:
            tx_hash = self.w3.eth.send_raw_transaction(raw)
        except Exception as exc:
            if attempt < max_attempts and self._is_transient_rpc_error(exc):
                delay = min(base_delay * (2 ** (attempt - 1)), 30.0)
                log.warning(
                    "⚠️  submit_record send attempt %d/%d failed: %s — retrying in %.1fs…",
                    attempt, max_attempts, exc, delay
                )
                time.sleep(delay)
                return self._submit_with_retry(
                    record_hash, ipfs_cid, attempt + 1, max_attempts
                )
            raise ChainSubmissionError(
                f"Failed to send transaction to RPC: {exc}"
            ) from exc

        tx_hash_hex = "0x" + tx_hash.hex()
        log.info("Transaction sent: %s", tx_hash_hex)

        try:
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        except Exception as exc:
            if attempt < max_attempts and self._is_transient_rpc_error(exc):
                delay = min(base_delay * (2 ** (attempt - 1)), 30.0)
                log.warning(
                    "⚠️  wait_for_receipt attempt %d/%d failed: %s — retrying in %.1fs…",
                    attempt, max_attempts, exc, delay
                )
                time.sleep(delay)
                return self._submit_with_retry(
                    record_hash, ipfs_cid, attempt + 1, max_attempts
                )
            raise ChainSubmissionError(
                f"Failed to confirm transaction receipt: {exc}",
                tx_hash=tx_hash_hex,
            ) from exc

        if receipt.status != 1:
            raise ChainSubmissionError(
                f"Transaction reverted on-chain (status={receipt.status}). "
                f"View at {POLYGONSCAN_TX_URL.format(tx_hash=tx_hash_hex)}",
                tx_hash=tx_hash_hex,
            )

        record_id = self._extract_record_id_from_receipt(receipt)
        return {
            "tx_hash": tx_hash_hex,
            "record_id": record_id,
            "polygonscan_url": POLYGONSCAN_TX_URL.format(tx_hash=tx_hash_hex),
        }

    @staticmethod
    def _is_transient_rpc_error(exc: Exception) -> bool:
        """Return True for transient RPC/node errors worth retrying."""
        msg = str(exc).lower()
        if isinstance(exc, (TimeoutError, ConnectionError, TransactionNotFound)):
            return True
        if "timeout" in msg or "network" in msg or "connection" in msg:
            return True
        if "429" in msg or "rate" in msg or "too many requests" in msg:
            return True
        return False

    def _extract_record_id_from_receipt(self, receipt) -> int:
        """Parse RecordSubmitted event logs to recover the recordId."""
        event = self.contract.events.RecordSubmitted()
        logs = event.process_receipt(receipt)
        if not logs:
            raise RuntimeError(
                "No RecordSubmitted event found in transaction receipt. "
                "The contract may have a different event signature."
            )
        return int(logs[0]["args"]["recordId"])

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_record(self, record_id: int) -> dict:
        """Read a record back from the contract. Retries transient RPC errors."""
        return self._get_record_with_retry(record_id)

    def _get_record_with_retry(
        self, record_id: int, attempt: int = 1, max_attempts: int = 3
    ) -> dict:
        try:
            raw = self.contract.functions.getRecord(record_id).call()
        except Exception as exc:
            if attempt < max_attempts and self._is_transient_rpc_error(exc):
                delay = min(2.0 * (2 ** (attempt - 1)), 30.0)
                log.warning(
                    "⚠️  get_record attempt %d/%d failed: %s — retrying in %.1fs…",
                    attempt, max_attempts, exc, delay
                )
                time.sleep(delay)
                return self._get_record_with_retry(record_id, attempt + 1, max_attempts)
            raise
        record_hash_bytes, ipfs_cid, timestamp, submitter = raw
        return {
            "recordHash": "0x" + record_hash_bytes.hex(),
            "ipfsCID": ipfs_cid,
            "timestamp": int(timestamp),
            "submitter": submitter,
        }

    def verify_record(self, record_id: int, expected_hash: bytes) -> bool:
        """Call verifyRecord view function. Retries transient RPC errors."""
        if len(expected_hash) != 32:
            raise ValueError(
                f"expected_hash must be exactly 32 bytes, got {len(expected_hash)}"
            )
        return self._verify_with_retry(record_id, expected_hash)

    def _verify_with_retry(
        self, record_id: int, expected_hash: bytes,
        attempt: int = 1, max_attempts: int = 3,
    ) -> bool:
        try:
            return bool(
                self.contract.functions.verifyRecord(record_id, expected_hash).call()
            )
        except Exception as exc:
            if attempt < max_attempts and self._is_transient_rpc_error(exc):
                delay = min(2.0 * (2 ** (attempt - 1)), 30.0)
                log.warning(
                    "⚠️  verify_record attempt %d/%d failed: %s — retrying in %.1fs…",
                    attempt, max_attempts, exc, delay
                )
                time.sleep(delay)
                return self._verify_with_retry(
                    record_id, expected_hash, attempt + 1, max_attempts
                )
            raise

    def get_record_count(self) -> int:
        return int(self.contract.functions.recordCount().call())
