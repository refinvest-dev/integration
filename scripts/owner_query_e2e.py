"""Owned, non-production Core -> Go -> Core metadata service acceptance."""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import time
from urllib.parse import quote

from core_migrations import core_migrations
from process_control import powershell_executable

TRACE_ID = "12345678901234567890123456789012"
TRACE_PARENT = "00-" + TRACE_ID + "-1234567890123456-01"
MEMBER = "9007199254740993"
ACCOUNT = "9223372036854775806"
BOOK = "9223372036854775807"
ISSUER = "https://owner-query.integration.invalid/delegation"


class AcceptanceFailure(Exception):
    pass


def require(condition, code):
    if not condition:
        raise AcceptanceFailure(code)


def environment(path):
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if re.match(r"^\s*[A-Za-z_][A-Za-z0-9_]*=", line):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    require(values.get("POSTGRES_DB") == "refinvest_integration", "UNSAFE_DATABASE")
    host = values.get("INTEGRATION_POSTGRES_HOST", "127.0.0.1")
    require(host in ("localhost", "127.0.0.1", "::1"), "UNSAFE_DATABASE_HOST")
    port = values.get("INTEGRATION_POSTGRES_PORT", "5433")
    require(port.isdecimal() and 0 < int(port) < 65536, "INVALID_DATABASE_PORT")
    require(bool(values.get("POSTGRES_USER")) and bool(values.get("POSTGRES_PASSWORD")), "MISSING_DATABASE_CREDENTIAL")
    values["INTEGRATION_POSTGRES_HOST"] = "127.0.0.1" if host == "localhost" else host
    return values


def free_port():
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


def private_write(path, value):
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def identities(directory):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID

    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "RefInvest synthetic owner query")])
    ca = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key()) \
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1)) \
        .not_valid_after(now + timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True, path_length=0), True) \
        .sign(ca_key, hashes.SHA256())

    def server():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        certificate = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()) \
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1)) \
            .not_valid_after(now + timedelta(days=1)).add_extension(x509.BasicConstraints(ca=False, path_length=None), True) \
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False) \
            .sign(ca_key, hashes.SHA256())
        return key, certificate

    pem = lambda cert: cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    private = lambda key: key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()).decode("ascii")
    go_key, go_certificate = server()
    core_key, core_certificate = server()
    signing = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = signing.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
    ca_pem = pem(ca)
    private_write(directory / "root-ca.pem", ca_pem)
    (directory / "core.p12").write_bytes(pkcs12.serialize_key_and_certificates(b"core-fixture", core_key, core_certificate,
        [ca], serialization.BestAvailableEncryption(b"fixture-only")))
    (directory / "core.p12").chmod(0o600)
    return {"ca": ca_pem, "goCertificate": pem(go_certificate) + ca_pem, "goPrivateKey": private(go_key),
            "publicKey": public, "privateKey": private(signing)}


class ServiceAcceptance:
    def __init__(self, args):
        self.args = args
        self.values = environment(args.environment_file)
        self.schema = "tnorm_" + secrets.token_hex(6)
        self.directory = args.output_root.resolve() / self.schema
        self.directory.mkdir(parents=True, exist_ok=False)
        self.directory.chmod(0o700)
        self.children = []
        self.logs = []
        self.evidence = {"result": "RUNNING", "schema": self.schema, "scenarios": {}, "startupAttempts": [], "processes": []}
        self.secrets = {self.values["POSTGRES_PASSWORD"]}
        self.schema_owned = False
        self.go_port = args.go_port or free_port()
        self.core_port = args.core_port or free_port()
        require(self.go_port != self.core_port, "PORT_COLLISION")
        self.core_origin = f"https://127.0.0.1:{self.core_port}"
        self.go_origin = f"https://127.0.0.1:{self.go_port}"
        self.psql = str(args.psql or shutil.which("psql") or shutil.which("psql.exe") or "C:/Program Files/PostgreSQL/17/bin/psql.exe")
        self.pg_env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PG")}
        self.pg_env.update(PGHOST=self.values["INTEGRATION_POSTGRES_HOST"], PGPORT=self.values.get("INTEGRATION_POSTGRES_PORT", "5433"),
                           PGUSER=self.values["POSTGRES_USER"], PGPASSWORD=self.values["POSTGRES_PASSWORD"], PGDATABASE="refinvest_integration")

    def sql(self, statement, *, scoped=True):
        env = dict(self.pg_env)
        if scoped:
            env["PGOPTIONS"] = "-c search_path=" + self.schema
        result = subprocess.run([self.psql, "-X", "-w", "-v", "ON_ERROR_STOP=1", "-tA"], input=statement,
                                env=env, capture_output=True, text=True, encoding="utf-8", timeout=40)
        require(result.returncode == 0, "FIXTURE_SQL_FAILED")
        return result.stdout.strip()

    def sync(self):
        command = [powershell_executable(), "-NoProfile", "-File", str(self.args.integration_path / "scripts/verify-contract-sync.ps1"),
                   "-ContextPath", str(self.args.context_path), "-CoreApiPath", str(self.args.core_path),
                   "-ComputeApiPath", str(self.args.compute_path), "-WebPath", str(self.args.web_path), "-IncludeWeb"]
        process, _ = self.spawn("contract-sync", command, dict(os.environ))
        try:
            code = process.wait(timeout=600)
        except subprocess.TimeoutExpired:
            self.stop(process)
            raise AcceptanceFailure("CONTRACT_SYNC_TIMEOUT") from None
        require(code == 0, "CONTRACT_SYNC_FAILED")
        self.evidence["contractSync"] = "PASS"

    def initialize(self):
        require(self.sql("SELECT current_database();", scoped=False) == "refinvest_integration", "DATABASE_IDENTITY_MISMATCH")
        self.sql("CREATE SCHEMA " + self.schema + ";", scoped=False)
        self.schema_owned = True
        migrations = core_migrations(self.args.core_path)
        ddl = "\n".join(path.read_text(encoding="utf-8-sig") for path in migrations)
        candidate = self.args.core_path / "auth/adapter/persistence/src/main/resources/sql/member-authority-candidate.sql"
        seed = f"""
INSERT INTO members(id,role,created_at) VALUES ({MEMBER},'MEMBER',now()),(9007199254740994,'MEMBER',now());
INSERT INTO auth_authority_policy VALUES (1,1,TRUE);
INSERT INTO auth_member_access VALUES ({MEMBER},TRUE,1),(9007199254740994,TRUE,1);
INSERT INTO auth_authority_callers VALUES ('core-api',TRUE,FALSE,TRUE,1),('trading-review-api',FALSE,TRUE,TRUE,1);
INSERT INTO auth_authority_keys VALUES ('{ISSUER}','fixture-key',TRUE,1);
INSERT INTO trading_accounts(id,owner_member_id,venue,display_name,status,created_at,updated_at) VALUES
({ACCOUNT},{MEMBER},'BINANCE','synthetic owner','ACTIVE',now(),now()),(202,9007199254740994,'BINANCE','foreign','ACTIVE',now(),now());
INSERT INTO trading_books(id,account_id,owner_member_id,venue,display_name,product_family,position_mode,settlement_asset,review_timezone,status,created_at,updated_at) VALUES
({BOOK},{ACCOUNT},{MEMBER},'BINANCE','synthetic book','LINEAR_PERPETUAL','ONE_WAY','USDT','Asia/Seoul','ACTIVE',now(),now()),
(302,202,9007199254740994,'BINANCE','foreign','LINEAR_PERPETUAL','ONE_WAY','USDT','UTC','ACTIVE',now(),now());
"""
        self.sql("BEGIN;\n" + ddl + "\n" + candidate.read_text(encoding="utf-8-sig") + "\n" + seed + "\nCOMMIT;")
        self.evidence["migrationFiles"] = [p.name for p in migrations]

    def configure(self):
        material = identities(self.directory)
        self.browser_secret = secrets.token_urlsafe(64)
        self.service_credential = secrets.token_urlsafe(32)
        self.authority_credential = secrets.token_urlsafe(32)
        self.secrets.update([self.browser_secret, self.service_credential, self.authority_credential,
                             material["privateKey"], material["goPrivateKey"]])
        self.go_config = {"tlsCertificate": material["goCertificate"], "tlsPrivateKey": material["goPrivateKey"],
            "keys": [{"issuer": ISSUER, "kid": "fixture-key", "publicKey": material["publicKey"]}],
            "callers": [{"id": "core-api", "credentials": [self.service_credential], "readAllowed": True}],
            "authorityEndpoint": self.core_origin + "/internal/trading-review/v1/delegations/check",
            "authorityRootCAs": material["ca"], "authorityCredential": self.authority_credential,
            "requestTimeout": "4s", "authorityTimeout": "2s", "maxInFlight": 16, "authorityConnections": 4, "maxConnections": 32}
        private_write(self.directory / "go-runtime.json", self.go_config)
        private_write(self.directory / "core-fixture.json", {"issuer": ISSUER, "kid": "fixture-key", "publicKey": material["publicKey"],
            "privateKey": material["privateKey"], "serviceCredential": self.service_credential,
            "authorityCredential": self.authority_credential, "goOrigin": self.go_origin, "goRootCA": material["ca"]})
        host, port = self.pg_env["PGHOST"], self.pg_env["PGPORT"]
        address = "[" + host + "]" if ":" in host else host
        self.go_env = dict(os.environ, TRADING_REVIEW_API_MODE="owner-query", TRADING_REVIEW_OWNER_QUERY_FILE=str(self.directory / "go-runtime.json"),
            TRADING_REVIEW_API_ADDR=f"127.0.0.1:{self.go_port}", TRADING_REVIEW_STARTUP_TIMEOUT="15s", TRADING_REVIEW_DB_MAX_CONNS="4",
            TRADING_REVIEW_DATABASE_URL=f"postgres://{quote(self.pg_env['PGUSER'],safe='')}:{quote(self.pg_env['PGPASSWORD'],safe='')}@{address}:{port}/refinvest_integration?sslmode=disable&search_path={self.schema}")
        settings = {"spring.config.import": "", "spring.datasource.url": f"jdbc:postgresql://{address}:{port}/refinvest_integration?currentSchema={self.schema}",
            "spring.datasource.username": self.pg_env["PGUSER"], "spring.datasource.password": self.pg_env["PGPASSWORD"],
            "spring.datasource.hikari.maximum-pool-size": 4, "spring.flyway.enabled": False, "spring.jpa.show-sql": False,
            "server.address": "127.0.0.1", "server.port": self.core_port, "server.ssl.enabled": True,
            "server.ssl.key-store": str(self.directory / "core.p12"), "server.ssl.key-store-type": "PKCS12",
            "server.ssl.key-store-password": "fixture-only", "server.tomcat.threads.max": 4, "server.tomcat.threads.min-spare": 2,
            "refinvest.id.node-id": 0, "refinvest.security.issuer": "https://browser.integration.invalid", "refinvest.security.audience": "integration-browser",
            "refinvest.security.jwt-secret": self.browser_secret, "refinvest.security.web-origin": "https://web.integration.invalid",
            "refinvest.compute.enabled": False, "refinvest.trading-review.retention-enabled": False, "refinvest.trading-review.deletion-enabled": False,
            "refinvest.trading-review.raw.root": str(self.directory / "raw-workspaces"),
            "trading-review.owner-query-proxy.enabled": True, "trading-review.owner-query-proxy.request-timeout": "8s"}
        self.core_env = dict(os.environ, SPRING_APPLICATION_JSON=json.dumps(settings),
                             REFINVEST_OWNER_QUERY_FIXTURE_FILE=str(self.directory / "core-fixture.json"))
        self.secrets.add(self.go_env["TRADING_REVIEW_DATABASE_URL"])

    def build(self):
        if not self.args.skip_build:
            wrapper = "gradlew.bat" if os.name == "nt" else "./gradlew"
            command = (["cmd.exe", "/d", "/c", wrapper] if os.name == "nt" else ["bash", wrapper]) + [":app:prepareOwnerQueryFixture", "--console=plain", "--no-daemon", "--max-workers=1"]
            with (self.directory / "core-build.log").open("w", encoding="utf-8") as output:
                result = subprocess.run(command, cwd=self.args.core_path, stdout=output, stderr=subprocess.STDOUT, timeout=600)
            require(result.returncode == 0, "CORE_FIXTURE_BUILD_FAILED")
        if self.args.go_binary is None:
            self.args.go_binary = self.directory / ("trading-review-api.exe" if os.name == "nt" else "trading-review-api")
            with (self.directory / "go-build.log").open("w", encoding="utf-8") as output:
                result = subprocess.run(["go", "build", "-o", str(self.args.go_binary), "./cmd/api"], cwd=self.args.go_path,
                                        stdout=output, stderr=subprocess.STDOUT, timeout=300)
            require(result.returncode == 0, "GO_BUILD_FAILED")
        require(self.args.go_binary.is_file(), "GO_BINARY_MISSING")
        classpath = (self.args.core_path / "app/build/owner-query-fixture-classpath.txt").read_text(encoding="utf-8")
        quoted = classpath.replace("\\", "\\\\").replace('"', '\\"')
        private_write(self.directory / "core.args", '-cp\n"' + quoted + '"\ncom.refinvestfixture.ownerquery.OwnerQueryServiceFixtureKt\n--spring.config.import=\n')
        self.evidence["goBinarySha256"] = hashlib.sha256(self.args.go_binary.read_bytes()).hexdigest()
        self.evidence["revisions"] = {}
        for name in ("context", "core", "go", "integration"):
            root = getattr(self.args, name + "_path")
            result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=15)
            require(result.returncode == 0, "SOURCE_REVISION_UNAVAILABLE")
            self.evidence["revisions"][name] = result.stdout.strip()

    def spawn(self, name, command, env):
        log = self.directory / (name + ".log")
        output = log.open("w", encoding="utf-8")
        self.logs.append((log, output))
        process = subprocess.Popen(command, cwd=self.directory, env=env, stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.children.append(process)
        self.evidence["processes"].append({"name": name, "pid": process.pid})
        return process, log

    def stop(self, process):
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            else:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def start(self, name, command, env, ready):
        import requests
        for attempt in range(1, 4):
            process, log = self.spawn(name + "-" + str(len(self.children)), command, env)
            deadline = time.monotonic() + self.args.timeout_seconds
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    if self.session.get(ready, timeout=(2, 3)).status_code == 200:
                        self.evidence["startupAttempts"].append({"service": name, "attempt": attempt, "result": "READY"})
                        return process
                except requests.RequestException:
                    pass
                time.sleep(0.25)
            self.stop(process)
            text = log.read_text(encoding="utf-8", errors="replace")
            known = os.name == "nt" and ("Bad address: listen" in text or "os_error_code\":10014" in text or "os_error_code\": 10014" in text)
            self.evidence["startupAttempts"].append({"service": name, "attempt": attempt,
                "result": "WINDOWS_LISTEN_FAILED" if known else "STARTUP_FAILED"})
            require(known and attempt < 3, "SERVICE_STARTUP_FAILED")
        raise AcceptanceFailure("SERVICE_STARTUP_FAILED")

    def browser_token(self, role="MEMBER"):
        encode = lambda value: base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")
        now = int(time.time())
        claims = {"sub": MEMBER, "role": role, "typ": "access", "iss": "https://browser.integration.invalid",
                  "aud": ["integration-browser"], "iat": now, "exp": now + 300, "jti": secrets.token_hex(16)}
        signing = encode({"alg": "HS256", "typ": "JWT"}) + "." + encode(claims)
        signature = base64.urlsafe_b64encode(hmac.new(self.browser_secret.encode(), signing.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        return signing + "." + signature

    def request(self, path, status=200, code=None, *, role="MEMBER", method="GET", authenticate=True):
        headers = {"traceparent": TRACE_PARENT, "baggage": "private=must-not-propagate", "X-Owner-ID": "2",
                   "X-RefInvest-User-Delegation": "browser-forged", "Authorization": "Bearer browser-forged"}
        if authenticate:
            headers["Cookie"] = "REFINVEST_ACCESS_TOKEN=" + self.browser_token(role)
        response = self.session.request(method, self.core_origin + path, headers=headers, timeout=(3, 12))
        require(response.status_code == status, "UNEXPECTED_PUBLIC_STATUS_" + str(response.status_code))
        if method == "GET":
            require(response.headers.get("Cache-Control") == "no-store", "PUBLIC_CACHE_POLICY")
        if code:
            body = response.json()
            require(body.get("code") == code, "UNEXPECTED_PUBLIC_ERROR")
            if status not in (401,):
                require(set(body) == {"code", "message", "traceId", "retryable"} and body["traceId"] == TRACE_ID,
                        "UNSAFE_PUBLIC_ERROR_ENVELOPE")
        return response

    def mark(self, name):
        self.evidence["scenarios"][name] = "PASS"
        self.checkpoint()

    def checkpoint(self, phase=None):
        if phase:
            self.evidence["phase"] = phase
        (self.directory / "evidence.json").write_text(json.dumps(self.evidence, indent=2), encoding="utf-8")

    def scenarios(self):
        account = self.request("/trading-accounts/" + ACCOUNT).json()
        require(account["id"] == ACCOUNT and set(account) == {"id", "venue", "displayName", "status", "createdAt"}, "ACCOUNT_DTO")
        book = self.request("/trading-books/" + BOOK).json()
        require(book["id"] == BOOK and book["tradingAccountId"] == ACCOUNT and book["reviewTimezone"] == "Asia/Seoul" and
                set(book) == {"id", "tradingAccountId", "venue", "productFamily", "positionMode", "settlementAsset", "reviewTimezone", "status", "createdAt"}, "BOOK_DTO")
        self.mark("authenticated_principal_delegation_authority_and_bigint_metadata")
        self.request("/trading-accounts/202", 404, "RESOURCE_NOT_FOUND", role="ADMIN")
        self.request("/trading-books/302", 404, "RESOURCE_NOT_FOUND")
        self.request("/trading-books/301", 404, "RESOURCE_NOT_FOUND")
        self.mark("foreign_absent_and_admin_ownership")
        self.request("/trading-accounts/" + ACCOUNT, 401, authenticate=False)
        self.request("/trading-accounts", 403, method="POST")
        self.request("/trading-accounts/" + ACCOUNT + "?ownerId=2", 400, "INVALID_REQUEST")
        self.mark("browser_auth_csrf_and_untrusted_headers")
        self.sql(f"UPDATE auth_member_access SET allowed=FALSE,revision=revision+1 WHERE member_id={MEMBER};")
        self.request("/trading-books/" + BOOK, 403, "MEMBER_ACCESS_DENIED")
        self.sql(f"UPDATE auth_member_access SET allowed=TRUE,revision=revision+1 WHERE member_id={MEMBER};")
        self.request("/trading-books/" + BOOK)
        self.mark("member_block_commit_and_no_positive_cache")
        self.sql("UPDATE auth_authority_keys SET enabled=FALSE,revision=revision+1;")
        self.request("/trading-books/" + BOOK, 403, "MEMBER_ACCESS_DENIED")
        self.sql("UPDATE auth_authority_keys SET enabled=TRUE,revision=revision+1;")
        self.sql("UPDATE auth_authority_policy SET ready=FALSE;")
        self.request("/trading-books/" + BOOK, 503, "AUTHORITY_UNAVAILABLE")
        require(self.session.get(self.go_origin + "/readyz", timeout=3).status_code == 200, "READINESS_CONFUSED_WITH_AUTHORITY")
        self.sql("UPDATE auth_authority_policy SET ready=TRUE;")
        self.mark("key_revocation_authority_unavailable_and_readiness")
        with ThreadPoolExecutor(max_workers=8) as workers:
            list(workers.map(lambda _: self.request("/trading-books/" + BOOK), range(8)))
        self.mark("eight_concurrent_callbacks_with_four_core_servlet_threads")
        self.deadline_and_abort()
        self.sql(f"UPDATE trading_accounts SET status='ARCHIVED' WHERE id={ACCOUNT}; UPDATE trading_books SET status='ARCHIVED' WHERE id={BOOK};")
        require(self.request("/trading-books/" + BOOK).json()["status"] == "ARCHIVED", "ARCHIVED_METADATA")
        self.sql(f"UPDATE trading_accounts SET status='DELETION_PENDING',deletion_generation=deletion_generation+1 WHERE id={ACCOUNT};")
        self.request("/trading-books/" + BOOK, 410, "RESOURCE_DELETED")
        self.mark("archived_metadata_and_current_parent_deletion_guard")
        self.stop(self.go_process)
        self.go_config["callers"][0]["credentials"] = [secrets.token_urlsafe(32)]
        private_write(self.directory / "go-runtime.json", self.go_config)
        self.go_process = self.start("go-reconfigured", [str(self.args.go_binary)], self.go_env, self.go_origin + "/readyz")
        self.request("/trading-books/" + BOOK, 502, "UPSTREAM_SERVICE_ERROR")
        self.mark("internal_auth_failure_maps_to_502_without_legacy_fallback")
        require(self.sql("SELECT count(*) FROM trading_idempotency;") == "0", "READ_CREATED_IDEMPOTENCY")
        require(self.sql("SELECT count(*) FROM trading_import_sessions;") == "0", "READ_CREATED_JOB")
        observations = [json.loads(line) for line in (self.directory / "authority-observations.jsonl").read_text().splitlines()]
        require(len(observations) >= 15 and all(r["parent"].startswith("00-" + TRACE_ID + "-") and
                r["parent"] != TRACE_PARENT and not r["baggagePresent"] for r in observations), "TRACE_PROPAGATION")
        self.mark("trace_children_without_baggage_or_durable_admission")

    def deadline_and_abort(self):
        env = dict(self.pg_env, PGOPTIONS="-c search_path=" + self.schema)
        locker, _ = self.spawn("record-lock", [self.psql, "-X", "-w", "-v", "ON_ERROR_STOP=1", "-c",
            "BEGIN; LOCK TABLE trading_accounts IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep(20); COMMIT;"], env)
        locked = "SELECT count(*) FROM pg_locks WHERE relation='trading_accounts'::regclass AND mode='AccessExclusiveLock' AND granted;"
        waiting = "SELECT count(*) FROM pg_locks WHERE relation='trading_accounts'::regclass AND NOT granted;"
        try:
            deadline = time.monotonic() + 5
            while self.sql(locked) != "1" and time.monotonic() < deadline:
                time.sleep(0.1)
            require(self.sql(locked) == "1", "LOCK_FAULT_NOT_ESTABLISHED")
            self.request("/trading-accounts/" + ACCOUNT, 504, "REQUEST_DEADLINE_EXCEEDED")
            self.mark("go_query_deadline_maps_to_public_504")
            context = ssl.create_default_context(cafile=str(self.directory / "root-ca.pem"))
            connection = context.wrap_socket(socket.create_connection(("127.0.0.1", self.core_port), timeout=3), server_hostname="127.0.0.1")
            try:
                message = f"GET /trading-accounts/{ACCOUNT} HTTP/1.1\r\nHost: 127.0.0.1\r\nCookie: REFINVEST_ACCESS_TOKEN={self.browser_token()}\r\ntraceparent: {TRACE_PARENT}\r\nConnection: close\r\n\r\n"
                connection.sendall(message.encode("ascii"))
                deadline = time.monotonic() + 3
                while self.sql(waiting) == "0" and time.monotonic() < deadline:
                    time.sleep(0.1)
                require(self.sql(waiting) != "0", "ABORT_QUERY_NOT_OBSERVED")
            finally:
                connection.close()
            deadline = time.monotonic() + 6
            while self.sql(waiting) != "0" and time.monotonic() < deadline:
                time.sleep(0.1)
            require(self.sql(waiting) == "0" and self.sql(locked) == "1", "ABORT_NOT_BOUNDED_BY_REQUEST_DEADLINE")
            self.mark("browser_disconnect_is_bounded_not_durable_job_cancellation")
        finally:
            self.sql("SELECT pg_cancel_backend(pid) FROM pg_locks WHERE relation='trading_accounts'::regclass AND mode='AccessExclusiveLock' AND granted;")
            self.stop(locker)
            deadline = time.monotonic() + 3
            while self.sql(locked) != "0" and time.monotonic() < deadline:
                time.sleep(0.1)
            require(self.sql(locked) == "0", "OWNED_FAULT_LOCK_NOT_RELEASED")
        self.request("/trading-accounts/" + ACCOUNT)

    def execute(self):
        import requests
        self.checkpoint("CONTRACT_SYNC")
        self.sync()
        self.checkpoint("DATABASE_FIXTURE")
        self.initialize()
        self.checkpoint("KEY_AND_CONFIGURATION_FIXTURE")
        self.configure()
        self.checkpoint("BUILD")
        self.build()
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = str(self.directory / "root-ca.pem")
        java = str(self.args.java or shutil.which("java") or "java")
        self.checkpoint("CORE_STARTUP")
        self.core_process = self.start("core", [java, "@" + str(self.directory / "core.args")], self.core_env, self.core_origin + "/actuator/health")
        self.checkpoint("GO_STARTUP")
        self.go_process = self.start("go", [str(self.args.go_binary)], self.go_env, self.go_origin + "/readyz")
        self.checkpoint("SCENARIOS")
        self.scenarios()
        self.evidence["result"] = "PASS"

    def cleanup(self):
        stop_errors = []
        for child in reversed(self.children):
            try:
                self.stop(child)
            except (OSError, subprocess.SubprocessError):
                stop_errors.append(child.pid)
        self.evidence["ownedProcessesStopped"] = all(p.poll() is not None for p in self.children)
        for _, handle in self.logs:
            handle.close()
        if self.schema_owned and self.evidence["ownedProcessesStopped"] and not stop_errors:
            require(re.fullmatch(r"tnorm_[0-9a-f]{12}", self.schema) is not None, "UNSAFE_CLEANUP_SCHEMA")
            require(self.sql("SELECT current_database();", scoped=False) == "refinvest_integration", "UNSAFE_CLEANUP_DATABASE")
            self.sql("DROP SCHEMA " + self.schema + " CASCADE;", scoped=False)
            self.evidence["ownedSchemaRemoved"] = True
        require(not stop_errors and self.evidence["ownedProcessesStopped"], "OWNED_PROCESS_CLEANUP_FAILED")
        for log, _ in self.logs:
            text = log.read_text(encoding="utf-8", errors="replace")
            for secret in sorted(self.secrets, key=len, reverse=True):
                for value in (secret, json.dumps(secret)[1:-1]):
                    if value:
                        text = text.replace(value, "<redacted>")
            log.write_text(text, encoding="utf-8")
        self.evidence["termination"] = "owned process termination; Windows forced exit is not graceful-drain evidence"
        (self.directory / "evidence.json").write_text(json.dumps(self.evidence, indent=2), encoding="utf-8")


def main():
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment-file", type=Path, default=root / ".env.local")
    parser.add_argument("--integration-path", type=Path, default=root)
    for name, suffix in [("context", "context"), ("core", "api/refinvest"), ("go", "trading-review-api/trading-review-api"),
                         ("compute", "compute-api/refinvest"), ("web", "web/web")]:
        parser.add_argument("--" + name + "-path", type=Path, default=root.parent / suffix)
    parser.add_argument("--output-root", type=Path, default=root / ".integration-data/owner-query-service")
    parser.add_argument("--go-binary", type=Path)
    parser.add_argument("--java", type=Path)
    parser.add_argument("--psql", type=Path)
    parser.add_argument("--go-port", type=int, default=0)
    parser.add_argument("--core-port", type=int, default=0)
    parser.add_argument("--timeout-seconds", type=int, default=90)
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    run = None
    try:
        run = ServiceAcceptance(args)
        run.execute()
    except Exception as error:
        code = str(error) if isinstance(error, AcceptanceFailure) else type(error).__name__
        if run:
            run.evidence.update(result="FAIL", failure=code)
        print("Owner query service acceptance failed: " + code, file=sys.stderr)
    finally:
        if run:
            try:
                run.cleanup()
            except Exception as error:
                run.evidence.update(result="FAIL", cleanupFailure=type(error).__name__)
                (run.directory / "evidence.json").write_text(json.dumps(run.evidence, indent=2), encoding="utf-8")
            print(json.dumps({"result": run.evidence["result"], "scenarios": len(run.evidence["scenarios"]),
                              "evidence": str(run.directory / "evidence.json")}))
    return 0 if run and run.evidence["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
