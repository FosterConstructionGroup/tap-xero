import os
import re
import json
import decimal
from os.path import join
from datetime import datetime, date, time, timedelta
import requests
from singer.utils import strftime, strptime_to_utc
import six
import pytz

BASE_URL = "https://api.xero.com/api.xro/2.0"

refresh_token_path = os.path.join(os.path.dirname(__file__), "refresh_token.secret")


def get_token(config):
    # fall back to the refresh token in config on failure (which will be the first time it runs, or if it expires)
    try:
        with open(refresh_token_path) as f:
            refresh_token = f.read()
    except:
        refresh_token = config["refresh_token"]

    url = "https://identity.xero.com/connect/token"
    payload = f"grant_type=refresh_token&refresh_token={refresh_token}"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    auth = (config["client_id"], config["client_secret"])

    json = requests.post(url, auth=auth, headers=headers, data=payload).json()
    access_token = json["access_token"]
    refresh_token = json["refresh_token"]

    with open(refresh_token_path, "w") as f:
        f.write(refresh_token)

    return access_token


def parse_date(value):
    # Xero datetimes can be .NET JSON date strings which look like
    # "/Date(1419937200000+0000)/"
    # https://developer.xero.com/documentation/api/requests-and-responses
    pattern = r"Date\((\-?\d+)([-+])?(\d+)?\)"
    match = re.search(pattern, value)

    iso8601pattern = r"((\d{4})-([0-2]\d)-0?([0-3]\d)T([0-5]\d):([0-5]\d):([0-6]\d))"

    if not match:
        iso8601match = re.search(iso8601pattern, value)
        if iso8601match:
            try:
                return strptime_to_utc(value)
            except Exception:
                return None
        else:
            return None

    millis_timestamp, offset_sign, offset = match.groups()
    if offset:
        if offset_sign == "+":
            offset_sign = 1
        else:
            offset_sign = -1
        offset_hours = offset_sign * int(offset[:2])
        offset_minutes = offset_sign * int(offset[2:])
    else:
        offset_hours = 0
        offset_minutes = 0

    return datetime.utcfromtimestamp((int(millis_timestamp) / 1000)) + timedelta(
        hours=offset_hours, minutes=offset_minutes
    )


def _json_load_object_hook(_dict):
    """Hook for json.parse(...) to parse Xero date formats."""
    # This was taken from the pyxero library and modified
    # to format the dates according to RFC3339
    for key, value in _dict.items():
        if isinstance(value, six.string_types):
            value = parse_date(value)
            if value:
                # NB> Pylint disabled because, regardless of idioms, this is more explicit than isinstance.
                if type(value) is date:  # pylint: disable=unidiomatic-typecheck
                    value = datetime.combine(value, time.min)
                value = value.replace(tzinfo=pytz.UTC)
                _dict[key] = strftime(value)
    return _dict


class XeroClient:
    def __init__(self, config):
        self.session = requests.Session()
        self.user_agent = config.get("user_agent")
        self.tenant_id = None
        self.access_token = None

    def refresh_credentials(self, config):
        self.tenant_id = config["tenant_id"]
        # handles refresh, returns access token
        self.access_token = get_token(config)

    def fetch(self, tap_stream_id, since=None, **params):
        xero_resource_name = tap_stream_id.title().replace("_", "")
        url = join(BASE_URL, xero_resource_name)
        headers = {
            "Accept": "application/json",
            "Authorization": "Bearer " + self.access_token,
            "Xero-tenant-id": self.tenant_id,
        }
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        if since:
            headers["If-Modified-Since"] = since

        request = requests.Request(
            "GET", url, headers=headers, params={**params, "includeArchived": "true"}
        )
        response = self.session.send(request.prepare())
        response.raise_for_status()
        response_meta = json.loads(
            response.text,
            object_hook=_json_load_object_hook,
            parse_float=decimal.Decimal,
        )
        response_body = response_meta.pop(xero_resource_name)
        return response_body
