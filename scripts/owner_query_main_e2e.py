"""Ordinary production Core bootJar -> Go Main acceptance, using synthetic local state."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import zipfile

from owner_query_authority_observer import AuthorityObserver
from owner_query_e2e import MEMBER, ISSUER, TRACE_PARENT, ServiceAcceptance, private_write, require

LIFECYCLE_ACCOUNT = "903"
LEGACY_WORKERS = {
    "TradingNormalizationDispatchScheduler", "NormalizationResultSpoolSweeper", "TradingRawOrphanWorker",
    "TradingDeletionWorker", "TradingImportExpiryScheduler", "TradingRawRetentionWorker",
}


def inspect_jar(path):
    with zipfile.ZipFile(path) as archive:
        manifest = archive.read("META-INF/MANIFEST.MF").decode("utf-8").replace("\r\n ", "")
        require("Start-Class: com.refinvest.RefinvestApplicationKt" in manifest, "CORE_MAIN_CLASS_MISMATCH")
        require("Main-Class: org.springframework.boot.loader.launch.JarLauncher" in manifest, "CORE_NOT_BOOT_JAR")
        require(not any(name.startswith("BOOT-INF/classes/com/refinvestfixture/") for name in archive.namelist()),
                "CORE_CONTAINS_TEST_FIXTURE")


class MainServiceAcceptance(ServiceAcceptance):
    observer = None

    def configure(self):
        material, self.settings = super().configure()
        (self.directory / "core-fixture.json").unlink()
        self.core_env.pop("REFINVEST_OWNER_QUERY_FIXTURE_FILE", None)
        private_write(self.directory / "observer-cert.pem", material["goCertificate"])
        private_write(self.directory / "observer-key.pem", material["goPrivateKey"])
        self.observer = AuthorityObserver(self.core_origin, self.directory / "root-ca.pem",
                                          self.directory / "observer-cert.pem", self.directory / "observer-key.pem")
        self.go_config["authorityEndpoint"] = self.observer.origin + "/internal/trading-review/v1/delegations/check"
        private_write(self.directory / "go-runtime.json", self.go_config)
        private_write(self.directory / "core-runtime.json", {
            "issuer": ISSUER, "signingKid": "fixture-key", "privateKey": material["privateKey"],
            "keys": [{"issuer": ISSUER, "kid": "fixture-key", "publicKey": material["publicKey"]}],
            "goOrigin": self.go_origin, "goCredential": self.service_credential, "goRootCAs": material["ca"],
            "authorityCredentials": [self.authority_credential], "requestTimeout": "PT12S", "connectTimeout": "PT2S",
            "shutdownTimeout": "PT1S", "queryTimeoutSeconds": 3, "maxInFlight": 16,
        })
        self.settings.pop("trading-review.owner-query-proxy.request-timeout")
        self.settings.update({
            "trading-review.owner-query-runtime.enabled": True,
            "trading-review.owner-query-runtime.snapshot-file": str(self.directory / "core-runtime.json"),
            "trading-review.owner-query-runtime.preparation-timeout": "PT15S",
            "trading-review.owner-query-runtime.drain-timeout": "PT6S",
            "spring.datasource.hikari.maximum-pool-size": 2,
            "spring.datasource.hikari.minimum-idle": 2,
            "spring.datasource.hikari.connection-timeout": 1000,
            "refinvest.trading-review.normalization.delivery.spool-sweep-enabled": False,
            "management.endpoints.web.exposure.include": "health,beans,shutdown",
            "management.endpoint.beans.access": "read-only",
            "management.endpoint.shutdown.access": "unrestricted",
        })
        self.settings["spring.datasource.url"] += ("&connectTimeout=2&socketTimeout=5&cancelSignalTimeout=2&ApplicationName=" +
                                                  self.schema + "-core")
        self.update_core_environment()
        self.evidence.update(coreMode="ordinary-main", authorityHop="Core Main -> Go Main -> local TLS observer -> Core Main",
                             testInitializer=False, operationalActivation=False)

    def update_core_environment(self):
        self.core_env["SPRING_APPLICATION_JSON"] = json.dumps(self.settings)

    def build(self):
        if not self.args.skip_build:
            wrapper = "gradlew.bat" if os.name == "nt" else "./gradlew"
            command = (["cmd.exe", "/d", "/c", wrapper] if os.name == "nt" else ["bash", wrapper]) + [
                ":app:bootJar", "--console=plain", "--no-daemon", "--max-workers=1"]
            with (self.directory / "core-build.log").open("w", encoding="utf-8") as output:
                result = subprocess.run(command, cwd=self.args.core_path, stdout=output, stderr=subprocess.STDOUT, timeout=600)
            require(result.returncode == 0, "CORE_BOOT_JAR_BUILD_FAILED")
        if self.args.core_jar is None:
            jars = [p for p in (self.args.core_path / "app/build/libs").glob("*.jar") if not p.name.endswith("-plain.jar")]
            require(len(jars) == 1, "CORE_BOOT_JAR_AMBIGUOUS")
            self.args.core_jar = jars[0]
        require(self.args.core_jar.is_file(), "CORE_BOOT_JAR_MISSING")
        inspect_jar(self.args.core_jar)
        self.build_go()
        temporary = self.directory / "jvm-tmp"
        temporary.mkdir(mode=0o700)
        quoted = lambda p: str(p).replace("\\", "\\\\").replace('"', '\\"')
        private_write(self.directory / "core.args", '"-Djava.io.tmpdir=' + quoted(temporary) + '"\n-jar\n"' +
                      quoted(self.args.core_jar.resolve()) + '"\n--spring.config.import=\n')
        self.record_sources()
        self.evidence["coreBootJarSha256"] = hashlib.sha256(self.args.core_jar.read_bytes()).hexdigest()
        self.evidence["harnessSources"] = {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
            for name in ("owner_query_e2e.py", "owner_query_main_e2e.py", "owner_query_authority_observer.py")}

    def observations(self):
        with self.observer.lock:
            return list(self.observer.records)

    def scenarios(self):
        self.inspect_beans()
        super().scenarios()
        self.sql(f"INSERT INTO trading_accounts(id,owner_member_id,venue,display_name,status,created_at,updated_at) "
                 f"VALUES ({LIFECYCLE_ACCOUNT},{MEMBER},'BINANCE','lifecycle fixture','ACTIVE',now(),now());")
        self.stop(self.go_process)
        self.go_config["callers"][0]["credentials"] = [self.service_credential]
        self.go_config.update(requestTimeout="8s", authorityTimeout="6s")
        private_write(self.directory / "go-runtime.json", self.go_config)
        self.go_process = self.start("go-authority-faults", [str(self.args.go_binary)], self.go_env, self.go_origin + "/readyz")
        self.authority_faults()
        self.shutdown_scenario(forced=False)
        self.settings["trading-review.owner-query-runtime.drain-timeout"] = "PT0.1S"
        self.update_core_environment()
        java = str(self.args.java or shutil.which("java") or "java")
        self.core_process = self.start("core-short-drain", [java, "@" + str(self.directory / "core.args")],
                                       self.core_env, self.core_origin + "/actuator/health")
        self.inspect_beans()
        self.shutdown_scenario(forced=True)

    def inspect_beans(self):
        response = self.session.get(self.core_origin + "/actuator/beans",
                                    headers={"Cookie": "REFINVEST_ACCESS_TOKEN=" + self.browser_token()}, timeout=(3, 8))
        require(response.status_code == 200 and len(response.content) < 2 * 1024 * 1024, "BEAN_OBSERVATION_FAILED")
        types = [bean["type"] for context in response.json()["contexts"].values() for bean in context["beans"].values()]
        require(not any(t.rsplit(".", 1)[-1] in LEGACY_WORKERS for t in types), "LEGACY_WORKER_ENABLED")
        require(sum(t == "com.refinvest.core.config.ownerquery.OwnerQueryRuntime" for t in types) == 1, "RUNTIME_ROOT_NOT_UNIQUE")
        require(not any("com.refinvestfixture." in t for t in types), "TEST_FIXTURE_BEAN_PRESENT")
        self.mark("ordinary_main_runtime_root_and_six_legacy_workers_absent")

    @contextmanager
    def authority_lock(self):
        env = dict(self.pg_env, PGOPTIONS="-c search_path=" + self.schema, PGAPPNAME=self.schema + "-authority-fault")
        locker, _ = self.spawn("authority-lock-" + str(len(self.children)), [self.psql, "-X", "-w", "-v", "ON_ERROR_STOP=1", "-c",
            "BEGIN; LOCK TABLE auth_member_access IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep(25); COMMIT;"], env)
        try:
            self.await_sql("SELECT count(*) FROM pg_locks WHERE relation='auth_member_access'::regclass "
                           "AND mode='AccessExclusiveLock' AND granted;", "1", "AUTHORITY_LOCK_NOT_ESTABLISHED")
            yield
        finally:
            self.sql("SELECT pg_cancel_backend(l.pid) FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid "
                     "WHERE l.relation='auth_member_access'::regclass AND l.mode='AccessExclusiveLock' AND l.granted "
                     f"AND a.application_name='{self.schema}-authority-fault';")
            self.stop(locker)
            self.await_sql("SELECT count(*) FROM pg_locks WHERE relation='auth_member_access'::regclass "
                           "AND mode='AccessExclusiveLock' AND granted;", "0", "AUTHORITY_LOCK_NOT_RELEASED")

    def await_sql(self, sql, expected, failure, seconds=5):
        deadline = time.monotonic() + seconds
        while self.sql(sql) != expected and time.monotonic() < deadline:
            time.sleep(0.05)
        require(self.sql(sql) == expected, failure)

    def authority_faults(self):
        path = "/trading-accounts/" + LIFECYCLE_ACCOUNT
        waiters = "SELECT count(*) FROM pg_locks WHERE relation='auth_member_access'::regclass AND NOT granted;"
        with ThreadPoolExecutor(max_workers=2) as workers:
            with self.authority_lock():
                pending = [workers.submit(self.request, path) for _ in range(2)]
                self.await_sql(waiters, "2", "AUTHORITY_POOL_NOT_OCCUPIED", seconds=2)
                self.request(path, 503, "AUTHORITY_UNAVAILABLE")
                require(not any(f.done() for f in pending), "POOL_FAILURE_DID_NOT_PRECEDE_SQL_TIMEOUT")
            for future in pending:
                future.result(timeout=8)
        self.request(path)
        self.mark("primary_hikari_pool_exhaustion_is_bounded_and_recovers")
        with self.authority_lock():
            self.request(path, 503, "AUTHORITY_UNAVAILABLE")
            self.await_sql(waiters, "0", "AUTHORITY_SQL_NOT_CANCELLED")
        self.request(path)
        self.mark("primary_authority_sql_timeout_releases_connection_and_recovers")

    def trigger_shutdown(self):
        cookie = "REFINVEST_ACCESS_TOKEN=" + self.browser_token()
        csrf = self.session.get(self.core_origin + "/auth/csrf", headers={"Cookie": cookie}, timeout=(3, 5))
        require(csrf.status_code == 204, "SHUTDOWN_CSRF_NOT_ACQUIRED")
        token = csrf.cookies.get("XSRF-TOKEN") or self.session.cookies.get("XSRF-TOKEN")
        require(bool(token), "SHUTDOWN_CSRF_COOKIE_MISSING")
        result = self.session.post(self.core_origin + "/actuator/shutdown", data="{}", headers={
            "Cookie": cookie + "; XSRF-TOKEN=" + token, "X-XSRF-TOKEN": token,
            "Content-Type": "application/json"}, timeout=(3, 5))
        require(result.status_code == 200, "STANDARD_SHUTDOWN_NOT_ACCEPTED")

    def shutdown_scenario(self, *, forced):
        path = "/trading-accounts/" + LIFECYCLE_ACCOUNT
        name = next(p["name"] for p in self.evidence["processes"] if p["pid"] == self.core_process.pid)
        log = self.directory / (name + ".log")
        self.observer.arm()
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(self.request, path, 503 if forced else 200,
                                     "OWNER_QUERY_UNAVAILABLE" if forced else None)
            try:
                require(self.observer.held.wait(timeout=5), "CALLBACK_NOT_HELD")
                self.trigger_shutdown()
                if not forced:
                    deadline = time.monotonic() + 3
                    gate_closed = False
                    while time.monotonic() < deadline:
                        response = self.session.get(self.core_origin + path, headers={
                            "Cookie": "REFINVEST_ACCESS_TOKEN=" + self.browser_token(), "traceparent": TRACE_PARENT}, timeout=(2, 2))
                        if response.status_code == 503 and response.json().get("code") == "OWNER_QUERY_UNAVAILABLE":
                            gate_closed = True
                            break
                        time.sleep(0.05)
                    require(gate_closed, "DRAIN_ADMISSION_NOT_CLOSED")
                    self.observer.release.set()
                pending.result(timeout=10)
                require(self.core_process.wait(timeout=15) == 0, "CORE_SHUTDOWN_EXIT_FAILED")
                if not forced:
                    require(self.observer.released_forward.is_set(), "LATE_CALLBACK_NOT_FORWARDED")
            finally:
                self.observer.release.set()
        text = log.read_text(encoding="utf-8", errors="replace")
        self.await_sql(f"SELECT count(*) FROM pg_stat_activity WHERE application_name='{self.schema}-core';",
                       "0", "CORE_PRIMARY_SESSIONS_NOT_RETURNED")
        if forced:
            require("Owner query shutdown incomplete" in text and "Owner query shutdown complete" not in text,
                    "FORCED_SHUTDOWN_INCOMPLETENESS_NOT_REPORTED")
            self.mark("ordinary_main_forced_budget_reports_incomplete_even_with_exit_zero")
        else:
            signals = [text.find(s) for s in ("Owner query shutdown complete", "Commencing graceful shutdown", "Shutdown initiated")]
            require(all(n >= 0 for n in signals) and signals == sorted(signals) and "Owner query shutdown incomplete" not in text,
                    "ORDINARY_MAIN_SHUTDOWN_ORDER_UNPROVEN")
            self.mark("ordinary_main_drains_late_callback_before_connector_and_primary_pool")

    def cleanup(self):
        try:
            if self.observer:
                self.observer.close()
                self.evidence["observerStopped"] = True
        finally:
            super().cleanup()
