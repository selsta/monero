#!/usr/bin/env python3
# Copyright (c) 2026, The Monero Project
# SPDX-License-Identifier: BSD-3-Clause

"""Exercise file persistence with real daemons on Windows, macOS, and Linux.

Run separately from functional_tests_rpc.py; this starts its own offline services
and needs only Python's standard library. Wallets and chain data are temporary,
while service logs remain in --log-dir for CI artifacts.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import ssl
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Service:
    def __init__(self, command, url, log_file):
        self.command = [str(arg) for arg in command]
        self.url = url
        self.log_file = log_file
        # These are newly generated self-signed certificates on loopback only.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        self.http = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
        )

    def __enter__(self):
        self.output = self.log_file.open("wb")
        try:
            self.process = subprocess.Popen(
                self.command, stdin=subprocess.DEVNULL,
                stdout=self.output, stderr=subprocess.STDOUT,
            )
        except BaseException:
            self.output.close()
            raise
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=15)
        finally:
            self.output.close()

    def request(self, path, params=None, timeout=60):
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(params or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with self.http.open(request, timeout=timeout) as response:
            result = json.load(response)
        require("error" not in result, "RPC error: " + repr(result))
        require(result.get("status", "OK") == "OK", "RPC failed: " + repr(result))
        return result

    def rpc(self, method, params=None, timeout=60):
        return self.request("/json_rpc", {
            "jsonrpc": "2.0", "id": "file-io-regression", "method": method,
            "params": params or {},
        }, timeout)["result"]

    def ready(self, probe):
        deadline = time.monotonic() + 90
        last_error = None
        while time.monotonic() < deadline:
            require(self.process.poll() is None,
                    "Service exited during startup; see " + str(self.log_file))
            try:
                probe()
                return
            except (OSError, urllib.error.URLError) as error:
                last_error = error
                time.sleep(0.2)
        raise RuntimeError("Service startup timed out: {}; see {}".format(last_error, self.log_file))

    def stopped(self):
        code = self.process.wait(timeout=60)
        require(code == 0, "Service exited with {}; see {}".format(code, self.log_file))


def test_wallet(binary, root, logs):
    wallets = root / "wallets"
    wallets.mkdir()
    port = free_port()
    filename = "cache-regression"
    credentials = {"filename": filename, "password": "temporary-ci-password"}
    cache = wallets / filename
    address = None
    value = ("wallet cache metadata: \u00e9\u65e5\u672c " * 100000) + "end"
    for run in range(2):
        command = [binary, "--offline", "--wallet-dir", wallets,
                   "--rpc-bind-ip", "127.0.0.1", "--rpc-bind-port", str(port),
                   "--disable-rpc-login", "--rpc-ssl", "disabled",
                   "--log-file", logs / ("wallet-{}.log".format(run))]
        with Service(command, "http://127.0.0.1:" + str(port),
                     logs / ("wallet-{}-console.log".format(run))) as wallet:
            wallet.ready(lambda: wallet.rpc("get_version", timeout=2))
            if run == 0:
                wallet.rpc("create_wallet", dict(credentials, language="English"))
                address = wallet.rpc("get_address", {"account_index": 0})["address"]
                wallet.rpc("set_attribute", {"key": "file-io-ci", "value": value})
                wallet.rpc("store")
                large_size = cache.stat().st_size
                require(large_size > 2 * 1024 * 1024, "Wallet cache did not cross multiple read chunks")
                wallet.rpc("close_wallet", {"autosave_current": False})
                wallet.rpc("open_wallet", credentials)
                require(wallet.rpc("get_attribute", {"key": "file-io-ci"})["value"] == value,
                        "Large wallet metadata changed after reopening")
                value = "short metadata after replacing the larger cache"
                wallet.rpc("set_attribute", {"key": "file-io-ci", "value": value})
                wallet.rpc("store")
                require(cache.stat().st_size < large_size, "Wallet cache was not truncated")
            else:
                wallet.rpc("open_wallet", credentials)
            require(wallet.rpc("get_address", {"account_index": 0})["address"] == address,
                    "Wallet address changed after reopening")
            require(wallet.rpc("get_attribute", {"key": "file-io-ci"})["value"] == value,
                    "Wallet metadata changed after reopening")
            require((wallets / (filename + ".keys")).stat().st_size > 0, "Empty wallet keys file")
            wallet.rpc("stop_wallet")
            wallet.stopped()
    print("PASS: wallet cache/key persistence, Unicode metadata, truncation, and service restart", flush=True)


def test_daemon(binary, root, logs):
    data = root / "daemon"
    data.mkdir()
    network_data = data / "fake"  # --regtest appends this to --data-dir.
    port = free_port()
    cert, key, fingerprint = [network_data / ("rpc_ssl" + suffix)
                              for suffix in (".crt", ".key", ".fingerprint")]
    original = None
    for run in range(3):
        if run == 2:
            # Exercise the compatibility path that recreates a missing fingerprint.
            fingerprint.chmod(stat.S_IREAD | stat.S_IWRITE)
            fingerprint.unlink()
        command = [binary, "--regtest", "--keep-fakechain", "--offline", "--non-interactive",
                   "--data-dir", data, "--rpc-bind-ip", "127.0.0.1",
                   "--rpc-bind-port", str(port), "--rpc-ssl", "enabled",
                   "--no-zmq", "--disable-dns-checkpoints", "--check-updates", "disabled",
                   "--log-file", logs / ("daemon-{}.log".format(run))]
        with Service(command, "https://127.0.0.1:" + str(port),
                     logs / ("daemon-{}-console.log".format(run))) as daemon:
            daemon.ready(lambda: daemon.request("/get_info", timeout=2))
            pair = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            pair.load_cert_chain(str(cert), str(key))
            der = ssl.PEM_cert_to_DER_cert(cert.read_text(encoding="ascii"))
            expected = ":".join("{:02X}".format(byte) for byte in hashlib.sha256(der).digest())
            require(fingerprint.read_text(encoding="ascii").strip() == expected,
                    "Saved TLS fingerprint does not match certificate")
            saved = (cert.read_bytes(), key.read_bytes(), fingerprint.read_bytes())
            if original is None:
                original = saved
            else:
                require(saved == original, "TLS credentials changed on daemon restart")
            if os.name != "nt":
                require(stat.S_IMODE(key.stat().st_mode) & 0o077 == 0,
                        "TLS private key is accessible to group or other users")
            daemon.request("/stop_daemon")
            daemon.stopped()
        require((network_data / "p2pstate.bin").stat().st_size > 0,
                "Daemon failed to persist the peer list on shutdown")
    print("PASS: TLS keys/certificate/fingerprint, restart, fingerprint regeneration, and peer-list file", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    args = parser.parse_args()
    binaries = args.build_dir.resolve() / "bin"
    logs = args.log_dir.resolve()
    logs.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if os.name == "nt" else ""
    wallet = binaries / ("monero-wallet-rpc" + suffix)
    daemon = binaries / ("monerod" + suffix)
    require(wallet.is_file() and daemon.is_file(), "Build monerod and monero-wallet-rpc first")
    with tempfile.TemporaryDirectory(prefix="monero-file-io-ci-") as directory:
        root = Path(directory)
        test_wallet(wallet, root, logs)
        test_daemon(daemon, root, logs)
    print("All file persistence integration checks passed", flush=True)


if __name__ == "__main__":
    main()
