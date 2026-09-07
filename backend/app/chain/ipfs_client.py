"""IPFS pinning via Pinata with retry on transient errors."""

import json
import logging
import os
from pathlib import Path
from typing import Any

import requests

from backend.app.utils.retry import retry_on_network_error

log = logging.getLogger("ipfs")
PINATA_PIN_URL = "https://api.pinata.cloud/pinning/pinJSONToIPFS"


def _get_pinata_auth() -> tuple[str, str]:
    """Return (api_key, secret_api_key) from environment."""
    api_key = os.environ.get("PINATA_API_KEY", "").strip()
    secret = os.environ.get("PINATA_SECRET_API_KEY", "").strip()
    if not api_key or not secret:
        raise EnvironmentError(
            "PINATA_API_KEY and PINATA_SECRET_API_KEY must be set. "
            "Copy .env.example to .env and fill in your keys."
        )
    return api_key, secret


class PinataAPIError(Exception):
    """Raised when Pinata returns a non-200 response."""

    def __init__(self, status_code: int, response_body: str):
        self.status_code = status_code
        self.response_body = response_body
        self.message = f"Pinata API error {status_code}: {response_body}"
        super().__init__(self.message)


PINBYTES_URL = "https://api.pinata.cloud/pinning/pinFileToIPFS"


class PinataClient:
    """Client for the Pinata IPFS pinning REST API.

    Requires ``PINATA_API_KEY`` and ``PINATA_SECRET_API_KEY`` in the environment.
    Retries transient HTTP errors with exponential backoff (max 3 attempts).
    """

    def __init__(self, pin_url: str = PINATA_PIN_URL, pinbytes_url: str = PINBYTES_URL):
        self.pin_url = pin_url
        self.pinbytes_url = pinbytes_url
        self._api_key, self._secret = _get_pinata_auth()

    @retry_on_network_error(max_attempts=3, base_delay=1.0, max_delay=15.0)
    def pin_bytes(
        self,
        data: bytes,
        filename: str = "record.json",
        pinata_metadata_name: str | None = None,
    ) -> str:
        """Pin raw bytes to IPFS via Pinata's pinFileToIPFS endpoint.

        This is the preferred pinning method because the caller controls the exact
        byte sequence, guaranteeing that sha256(data) matches what IPFS stores.

        Retries on HTTP 429, 5xx, and connection errors up to 3 times with
        exponential backoff.

        Parameters
        ----------
        data : bytes
            The exact bytes to pin. Callers must compute sha256(data) *before*
            calling this so the hash covers the identical bytes Pinata stores.
        filename : str
            Filename stored in Pinata's metadata (default: ``record.json``).
        pinata_metadata_name : str, optional
            Optional name stored in Pinata's metadata.

        Returns
        -------
        str
            The IPFS CID of the pinned content.

        Raises
        ------
        PinataAPIError
            If Pinata returns a non-200 response after all retries are exhausted.
        """
        headers = {
            "pinata_api_key": self._api_key,
            "pinata_secret_api_key": self._secret,
        }

        metadata_name = pinata_metadata_name or filename
        try:
            response = requests.post(
                self.pinbytes_url,
                files={"file": (filename, data, "application/json")},
                data={
                    "pinataMetadata": json.dumps({"name": metadata_name}),
                    "pinataOptions": json.dumps({"cidVersion": 1}),
                },
                headers=headers,
                timeout=30,
            )
        except requests.RequestException as exc:
            # Let the retry decorator catch it
            raise

        if response.status_code != 200:
            raise PinataAPIError(
                status_code=response.status_code,
                response_body=response.text,
            )

        result_data = response.json()
        cid = result_data.get("IpfsHash", "")
        if not cid:
            raise PinataAPIError(
                status_code=response.status_code,
                response_body=f"No IpfsHash in response: {response.text}",
            )

        log.info("Pinned %d bytes to Pinata → CID=%s", len(data), cid)
        return cid

    @retry_on_network_error(max_attempts=3, base_delay=1.0, max_delay=15.0)
    def pin_json(
        self,
        record_dict: dict[str, Any],
        pinata_metadata_name: str | None = None,
    ) -> str:
        """Pin a JSON object to IPFS via Pinata's pinJSONToIPFS endpoint.

        .. deprecated::
            Use :meth:`pin_bytes` instead. ``pinJSONToIPFS`` re-serialises the
            dict on Pinata's server, which can produce different UTF-8 bytes than
            the caller's encoder (e.g. Go encodes ``0.0`` as ``0``). This causes
            the sha256 of the stored bytes to diverge from sha256 of the bytes the
            caller hashed, breaking the chain↔IPFS integrity check.

        Parameters
        ----------
        record_dict : dict
            The canonical match record JSON to pin.
        pinata_metadata_name : str, optional
            Optional name stored in Pinata's metadata.

        Returns
        -------
        str
            The IPFS CID of the pinned content.

        Raises
        ------
        PinataAPIError
            If Pinata returns a non-200 response after all retries are exhausted.
        """
        headers = {
            "pinata_api_key": self._api_key,
            "pinata_secret_api_key": self._secret,
        }

        payload = {
            "pinataContent": record_dict,
            "pinataMetadata": {
                "name": pinata_metadata_name
                or record_dict.get("timestamp", "faceid-record"),
            },
            "pinataOptions": {
                "cidVersion": 1,
            },
        }

        try:
            response = requests.post(
                self.pin_url,
                json=payload,
                headers=headers,
                timeout=30,
            )
        except requests.RequestException as exc:
            # Let the retry decorator catch it
            raise

        if response.status_code != 200:
            raise PinataAPIError(
                status_code=response.status_code,
                response_body=response.text,
            )

        data = response.json()
        cid = data.get("IpfsHash", "")
        if not cid:
            raise PinataAPIError(
                status_code=response.status_code,
                response_body=f"No IpfsHash in response: {response.text}",
            )

        return cid
