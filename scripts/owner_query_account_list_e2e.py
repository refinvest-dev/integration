"""ADR-087 ordinary Core/Go Main list acceptance, never operational activation."""
from concurrent.futures import ThreadPoolExecutor
import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time

from owner_query_account_list_fault import ListFaults, PageMutation
from owner_query_e2e import ACCOUNT, BOOK, MEMBER, TRACE_ID, TRACE_PARENT, private_write, require
from owner_query_fleet_relay import FleetRelay
from owner_query_main_e2e import MainServiceAcceptance
from owner_query_transport_e2e import TransportServiceAcceptance

LIST = "/trading-accounts"
INTERNAL = "/internal/trading-review/v1/accounts"
LIST_SCOPE = "trading-review:owner:accounts:list"
READ_SCOPE = "trading-review:owner:read"


def claims(compact):
    raw = compact.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))


def transport_error_codes(error):
    codes, seen = [], set()
    while error is not None and id(error) not in seen and len(seen) < 16:
        seen.add(id(error))
        code = {name:value for name in ("errno","winerror") if type(value := getattr(error,name,None)) is int}
        if code:
            codes.append(code)
        error = error.__cause__ or error.__context__
    return codes


class AccountListServiceAcceptance(TransportServiceAcceptance):
    primary_path = LIST + "?size=100"
    secondary_path = LIST + "?size=100"

    def __init__(self, args):
        super().__init__(args)
        self.pg_env["PGCLIENTENCODING"] = "UTF8"

    def execute(self):
        try:
            super().execute()
        except Exception as error:
            self.evidence["transportErrorCodes"] = transport_error_codes(error)
            raise

    def configure(self):
        MainServiceAcceptance.configure(self)
        self.body_faults = ListFaults()
        self.metadata_relay = FleetRelay("metadata", self.directory / "root-ca.pem", self.directory / "observer-cert.pem",
                                         self.directory / "observer-key.pem", body_fault=self.body_faults, account_list=True)
        self.metadata_relay.targets.register("go-a", self.go_origin)
        self.metadata_relay.targets.select("go-a", diagnostic=True)
        path = self.directory / "core-runtime.json"
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        snapshot.update(goOrigin=self.metadata_relay.origin, requestTimeout="PT3S", connectTimeout="PT1S",
                        queryTimeoutSeconds=1, maxInFlight=1)
        private_write(path, snapshot)
        self.go_config.update(requestTimeout="2s", authorityTimeout="1s", metadataEnabled=False, accountListEnabled=True)
        self.go_config["callers"][0].update(readAllowed=False, listAccountsAllowed=True)
        private_write(self.directory / "go-runtime.json", self.go_config)
        self.settings.update({"trading-review.owner-query-proxy.enabled": False,
                              "trading-review.account-list-proxy.enabled": True})
        self.update_core_environment()
        self.evidence.update(coreMode="ordinary-main-account-list", operationalActivation=False, browserUIExecuted=False,
            topology="Core Main -> list transport TLS relay -> Go Main -> authority TLS observer -> Core Main",
            fixtureBudgets={"coreRequestSeconds": 3, "goRequestSeconds": 2, "authoritySeconds": 1, "coreListMaxInFlight": 1})

    def record_sources(self):
        super().record_sources()
        names = ("owner_query_account_list_e2e.py", "owner_query_account_list_fault.py")
        self.evidence["accountListHarnessSources"] = {n: hashlib.sha256((Path(__file__).parent / n).read_bytes()).hexdigest() for n in names}
        self.evidence["contractCheckoutRevisions"] = {}
        for name in ("compute", "web"):
            revision = subprocess.run(["git", "-C", str(getattr(self.args,name+"_path")), "rev-parse", "HEAD"],
                                      capture_output=True,text=True,timeout=15)
            require(revision.returncode == 0,"CONTRACT_REVISION_UNAVAILABLE")
            self.evidence["contractCheckoutRevisions"][name] = revision.stdout.strip()

    def seed_page(self):
        self.sql(f"""UPDATE trading_accounts SET created_at='2026-01-01T00:00:00Z' WHERE id={ACCOUNT};
INSERT INTO trading_accounts(id,owner_member_id,venue,display_name,status,created_at,updated_at)
SELECT n,{MEMBER},'BINANCE',repeat('가',100),CASE WHEN n=9000 THEN 'ARCHIVED' ELSE 'ACTIVE' END,
'2026-01-01T00:00:00Z',now() FROM generate_series(9000,9098) n;
INSERT INTO trading_accounts(id,owner_member_id,venue,display_name,status,created_at,updated_at)
SELECT n,{MEMBER},'BINANCE','hidden',CASE WHEN n=8000 THEN 'DELETION_PENDING' WHEN n=8001 THEN 'DELETION_FAILED' ELSE 'ACTIVE' END,now(),now()
FROM generate_series(8000,8002) n;
INSERT INTO trading_deletion_requests(id,owner_member_id,target_type,target_id,requested_scope,state,deletion_generation,revoked_at,requested_at,updated_at,backup_purge_due_at)
VALUES(7000,{MEMBER},'TRADING_ACCOUNT',8002,'ALL','COMPLETED',1,now(),now(),now(),now());
INSERT INTO trading_deleted_resources VALUES({MEMBER},'TRADING_ACCOUNT',8002,7000);""")

    def page(self, query="", *, role="MEMBER"):
        response = self.request(LIST + query, role=role)
        page = response.json()
        require(set(page) == {"items", "page", "size", "total"} and type(page["total"]) is int, "PUBLIC_PAGE_ENVELOPE")
        require(all(set(i) == {"id", "venue", "displayName", "status", "createdAt"} for i in page["items"]), "PUBLIC_ITEM_FIELDS")
        return page, response

    def request(self, path, status=200, code=None, *, role="MEMBER", method="GET", authenticate=True):
        response = super().request(path,status,code,role=role,method=method,authenticate=authenticate)
        if code:
            messages = {"INVALID_REQUEST":"Invalid request", "UPSTREAM_SERVICE_ERROR":"Upstream service error",
                        "MEMBER_ACCESS_DENIED":"Access denied", "AUTHORITY_UNAVAILABLE":"Service unavailable",
                        "OWNER_QUERY_UNAVAILABLE":"Service unavailable", "REQUEST_DEADLINE_EXCEEDED":"Request deadline exceeded"}
            body = response.json()
            require(body["message"] == messages[code] and body["retryable"] is (status in (503,504)),"ERROR_REGISTRY")
            require("WWW-Authenticate" not in response.headers and "Location" not in response.headers,"INTERNAL_CHALLENGE_LEAK")
        return response

    def recovered(self):
        page, _ = self.page("?size=100")
        require(page["total"] == 100 and len(page["items"]) == 100, "LIST_SLOT_NOT_REUSABLE")

    def direct(self, path, token, status, code=None):
        response = self.session.get(self.go_origin + path, headers={"Authorization": "Bearer " + self.service_credential,
            "X-RefInvest-User-Delegation": token, "traceparent": TRACE_PARENT,
            "baggage": "private=must-not-propagate"}, timeout=(3, 6))
        require(response.status_code == status, "INTERNAL_STATUS_" + str(response.status_code))
        if code:
            require(response.json().get("code") == code and response.headers.get("Cache-Control") == "no-store", "INTERNAL_ERROR")
        return response

    def signed(self, token, **changes):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        payload = claims(token)
        payload.update(changes)
        encode = lambda v: base64.urlsafe_b64encode(json.dumps(v, separators=(",", ":")).encode()).decode().rstrip("=")
        header = token.split(".")[0]
        data = header + "." + encode(payload)
        snapshot = json.loads((self.directory / "core-runtime.json").read_text())
        key = serialization.load_pem_private_key(snapshot["privateKey"].encode(), password=None)
        signature = key.sign(data.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        return data + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")

    def restart_go(self, label):
        self.stop(self.go_process)
        private_write(self.directory / "go-runtime.json", self.go_config)
        self.go_process = self.start(label, [str(self.args.go_binary)], self.go_env, self.go_origin + "/readyz")

    def restart_modes(self, label, metadata, account_list):
        self.stop(self.core_process)
        self.go_config.update(metadataEnabled=metadata, accountListEnabled=account_list)
        self.go_config["callers"][0].update(readAllowed=metadata, listAccountsAllowed=account_list)
        self.restart_go("go-" + label)
        self.settings.update({"trading-review.owner-query-proxy.enabled": metadata,
                              "trading-review.account-list-proxy.enabled": account_list})
        self.update_core_environment()
        java = str(self.args.java or shutil.which("java") or "java")
        self.core_process = self.start("core-" + label, [java, "@" + str(self.directory / "core.args")],
                                       self.core_env, self.core_origin + "/actuator/health")
        self.inspect_beans()

    def baseline_list(self):
        before = len(self.observations())
        page, response = self.page("?size=100")
        expected = [ACCOUNT] + [str(n) for n in range(9098,8999,-1)]
        require([i["id"] for i in page["items"]] == expected and page["total"] == 100 and len(response.content) > 16384,
                "MAX_PAGE_VISIBILITY_ORDER_OR_CAP")
        require(page["items"][-1]["status"] == "ARCHIVED" and len(self.observations()) == before + 1, "LIST_AUTHORITY_HOP")
        require(claims(self.metadata_relay.token.peek())["scope"] == LIST_SCOPE, "KOTLIN_LIST_SCOPE")
        self.metadata_relay.targets.confirm("go-a")
        self.metadata_relay.targets.select("go-a")
        self.mark("actual_kotlin_list_delegation_go_authority_pg_max_page_big_id_and_visibility")

    def query_matrix(self):
        for query, page, size, count in [("",0,20,20),("?page=0&size=1",0,1,1),("?size=100",0,100,100),
            ("?page=1&size=100",1,100,0),("?page=2147483647&size=100",2147483647,100,0),("?%70age=0&%73ize=1",0,1,1)]:
            actual, _ = self.page(query)
            require((actual["page"],actual["size"],len(actual["items"]),actual["total"]) == (page,size,count,100), "QUERY_OR_OFFSET")
        token = self.metadata_relay.token.peek()
        for query in ("?page=", "?size=", "?page=00", "?page=-1", "?size=01", "?size=0", "?size=101", "?page=2147483648",
                      "?page=0&page=0", "?page=0&%70age=0", "?ownerId=2", "?sort=id", "?size=1e2", "?size=+1"):
            before = self.metadata_relay.counts["forwarded"]
            self.request(LIST + query,400,"INVALID_REQUEST")
            require(self.metadata_relay.counts["forwarded"] == before, "INVALID_PUBLIC_QUERY_REACHED_GO")
            self.direct(INTERNAL + query,token,400,"INVALID_REQUEST")
        self.mark("public_internal_query_boundaries_canonical_defaults_and_int64_offset")

    def authority_and_visibility(self):
        require(self.page("?size=100",role="ADMIN")[0]["total"] == 100, "ADMIN_OWNER_OVERRIDE")
        self.sql(f"""INSERT INTO trading_books(id,account_id,owner_member_id,venue,display_name,product_family,position_mode,settlement_asset,
review_timezone,status,deletion_generation,created_at,updated_at) VALUES
(7001,{ACCOUNT},{MEMBER},'BINANCE','deleted child','LINEAR_PERPETUAL','ONE_WAY','USDT','UTC','DELETION_PENDING',1,now(),now());""")
        self.recovered()
        for table,where,column in [("auth_member_access",f"member_id={MEMBER}","allowed"),
                ("auth_authority_callers","caller_id='core-api'","enabled"),("auth_authority_keys","TRUE","enabled")]:
            self.sql(f"UPDATE {table} SET {column}=FALSE,revision=revision+1 WHERE {where};")
            self.request(LIST,403,"MEMBER_ACCESS_DENIED")
            self.sql(f"UPDATE {table} SET {column}=TRUE,revision=revision+1 WHERE {where};")
            self.recovered()
        token = self.metadata_relay.token.peek()
        jti_hash = hashlib.sha256(claims(token)["jti"].encode()).hexdigest()
        self.sql(f"INSERT INTO auth_authority_revocations VALUES ('{claims(token)['iss']}','{jti_hash}',now()+interval '10 minutes');")
        self.direct(INTERNAL,token,403,"MEMBER_ACCESS_DENIED")
        self.recovered()
        self.sql("UPDATE auth_authority_policy SET ready=FALSE;")
        self.request(LIST,503,"AUTHORITY_UNAVAILABLE")
        require(self.session.get(self.go_origin + "/readyz",timeout=3).status_code == 200,"READINESS_IS_NOT_AUTHORITY")
        self.sql("UPDATE auth_authority_policy SET ready=TRUE;")
        self.recovered()
        self.mark("member_caller_key_jti_commit_revocation_no_positive_cache_and_readiness_separation")
        self.mark("foreign_admin_deleted_tombstone_and_book_only_visibility")

    def mode_and_token_matrix(self):
        token = self.metadata_relay.token.peek()
        self.direct(INTERNAL + "/" + ACCOUNT,token,404)
        before = self.metadata_relay.counts["forwarded"]
        # Unselected metadata retains legacy Core behavior, not an authenticated Go proxy.
        reply = self.session.get(self.core_origin + LIST + "/" + ACCOUNT,
            headers={"Cookie":"REFINVEST_ACCESS_TOKEN="+self.browser_token()},timeout=(3,6))
        require(reply.status_code == 200 and self.metadata_relay.counts["forwarded"] == before,"LIST_ENABLED_METADATA_PROXY")
        self.restart_modes("both",True,True)
        self.recovered()
        token = self.metadata_relay.token.peek()
        self.request(LIST + "/" + ACCOUNT)
        metadata_token = self.metadata_relay.token.peek()
        self.request("/trading-books/" + BOOK)
        for path,wrong in [(INTERNAL,metadata_token),(INTERNAL + "/" + ACCOUNT,token)]:
            before = len(self.observations())
            self.direct(path,wrong,401,"USER_DELEGATION_INVALID")
            require(len(self.observations()) == before,"CROSS_SCOPE_REACHED_AUTHORITY")
        for changes in ({"scope":[LIST_SCOPE]},{"scope":LIST_SCOPE+" "+READ_SCOPE},{"scope":"trading-review:owner:books:list"},
                        {"aud":"draft:audience"},{"act":{"sub":"foreign-service"}},{"exp":int(time.time())-1}):
            before = len(self.observations())
            self.direct(INTERNAL,self.signed(token,**changes),401,"USER_DELEGATION_INVALID")
            require(len(self.observations()) == before,"INVALID_PROFILE_REACHED_AUTHORITY")
        self.go_config["callers"][0]["listAccountsAllowed"] = False
        self.restart_go("go-read-only-permission")
        before = len(self.observations())
        self.request(LIST,502,"UPSTREAM_SERVICE_ERROR")
        require(len(self.observations()) == before,"READ_PERMISSION_INHERITED")
        self.restart_modes("metadata-only",True,False)
        before = self.metadata_relay.counts["forwarded"]
        legacy = self.session.get(self.core_origin+LIST,headers={"Cookie":"REFINVEST_ACCESS_TOKEN="+self.browser_token()},timeout=(3,6))
        require(legacy.status_code == 200 and self.metadata_relay.counts["forwarded"] == before,"METADATA_ENABLED_LIST_PROXY")
        self.direct(INTERNAL,token,404)
        self.restart_modes("list-only-again",False,True)
        self.recovered()
        self.mark("independent_three_main_modes_two_kotlin_scopes_and_caller_permission_no_fallback")

    def decoder_faults(self):
        self.body_faults.mutate("longmax")
        require(self.page("?size=100")[0]["total"] == 9223372036854775807,"LOSSY_TOTAL_DECODER")
        for mutation in ("overflow_total","duplicate_field","unknown_field","missing_field","wrong_echo","duplicate_id","unsorted","invalid_utf8"):
            fault = self.body_faults.mutate(mutation)
            self.request(self.primary_path,502,"UPSTREAM_SERVICE_ERROR")
            require(fault.finished.wait(timeout=2),"MUTATION_NOT_DELIVERED")
            self.recovered()
        self.mark("synthetic_decoder_longmax_overflow_structure_echo_ids_order_utf8_after_real_go_response")

    def scenarios(self):
        self.inspect_beans()
        self.seed_page()
        self.baseline_list()
        self.query_matrix()
        self.authority_and_visibility()
        self.request(LIST,401,authenticate=False)
        self.request(LIST,403,method="POST")
        self.mark("browser_hs256_csrf_and_unauthenticated_no_store")
        self.mode_and_token_matrix()
        self.decoder_faults()
        self.deadline_and_slot()
        for kind in ("truncate","declared_oversize","stream_overflow"):
            self.rejected_body(kind)
        self.browser_abort()
        observations = self.observations()
        require(observations and all(r["parent"].startswith("00-"+TRACE_ID+"-") and r["parent"] != TRACE_PARENT and
                                    not r["baggagePresent"] for r in observations),"LIST_TRACE")
        require(self.sql("SELECT count(*) FROM trading_idempotency;") == "0" and
                self.sql("SELECT count(*) FROM trading_import_sessions;") == "0","LIST_CREATED_DURABLE_WORK")
        self.mark("trace_children_no_baggage_and_no_durable_admission")
        self.healthy_body_drain()
        self.settings["trading-review.owner-query-runtime.drain-timeout"] = "PT0.1S"
        self.update_core_environment()
        java = str(self.args.java or shutil.which("java") or "java")
        self.core_process = self.start("core-list-short-drain",[java,"@"+str(self.directory/"core.args")],
                                       self.core_env,self.core_origin+"/actuator/health")
        self.forced_body_drain()
        self.evidence["listTransportFaults"] = [f.observation() for f in self.body_faults.records if not isinstance(f,PageMutation)]
        self.evidence["decoderMutations"] = [f.kind for f in self.body_faults.records if isinstance(f,PageMutation)]
