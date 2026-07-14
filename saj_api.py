"""
SAJ eSolar / elekeeper (iop.saj-electric.com) private Web API client.

Turns the inverter monitoring web portal into a plain Python API by replaying
the same signed requests the SPA makes. No browser required at call time.

Auth: reuses the JWT stored in the Hermes synced browser session at
  E:\\hermes-agent\\auth_states\\iop_saj-electric_com.json
(the "token" cookie). Refresh that session by logging in again in the browser
when the token expires (~7 day lifetime).

Request signing (reverse-engineered from static/js/index-*.js -> c_/c7):
  secret     = "b389a704-31f1-463d-8db7-435b18d1311d"  (VITE app secret, public in bundle)
  body_hash  = md5(raw_json_body_string).hexdigest()
  parts      = every "x-*" request header EXCEPT
               {x-timestamp, x-trace-id, x-sign, x-sign-enable}
               with the "x-" prefix stripped, PLUS  body=<body_hash>
  string     = "&".join(f"{k}={v}" for k in sorted(parts))
               + f"&nonce={x-trace-id}&timestamp={x-timestamp}"
  x-sign     = HMAC_SHA256(string, secret).hexdigest()
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
import datetime
from pathlib import Path
from urllib.parse import quote

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

BASE = "https://iop.saj-electric.com/dev-api"
APP_SECRET = "b389a704-31f1-463d-8db7-435b18d1311d"
# AES-128-ECB key used to encrypt the login password (bundle func mE, key E_).
LOGIN_AES_KEY = bytes.fromhex("ec1840a7c53cf0709eb784be480379b6")
SESSION_FILE = Path(r"E:\hermes-agent\auth_states\iop_saj-electric_com.json")


def _encrypt_password(plain: str) -> str:
    """Replicates bundle mE(): AES-128-ECB / PKCS7, returns lowercase hex ciphertext."""
    padder = PKCS7(128).padder()
    padded = padder.update(plain.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(LOGIN_AES_KEY), modes.ECB()).encryptor()
    return (enc.update(padded) + enc.finalize()).hex()

# Headers excluded from the signature (per the bundle's i7 set).
_SIGN_EXCLUDE = {"x-timestamp", "x-trace-id", "x-sign", "x-sign-enable"}


class SajError(RuntimeError):
    def __init__(self, err_code, err_msg, payload=None):
        super().__init__(f"SAJ API error {err_code}: {err_msg}")
        self.err_code = err_code
        self.err_msg = err_msg
        self.payload = payload


class SajClient:
    def __init__(self, token: str | None = None, org_code: str = "OAhz",
                 lang: str = "en", theme: str = "dark",
                 session_file: Path | str = SESSION_FILE,
                 username: str | None = None, password: str | None = None):
        # Credentials enable transparent auto-relogin when the token dies (10002).
        self._username = username
        self._password = password
        if token:
            self.token = token
        elif username and password:
            self.token = None  # logged in lazily on first call
        else:
            self.token = self._load_token(Path(session_file))
        self.org_code = org_code
        self.lang = lang
        self.theme = theme
        self.http = requests.Session()
        self.http.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://iop.saj-electric.com",
            "Referer": "https://iop.saj-electric.com/",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/148.0.0.0 Safari/537.36"),
        })

    # ---- auth loading -----------------------------------------------------
    @staticmethod
    def _load_token(path: Path) -> str:
        data = json.loads(path.read_text(encoding="utf-8"))
        for c in data.get("cookies", []):
            if c.get("name") == "token":
                return c["value"]
        raise RuntimeError(f"No 'token' cookie found in {path}")

    # ---- signing ----------------------------------------------------------
    def _client_date(self) -> str:
        return datetime.date.today().isoformat()

    def _sign(self, x_headers: dict, body_str: str, nonce: str, ts: str) -> str:
        parts = {}
        for k, v in x_headers.items():
            lk = k.lower()
            if lk.startswith("x-") and lk not in _SIGN_EXCLUDE and v not in (None, ""):
                parts[lk[2:]] = str(v)
        parts["body"] = hashlib.md5(body_str.encode()).hexdigest()
        seq = [f"{k}={parts[k]}" for k in sorted(parts)]
        seq.append(f"nonce={nonce}")
        seq.append(f"timestamp={ts}")
        string_to_sign = "&".join(seq)
        return hmac.new(APP_SECRET.encode(), string_to_sign.encode(),
                        hashlib.sha256).hexdigest()

    # ---- core call --------------------------------------------------------
    def call(self, path: str, payload: dict | None = None, timeout: int = 30,
             with_org: bool = True, with_token: bool = True) -> dict:
        """Signed POST with transparent auto-relogin on session expiry (10002).

        If constructed with username/password, a dead/absent token triggers one
        re-login and retry — so a long-running poller never needs babysitting.
        """
        if with_token and not self.token and self._username:
            self.login(self._username, self._password)
        try:
            return self._raw_call(path, payload, timeout, with_org, with_token)
        except SajError as e:
            if e.err_code in (10002,) and with_token and self._username:
                self.login(self._username, self._password)
                return self._raw_call(path, payload, timeout, with_org, with_token)
            raise

    def _raw_call(self, path: str, payload: dict | None = None, timeout: int = 30,
                  with_org: bool = True, with_token: bool = True) -> dict:
        """POST to a /dev-api endpoint with full signing. Returns the `data` field."""
        ts = int(time.time() * 1000)
        client_date = self._client_date()
        body = {
            "appProjectName": "elekeeper",
            "clientDate": client_date,
            "lang": self.lang,
            "timeStamp": ts,
            "clientId": "esolar-monitor-admin",
            "clientCode": "organization",
            "themeColor": self.theme,
        }
        if with_org:
            body["orgCode"] = self.org_code
        if payload:
            body.update(payload)
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

        nonce = uuid.uuid4().hex[:16]
        x_headers = {
            "X-App-Project-Name": "elekeeper",
            "X-Client-Code": "organization",
            "X-Lang": self.lang,
            "X-Client-Date": client_date,
            "X-Theme-Color": self.theme,
            "X-Timestamp": str(ts),
            "X-Trace-Id": nonce,
            "Content-Type": "application/json;charset=UTF-8",
            "lang": self.lang,
        }
        if with_token and self.token:
            x_headers["Authorization"] = f"Bearer {self.token}"
        if with_org:
            x_headers["X-Org-Code"] = self.org_code
        x_headers["X-Sign"] = self._sign(x_headers, body_str, nonce, str(ts))

        url = f"{BASE}{path}"
        resp = self.http.post(url, data=body_str.encode("utf-8"),
                              headers=x_headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        code = data.get("errCode", data.get("code"))
        if code not in (0, None, "0"):
            raise SajError(code, data.get("errMsg") or data.get("msg"), data)
        return data.get("data", data)

    # ---- login (auto token refresh) --------------------------------------
    def login(self, username: str, password: str) -> str:
        """Authenticate with email+password; store and return a fresh JWT.

        Real SPA flow (captured): JSON POST to /api/v2/sys/user/login with the
        password AES-128-ECB-encrypted, loginType=1, no orgCode, no prior token.
        """
        data = self.call("/api/v2/sys/user/login", {
            "username": username,
            "password": _encrypt_password(password),
            "loginType": 1,
            "rememberMe": False,
        }, with_org=False, with_token=False)
        tok = (data.get("token") or data.get("access_token") if isinstance(data, dict) else None)
        if not tok:
            raise SajError(-1, f"login returned no token: {data}", data)
        self.token = tok.replace("Bearer ", "").strip()
        # capture orgCode returned at login if present (else keep default)
        if isinstance(data, dict) and data.get("orgCode"):
            self.org_code = data["orgCode"]
        return self.token

    # ---- convenience endpoints -------------------------------------------
    def inverter_energy(self, device_sn: str) -> dict:
        """Live snapshot: power now, today/month/total energy, income, temp, battery."""
        return self.call("/api/v2/monitor/device/getInverterEnergyDetail",
                         {"deviceSn": device_sn})

    def inverter_base(self, device_sn: str) -> dict:
        """Static/device info: model, firmware, distributor, capabilities, aliases."""
        return self.call("/api/v2/monitor/device/baseInverterDetail",
                         {"deviceSn": device_sn})

    def supports_remote_cloudlink(self, device_sn: str, type_: int = 1) -> dict:
        return self.call("/api/v2/remote/setting/ifSupportRemoteCloudLink",
                         {"deviceSn": device_sn, "type": type_})

    def message_stats(self) -> dict:
        return self.call("/api/v2/monitor/msg/getMsgStatisticsV2")

    # ---- raw 5-minute time-series (the canonical generation feed) ---------
    def raw_data_page(self, device_sn: str, day: str, page_no: int = 1,
                      page_size: int = 300, device_type: int = 0) -> dict:
        """One page of 5-min raw rows for `day` (YYYY-MM-DD).

        Returns the full paging envelope: {list, total, pages, hasNextPage, ...}.
        Each row carries pac (AC W), PVP (PV W), pv1..6 V/I/P, gridList per-phase,
        deviceTemp, and today/month/year/total PVEnergy (kWh).
        """
        return self.call("/api/v2/monitor/deviceData/findRawdataPageList", {
            "deviceSn": device_sn,
            "deviceType": device_type,
            "timeStr": day,
            "startTime": f"{day} 00:00:00",
            "endTime": f"{day} 23:59:59",
            "pageNo": page_no,
            "pageSize": page_size,
        })

    def raw_data_day(self, device_sn: str, day: str, device_type: int = 0) -> list:
        """All 5-min rows for a device-day, oldest→newest, following pagination."""
        rows, page = [], 1
        while True:
            env = self.raw_data_page(device_sn, day, page_no=page,
                                     page_size=300, device_type=device_type)
            rows.extend(env.get("list", []))
            if not env.get("hasNextPage"):
                break
            page += 1
        rows.sort(key=lambda r: r.get("datetime") or "")
        return rows

    def latest_reading(self, device_sn: str, device_type: int = 0) -> dict | None:
        """Most recent 5-min row today — for the 15-min client-facing snapshot."""
        env = self.raw_data_page(device_sn, datetime.date.today().isoformat(),
                                 page_no=1, page_size=1, device_type=device_type)
        rows = env.get("list") or []
        return rows[0] if rows else None

    # ---- fleet enumeration ------------------------------------------------
    def plant_status_num(self) -> dict:
        """Fleet health counts: {totalNum, normalNum, offlineNum, alarmNum}."""
        return self.call("/api/v2/monitor/plant/getUserPlantListStatusNum")

    def list_plants(self, page_size: int = 100) -> list:
        """All plants (paged), each with plantUid/plantName/runningState/…."""
        plants, page = [], 1
        while True:
            env = self.call("/api/v2/monitor/plant/userPlantPage",
                            {"pageNo": page, "pageSize": page_size})
            batch = env.get("list") or []
            plants.extend(batch)
            total = env.get("total") or 0
            if len(plants) >= total or not batch:
                break
            page += 1
        return plants

    def plant_device_sns(self, plant_uid: str) -> list:
        """Device SNs belonging to a plant."""
        d = self.call("/api/v2/monitor/plantDevice/getPlantDeviceSnList",
                      {"plantUid": plant_uid})
        return d if isinstance(d, list) else (d.get("list") or [])

    def iter_all_devices(self, page_size: int = 100):
        """Yield (plant_uid, plant_name, device_sn) across the whole fleet."""
        for p in self.list_plants(page_size=page_size):
            uid = p.get("plantUid")
            name = p.get("plantName")
            for sn in self.plant_device_sns(uid):
                yield uid, name, sn


if __name__ == "__main__":
    import sys
    sn = sys.argv[1] if len(sys.argv) > 1 else "R6M2063J2516E18728"
    c = SajClient()
    print("== energy ==")
    print(json.dumps(c.inverter_energy(sn), indent=2, ensure_ascii=False))
    print("== base (subset) ==")
    b = c.inverter_base(sn)
    print(json.dumps({k: b.get(k) for k in
                      ("deviceSn", "aliases", "displayFw", "devicePc",
                       "distributorName", "deviceType")}, indent=2, ensure_ascii=False))
