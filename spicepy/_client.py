import os
from pathlib import Path
import platform
import threading
from typing import Dict, Union

import certifi
from pyarrow._flight import FlightCallOptions, FlightClient, Ticket  # pylint: disable=E0611
from .prices import PriceCollection
from .models import ModelsCollection

from ._http import HttpRequests
from .error import SpiceAIError
from . import config


def is_macos_arm64() -> bool:
    return (
        platform.platform().lower().startswith("macos")
        and platform.machine() == "arm64"
    )


try:
    from pyarrow import flight
except (ImportError, ModuleNotFoundError) as error:
    if is_macos_arm64():
        raise ImportError(
            "Failed to import pyarrow. Detected Apple M1 system."
            " Installation of pyarrow on Apple M1 systems requires additional steps."
            " See https://docs.spice.ai/sdks/python-sdk#m1-macs."
        ) from error
    raise error from error

DEFAULT_QUERY_TIMEOUT_SECS = 10 * 60


class _Cert:
    def __init__(
        self,
        tls_root_cert,
    ):
        if tls_root_cert is not None:
            tls_root_cert = (
                tls_root_cert
                if isinstance(tls_root_cert, Path)
                else Path(tls_root_cert)
            )
        else:
            tls_root_cert = Path(certifi.where())

        self.tls_root_certs = self.read_cert(tls_root_cert)

    def read_cert(self, tls_root_cert):
        with open(tls_root_cert, "rb") as cert_file:
            return cert_file.read()


class _SpiceFlight:
    def __init__(self, grpc: str, api_key: str, tls_root_certs):
        self._flight_client = flight.connect(grpc, tls_root_certs=tls_root_certs)
        self._api_key = api_key
        self._flight_options = flight.FlightCallOptions()
        self._authenticate()

    def _authenticate(self):
        self.headers = [self._flight_client.authenticate_basic_token("", self._api_key)]
        self._flight_options = flight.FlightCallOptions(
            headers=self.headers, timeout=DEFAULT_QUERY_TIMEOUT_SECS
        )

    def query(self, query: str, **kwargs) -> flight.FlightStreamReader:
        timeout = kwargs.get("timeout", None)

        if timeout is not None:
            if not isinstance(timeout, int) or timeout <= 0:
                raise ValueError("Timeout must be a positive integer")
            self._flight_options = flight.FlightCallOptions(
                headers=self.headers, timeout=timeout
            )

        flight_info = self._flight_client.get_flight_info(
            flight.FlightDescriptor.for_command(query), self._flight_options
        )

        try:
            reader = self._threaded_flight_do_get(
                ticket=flight_info.endpoints[0].ticket
            )
        except flight.FlightUnauthenticatedError:
            self._authenticate()
            reader = self._threaded_flight_do_get(
                ticket=flight_info.endpoints[0].ticket
            )
        except flight.FlightTimedOutError as exc:
            raise TimeoutError(
                f"Query timed out and was canceled after {timeout} seconds."
            ) from exc

        return reader

    def _threaded_flight_do_get(self, ticket: Ticket):
        thread = _ArrowFlightCallThread(
            ticket=ticket,
            flight_options=self._flight_options,
            flight_client=self._flight_client,
        )
        thread.start()
        while thread.is_alive():
            thread.join(1)

        return thread.reader


class Client:
    def __init__(
        self,
        api_key: str,
        flight_url: str = config.DEFAULT_FLIGHT_URL,
        firecache_url: str = config.DEFAULT_FIRECACHE_URL,
        http_url: str = config.DEFAULT_HTTP_URL,
        tls_root_cert: Union[str, Path, None] = None,
    ):  # pylint: disable=R0913
        tls_root_certs = _Cert(tls_root_cert).tls_root_certs
        self._flight = _SpiceFlight(flight_url, api_key, tls_root_certs)
        self._firecache = _SpiceFlight(firecache_url, api_key, tls_root_certs)

        self.api_key = api_key
        self.http = HttpRequests(http_url, self._headers())

    def _headers(self) -> Dict[str, str]:
        return {
            "X-API-Key": self._api_key(),
            "Accept": "application/json",
            "User-Agent": "spicepy 1.0",
        }

    def _api_key(self) -> str:
        key = self.api_key
        if key is None:
            key = os.environ.get("SPICE_API_KEY")
        if not key:
            raise SpiceAIError(
                "No API key provided. You need to set the SPICE_API_KEY environment variable or create a client "
                "with `spicepy.Client('API_KEY')`. You can find your API key on at https://spice.ai."
            )
        return key

    def query(self, query: str, **kwargs) -> flight.FlightStreamReader:
        return self._flight.query(query, **kwargs)

    def fire_query(self, query: str, **kwargs) -> flight.FlightStreamReader:
        return self._firecache.query(query, **kwargs)

    @property
    def models(self) -> ModelsCollection:
        return ModelsCollection(client=self.http)

    @property
    def prices(self) -> PriceCollection:
        return PriceCollection(client=self.http)


class _ArrowFlightCallThread(threading.Thread):
    def __init__(
        self,
        flight_client: FlightClient,
        ticket: Ticket,
        flight_options: FlightCallOptions,
    ):
        super().__init__()
        self._exc = None
        self._flight_client = flight_client
        self._ticket = ticket
        self._flight_options = flight_options
        self.reader = None

    def run(self):
        try:
            self.reader = self._flight_client.do_get(self._ticket, self._flight_options)
        except BaseException as exc:  # pylint: disable=W0718
            self._exc = exc

    def join(self, timeout=None):
        super().join(timeout)
        if self._exc:
            raise self._exc
