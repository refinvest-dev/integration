"""Two ordinary Core/Go processes with rolling immutable trust snapshots."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import time

from owner_query_e2e import ACCOUNT, BOOK, ISSUER, ServiceAcceptance, free_port, private_write, require
from owner_query_fleet_relay import AUTHORITY_PATH, FleetRelay
from owner_query_fleet_scenarios import FleetScenarios
from owner_query_main_e2e import LEGACY_WORKERS, MainServiceAcceptance

OLD_KID, NEW_KID = "fixture-key", "fixture-next-key"


class FleetNode:
    def __init__(self, label, port, directory):
        self.label, self.port, self.directory = label, port, directory
        self.origin = "https://127.0.0.1:" + str(port)
        self.role = label.split("-", 1)[0]
        self.revision = 0
        self.process = None
        self.serving = False
        self.config = {}
        self.directory.mkdir(mode=0o700)


class FleetServiceAcceptance(FleetScenarios, ServiceAcceptance):
    metadata_relay = None
    authority_relay = None

    def configure(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import secrets

        material, self.settings = super().configure()
        self.material = material
        (self.directory / "core-fixture.json").unlink()
        self.core_env.pop("REFINVEST_OWNER_QUERY_FIXTURE_FILE", None)
        private_write(self.directory / "relay-cert.pem", material["goCertificate"])
        private_write(self.directory / "relay-key.pem", material["goPrivateKey"])
        arguments = (self.directory / "root-ca.pem", self.directory / "relay-cert.pem", self.directory / "relay-key.pem")
        self.metadata_relay = FleetRelay("metadata", *arguments)
        self.authority_relay = FleetRelay("authority", *arguments)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode("ascii")
        public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
        self.keys = {OLD_KID: {"private": material["privateKey"], "public": material["publicKey"]},
                     NEW_KID: {"private": private, "public": public}}
        self.ingress_credentials = {"old": self.service_credential, "new": secrets.token_urlsafe(32)}
        self.callback_credentials = {"old": self.authority_credential, "new": secrets.token_urlsafe(32)}
        self.secrets.update([private, self.ingress_credentials["new"], self.callback_credentials["new"]])
        self.settings.pop("trading-review.owner-query-proxy.request-timeout")
        self.settings.update({
            "trading-review.owner-query-runtime.enabled": True,
            "trading-review.owner-query-runtime.preparation-timeout": "PT15S",
            "trading-review.owner-query-runtime.drain-timeout": "PT6S",
            "spring.datasource.hikari.maximum-pool-size": 2, "spring.datasource.hikari.minimum-idle": 1,
            "spring.datasource.hikari.connection-timeout": 1000,
            "refinvest.trading-review.normalization.delivery.spool-sweep-enabled": False,
            "management.endpoints.web.exposure.include": "health,beans,shutdown",
            "management.endpoint.beans.access": "read-only", "management.endpoint.shutdown.access": "unrestricted",
        })
        self.nodes = {}
        ports = {int(self.metadata_relay.origin.rsplit(":", 1)[1]), int(self.authority_relay.origin.rsplit(":", 1)[1])}
        for label in ("core-a", "core-b", "go-a", "go-b"):
            port = self.args.core_port if label == "core-a" else self.args.go_port if label == "go-a" else 0
            if not port:
                port = free_port()
                while port in ports:
                    port = free_port()
            require(port not in ports, "FLEET_PORT_COLLISION")
            ports.add(port)
            node = FleetNode(label, port, self.directory / label)
            self.nodes[label] = node
            self.relay(node).targets.register(label, node.origin)
            node.config = self.core_snapshot() if node.role == "core" else copy.deepcopy(self.go_config)
            if node.role == "go":
                node.config.update(authorityEndpoint=self.authority_relay.origin + AUTHORITY_PATH,
                                   requestTimeout="8s", authorityTimeout="6s", maxConnections=16)
            self.write_node(node)
        self.sql(f"INSERT INTO auth_authority_keys VALUES ('{ISSUER}','{NEW_KID}',TRUE,1);")
        self.evidence.update(coreMode="synthetic-fleet", coreNodes=2, goNodes=2, testInitializer=False,
            topology="Core nodes -> fixed loopback TLS metadata relay -> Go nodes -> fixed loopback TLS authority relay -> Core nodes",
            binaryVersionSkew=False, operationalActivation=False, matrices=[], rollouts=[])

    def core_snapshot(self):
        return {"issuer": ISSUER, "signingKid": OLD_KID, "privateKey": self.keys[OLD_KID]["private"],
            "keys": self.public_keys([OLD_KID]), "goOrigin": self.metadata_relay.origin,
            "goCredential": self.service_credential, "goRootCAs": self.material["ca"],
            "authorityCredentials": [self.authority_credential], "requestTimeout": "PT12S", "connectTimeout": "PT2S",
            "shutdownTimeout": "PT1S", "queryTimeoutSeconds": 3, "maxInFlight": 16}

    def public_keys(self, kids):
        return [{"issuer": ISSUER, "kid": kid, "publicKey": self.keys[kid]["public"]} for kid in kids]

    def relay(self, node):
        return self.metadata_relay if node.role == "go" else self.authority_relay

    def write_node(self, node):
        private_write(node.directory / "runtime.json", node.config)
        if node.role == "go":
            node.env = dict(self.go_env, TRADING_REVIEW_OWNER_QUERY_FILE=str(node.directory / "runtime.json"),
                            TRADING_REVIEW_API_ADDR="127.0.0.1:" + str(node.port), TRADING_REVIEW_DB_MAX_CONNS="2")
            return
        settings = dict(self.settings, **{
            "server.port": node.port,
            "trading-review.owner-query-runtime.snapshot-file": str(node.directory / "runtime.json"),
            "refinvest.trading-review.raw.root": str(node.directory / "raw-workspaces"),
        })
        settings["spring.datasource.url"] += "&connectTimeout=2&socketTimeout=5&cancelSignalTimeout=2&ApplicationName=" + self.schema + "-" + node.label
        node.env = dict(self.core_env, SPRING_APPLICATION_JSON=json.dumps(settings))

    def build(self):
        MainServiceAcceptance.build(self)
        for node in self.nodes.values():
            self.command(node)

    def command(self, node):
        if node.role == "go":
            return [str(self.args.go_binary)]
        temporary = node.directory / "jvm-tmp"
        temporary.mkdir(mode=0o700, exist_ok=True)
        quoted = lambda p: str(p).replace("\\", "\\\\").replace('"', '\\"')
        private_write(node.directory / "core.args", '"-Djava.io.tmpdir=' + quoted(temporary) + '"\n-jar\n"' +
                      quoted(self.args.core_jar.resolve()) + '"\n--spring.config.import=\n')
        return [str(self.args.java or shutil.which("java") or "java"), "@" + str(node.directory / "core.args")]

    def record_sources(self):
        super().record_sources()
        import hashlib
        self.evidence["fleetHarnessSources"] = {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
            for name in ("owner_query_e2e.py", "owner_query_main_e2e.py", "owner_query_fleet_e2e.py",
                         "owner_query_fleet_scenarios.py", "owner_query_fleet_relay.py")}

    def start_node(self, node):
        node.revision += 1
        node.process = self.start(node.label + "-r" + str(node.revision), self.command(node), node.env,
                                  node.origin + ("/readyz" if node.role == "go" else "/actuator/health"))
        if node.role == "core":
            response = self.session.get(node.origin + "/actuator/beans", headers={
                "Cookie": "REFINVEST_ACCESS_TOKEN=" + self.browser_token()}, timeout=(2, 8))
            require(response.status_code == 200, "FLEET_BEAN_OBSERVATION_FAILED")
            types = [bean["type"] for context in response.json()["contexts"].values() for bean in context["beans"].values()]
            require(not any(t.rsplit(".", 1)[-1] in LEGACY_WORKERS or "com.refinvestfixture." in t for t in types), "FLEET_LEGACY_OR_TEST_BEAN")
            require(types.count("com.refinvest.core.config.ownerquery.OwnerQueryRuntime") == 1, "FLEET_RUNTIME_NOT_UNIQUE")

    def request_node(self, node, *, book=False, status=200, code=None, diagnostic=False):
        require(node.serving or diagnostic, "UNCONFIRMED_INGRESS_NODE")
        previous = self.core_origin
        self.core_origin = node.origin
        try:
            response = self.request(("/trading-books/" + BOOK) if book else ("/trading-accounts/" + ACCOUNT), status, code)
            if status == 200:
                require(response.json()["id"] == (BOOK if book else ACCOUNT), "FLEET_METADATA_BINDING")
            return response
        finally:
            self.core_origin = previous

    def matrix(self, label, *, diagnostic=False):
        count = 0
        metadata_before = self.metadata_relay.counts["forwarded"]
        authority_before = self.authority_relay.counts["forwarded"]
        for core in self.role_nodes("core"):
            for go in self.role_nodes("go"):
                self.metadata_relay.targets.select(go.label, diagnostic=diagnostic)
                for authority in self.role_nodes("core"):
                    self.authority_relay.targets.select(authority.label, diagnostic=diagnostic)
                    for book in (False, True):
                        self.request_node(core, book=book, diagnostic=diagnostic)
                        count += 1
        metadata_hops = self.metadata_relay.counts["forwarded"] - metadata_before
        authority_hops = self.authority_relay.counts["forwarded"] - authority_before
        require(metadata_hops == count and authority_hops == count, "FLEET_REAL_HOPS_NOT_OBSERVED")
        self.evidence["matrices"].append({"phase": label, "combinations": 8, "resources": 2, "successfulRequests": count,
                                          "metadataHops": metadata_hops, "authorityHops": authority_hops})

    def role_nodes(self, role):
        return [n for n in self.nodes.values() if n.role == role]

    def confirm_node(self, node):
        for core in self.role_nodes("core"):
            if core != node and not core.serving:
                continue
            for go in self.role_nodes("go"):
                if go != node and not go.serving:
                    continue
                self.metadata_relay.targets.select(go.label, diagnostic=not go.serving)
                authorities = [node] if node.role == "core" else self.role_nodes("core")
                for authority in authorities:
                    if authority != node and not authority.serving:
                        continue
                    self.authority_relay.targets.select(authority.label, diagnostic=not authority.serving)
                    self.request_node(core, diagnostic=not core.serving)
                    self.request_node(core, book=True, diagnostic=not core.serving)
        self.relay(node).targets.confirm(node.label)
        node.serving = True

    def log_for(self, process):
        name = next(p["name"] for p in self.evidence["processes"] if p["pid"] == process.pid)
        return self.directory / (name + ".log")

    def retire_process(self, node):
        node.serving = False
        self.relay(node).targets.exclude(node.label)
        if node.role == "core":
            previous = self.core_origin
            self.core_origin = node.origin
            try:
                MainServiceAcceptance.trigger_shutdown(self)
            finally:
                self.core_origin = previous
            require(node.process.wait(timeout=20) == 0, "FLEET_CORE_SHUTDOWN_EXIT")
            log = self.log_for(node.process).read_text(encoding="utf-8", errors="replace")
            require("Owner query shutdown complete" in log and "Owner query shutdown incomplete" not in log, "FLEET_CORE_DRAIN_INCOMPLETE")
        else:
            self.stop(node.process)
        node.last_stopped_at = time.time()

    def roll(self, label, **changes):
        node = self.nodes[label]
        self.retire_process(node)
        survivors = [n for n in self.role_nodes("core") if n.serving]
        if survivors and any(n.serving for n in self.role_nodes("go")):
            self.request_node(survivors[0])
        node.config.update(changes)
        self.write_node(node)
        self.start_node(node)
        self.confirm_node(node)
        self.evidence["rollouts"].append({"node": label, "revision": node.revision, "confirmedByMetadata": True})
        self.checkpoint()

    def execute(self):
        import requests

        self.checkpoint("CONTRACT_SYNC")
        self.sync()
        self.checkpoint("DATABASE_FIXTURE")
        self.initialize()
        self.configure()
        self.checkpoint("BUILD")
        self.build()
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = str(self.directory / "root-ca.pem")
        self.checkpoint("FLEET_STARTUP")
        for node in self.nodes.values():
            self.start_node(node)
        self.matrix("initial-diagnostic-confirmation", diagnostic=True)
        for node in self.nodes.values():
            self.relay(node).targets.confirm(node.label)
            node.serving = True
        self.scenarios()
        for node in self.role_nodes("core"):
            self.retire_process(node)
        self.evidence["result"] = "PASS"

    def cleanup(self):
        try:
            for relay in (self.metadata_relay, self.authority_relay):
                if relay:
                    relay.close()
            self.evidence["relaysStopped"] = True
            self.evidence["delegationSlotsCleared"] = True
        finally:
            super().cleanup()
