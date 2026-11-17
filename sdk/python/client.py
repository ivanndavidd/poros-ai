"""Python SDK for the Poros (AI Distribution Administrator) consumer API (/v1/*).

Zero dependencies (uses urllib). A consumer app only needs an API key.

    from sdk.python.client import Client
    c = Client("http://localhost:8000", "sk_...")
    print(c.chat("total sales per region?", user_id="u1")["answer"])
"""
import json
import time
import urllib.request
import urllib.error


class ApiError(Exception):
    """`businesses` is filled on the BUSINESS_* refusals: the slices this caller may
    pick from. Show them to the user and resend with `business=` — retrying without
    one is never the fix, it does not widen what the role may read.
    `concepts` is filled on the SEMANTIC_* refusals (strict tenants): the curated
    meanings ({id, name, descr}) one KPI phrase could refer to — ask again naming one."""

    def __init__(self, code, message, status, businesses=None, concepts=None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.status = code, message, status
        self.businesses = businesses or []
        self.concepts = concepts or []


class Client:
    def __init__(self, base_url: str, api_key: str, timeout: int = 60):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _post(self, path: str, body: dict, jwt: str = None) -> dict:
        headers = {"Content-Type": "application/json", "X-API-Key": self.api_key}
        if jwt:
            headers["Authorization"] = "Bearer " + jwt   # end-user identity + role (verified server-side)
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode()).get("detail", {})
            except Exception:
                detail = {}
            if isinstance(detail, dict):
                raise ApiError(detail.get("code", "ERROR"), detail.get("message", str(e)),
                               e.code, detail.get("businesses"), detail.get("concepts"))
            raise ApiError("ERROR", str(detail), e.code)

    def _job_request(self, method: str, path: str, jwt: str = None) -> dict:
        headers = {"X-API-Key": self.api_key}
        if jwt:
            headers["Authorization"] = "Bearer " + jwt
        req = urllib.request.Request(self.base + path, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode()).get("detail", {})
            except Exception:
                detail = {}
            raise ApiError(
                detail.get("code", "ERROR") if isinstance(detail, dict) else "ERROR",
                detail.get("message", str(exc)) if isinstance(detail, dict) else str(detail),
                exc.code,
                detail.get("businesses") if isinstance(detail, dict) else None,
                detail.get("concepts") if isinstance(detail, dict) else None,
            )

    def chat(self, message: str, user_id: str = None, datasource_id: str = None,
             jwt: str = None, analysis_mode: str = "auto", history: list = None,
             business: str = None, confirmation: dict = None) -> dict:
        """Agent pipeline over DB + Docs + Memory; returns a governed, cited answer.
        Pass `jwt` (a signed end-user token) to act as that end-user's role/identity.
        Pass `business` (id or name, e.g. "3P") to answer from that slice only; a tenant
        may require it, and then a request without one is refused WITH the choices.
        A result with status == "awaiting_confirmation" is a QUESTION (which business),
        not an answer: show result["confirmation"]["options"] and resend the
        pending_question with `business` set. result["assumptions"] lists the readings
        taken instead of asking; a correction is chat(option["question"]).
        `confirmation` is accepted for older clients and unused."""
        return self._post("/v1/chat", {
            "message": message,
            "user_id": user_id,
            "datasource_id": datasource_id,
            "analysis_mode": analysis_mode,
            "history": history,
            "business": business,
            "confirmation": confirmation,
        }, jwt)

    def start_chat(self, message: str, datasource_id: str = None, jwt: str = None,
                   analysis_mode: str = "auto", history: list = None,
                   business: str = None, confirmation: dict = None) -> dict:
        """Queue gateway-safe chat/deep research and return its persistent job id."""
        return self._post("/v1/chat/jobs", {
            "message": message,
            "datasource_id": datasource_id,
            "analysis_mode": analysis_mode,
            "history": history,
            "business": business,
            "confirmation": confirmation,
        }, jwt)

    def get_chat_job(self, job_id: str, after: int = 0, jwt: str = None) -> dict:
        return self._job_request(
            "GET", f"/v1/chat/jobs/{job_id}?after={max(0, int(after))}", jwt
        )

    def cancel_chat_job(self, job_id: str, jwt: str = None) -> dict:
        return self._job_request("DELETE", f"/v1/chat/jobs/{job_id}", jwt)

    def chat_async(self, message: str, datasource_id: str = None, jwt: str = None,
                   analysis_mode: str = "auto", history: list = None,
                   poll_seconds: float = 0.9, on_event=None,
                   business: str = None, confirmation: dict = None) -> dict:
        """Start and poll until complete; no HTTP request spans the AI runtime."""
        started = self.start_chat(
            message, datasource_id=datasource_id, jwt=jwt,
            analysis_mode=analysis_mode, history=history, business=business,
            confirmation=confirmation,
        )
        after = 0
        while True:
            state = self.get_chat_job(started["job_id"], after=after, jwt=jwt)
            for event in state.get("events") or []:
                after = max(after, int(event.get("seq") or 0))
                if on_event:
                    on_event(event)
            if state["status"] == "succeeded":
                return state["result"]
            if state["status"] == "failed":
                error = state.get("error") or {}
                raise ApiError(error.get("code", "JOB_FAILED"),
                               error.get("message", "Job failed"), 400)
            if state["status"] == "cancelled":
                raise ApiError("CANCELLED", "Job cancelled", 409)
            time.sleep(max(0.2, poll_seconds))

    def feedback(self, trace_id: str, rating: str, reason: str = "",
                 comment: str = "", jwt: str = None) -> dict:
        """Rate one chat result. Re-submitting updates this user's feedback for the trace."""
        return self._post("/v1/feedback", {
            "trace_id": trace_id, "rating": rating, "reason": reason, "comment": comment
        }, jwt)

    def query(self, sql: str, datasource_id: str = None, jwt: str = None,
              business: str = None) -> dict:
        """Run a governed SQL SELECT (allowlist + RLAC/CLAC), inside `business` if given."""
        return self._post("/v1/query", {"sql": sql, "datasource_id": datasource_id,
                                        "business": business}, jwt)

    def retrieve(self, query: str, top_k: int = 5, datasource_id: str = None, jwt: str = None) -> dict:
        """Doc-RAG retrieval scoped to the (effective) role; returns citations."""
        return self._post("/v1/retrieve", {"query": query, "top_k": top_k, "datasource_id": datasource_id}, jwt)


if __name__ == "__main__":
    import sys
    base, key = sys.argv[1], sys.argv[2]
    c = Client(base, key)
    print(json.dumps(c.chat("total sales per region?"), indent=2))
